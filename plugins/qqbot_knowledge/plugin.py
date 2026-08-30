from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar
import asyncio
import time

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .domains import resolve_domains
from .messages import extract_last_user_query, inject_evidence
from .source_index import RootSpec, SearchLimits, SourceIndex


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用单次请求源码证据注入")
    config_version: str = Field(default="0.1.0", description="配置版本")


class SourceRootConfig(PluginConfigBase):
    domain: str = Field(default="", description="知识域标识")
    path: str = Field(default="", description="源码或邻近文档的绝对路径")


def _default_roots() -> list[SourceRootConfig]:
    return [
        SourceRootConfig(domain="dsp-vanilla", path="D:/project/dsp/DSPCore/DSPCore"),
        SourceRootConfig(
            domain="dsp-vanilla",
            path="D:/project/dsp/MLJ_DSPmods/gamedata/DecompiledSource/Assembly-CSharp",
        ),
        SourceRootConfig(domain="fractionate-everything", path="D:/project/dsp/MLJ_DSPmods/FractionateEverything/src"),
        SourceRootConfig(domain="fractionate-everything", path="D:/project/dsp/MLJ_DSPmods/FractionateEverything/README.md"),
        SourceRootConfig(domain="dsp-mod-tools", path="D:/project/dsp/MLJ_DSPmods/SaveDataExporter"),
        SourceRootConfig(domain="dsp-mod-tools", path="D:/project/dsp/MLJ_DSPmods/UXAEnhance"),
        SourceRootConfig(domain="dsp-mod-tools", path="D:/project/dsp/MLJ_DSPmods/AfterBuildEvent"),
        SourceRootConfig(domain="dsp-mod-tools", path="D:/project/dsp/MLJ_DSPmods/GetDspData"),
        SourceRootConfig(domain="dsp-mod-tools", path="D:/project/dsp/MLJ_DSPmods/VanillaCurveSim"),
        SourceRootConfig(domain="orbital-ring", path="D:/project/dsp/OrbitalRing-MOD"),
        SourceRootConfig(domain="orbital-ring", path="D:/project/dsp/MLJ_DSPmods/gamedata/DecompiledSource/ProjectOrbitalRing"),
        SourceRootConfig(domain="project-genesis", path="D:/project/dsp/ProjectGenesis"),
        SourceRootConfig(domain="project-genesis", path="D:/project/dsp/MLJ_DSPmods/gamedata/DecompiledSource/ProjectGenesis"),
        SourceRootConfig(domain="shapez", path="D:/project/shapez/DecompiledSource/Game.Content"),
        SourceRootConfig(domain="shapez", path="D:/project/shapez/shapez-mods/src"),
        SourceRootConfig(domain="shapez", path="D:/project/shapez/shapezPathAnalyzer/shapezAnalyzer"),
        SourceRootConfig(domain="factorio", path="D:/project/factorio/MLJ_Factorio_Mods"),
    ]


class SourcesSection(PluginConfigBase):
    __ui_label__ = "源码根"
    __ui_order__ = 1

    roots: list[SourceRootConfig] = Field(
        default_factory=_default_roots,
        max_length=64,
        description="按域配置的只读源码或文档根",
    )


class SearchSection(PluginConfigBase):
    __ui_label__ = "检索预算"
    __ui_order__ = 2

    max_results: int = Field(default=4, ge=1, le=8, description="单次最多证据条数")
    max_chars: int = Field(default=2600, ge=400, le=6000, description="单次证据正文最大字符数")
    max_files_per_domain: int = Field(
        default=80,
        ge=1,
        le=500,
        description="每个根的候选上限及每次查询的单域读取上限",
    )
    max_file_bytes: int = Field(default=220_000, ge=1024, le=1_000_000, description="单文件读取上限")
    max_query_chars: int = Field(default=2000, ge=100, le=4000, description="最后 user 查询读取上限")
    timeout_seconds: float = Field(default=3.0, ge=0.1, le=3.0, description="单次本地检索硬时限")
    refresh_seconds: int = Field(default=600, ge=30, le=86_400, description="源路径索引刷新秒数")
    cache_ttl_seconds: int = Field(default=600, ge=1, le=3600, description="相同检索结果复用秒数")
    cache_max_entries: int = Field(default=128, ge=1, le=1024, description="检索结果缓存最大条数")


class ScopeSection(PluginConfigBase):
    __ui_label__ = "会话范围"
    __ui_order__ = 3

    enabled_group_ids: list[str] = Field(
        default_factory=list,
        description="空列表不门控；非空时只允许结构化消息确认的群",
    )
    session_ttl_seconds: int = Field(default=600, ge=30, le=3600, description="session 到群号事实映射有效期")
    session_max_entries: int = Field(default=1024, ge=1, le=10_000, description="会话事实映射最大条数")


class KnowledgeConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    sources: SourcesSection = Field(default_factory=SourcesSection)
    search: SearchSection = Field(default_factory=SearchSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)


@dataclass(slots=True)
class _ScopeFact:
    group_id: str
    expires_at: float


class _SessionScopeMap:
    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._facts: dict[str, _ScopeFact] = {}

    def remember(self, session_id: str, group_id: str) -> None:
        if not session_id or len(session_id) > 512 or not group_id:
            return
        now = time.monotonic()
        self._prune(now)
        self._facts[session_id] = _ScopeFact(group_id, now + self.ttl_seconds)
        overflow = len(self._facts) - self.max_entries
        if overflow > 0:
            oldest = sorted(self._facts, key=lambda key: self._facts[key].expires_at)[:overflow]
            for key in oldest:
                self._facts.pop(key, None)

    def resolve(self, session_id: str) -> str:
        now = time.monotonic()
        fact = self._facts.get(session_id)
        if fact is None:
            return ""
        if fact.expires_at <= now:
            self._facts.pop(session_id, None)
            return ""
        return fact.group_id

    def clear(self) -> None:
        self._facts.clear()

    def _prune(self, now: float) -> None:
        for key in [key for key, fact in self._facts.items() if fact.expires_at <= now]:
            self._facts.pop(key, None)


class QQBotKnowledgePlugin(MaiBotPlugin):
    """Inject bounded local source evidence into one replyer request."""

    config_model: ClassVar[type[PluginConfigBase] | None] = KnowledgeConfig

    def __init__(self) -> None:
        super().__init__()
        self._index: SourceIndex | None = None
        self._scope_map: _SessionScopeMap | None = None

    async def on_load(self) -> None:
        self._rebuild_runtime()
        self.ctx.logger.info(
            "QQBot 源码知识插件已加载: domains=%s roots=%s",
            len(self.index.available_domains),
            len(self.config.sources.roots),
        )

    async def on_unload(self) -> None:
        if self._index is not None:
            self._index.clear()
        if self._scope_map is not None:
            self._scope_map.clear()
        self._index = None
        self._scope_map = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self._rebuild_runtime()
        self.ctx.logger.info("QQBot 源码知识配置已更新: version=%s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_knowledge_session_scope",
        description="从结构化接收消息建立有界 session 到群号事实映射",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_session_scope(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        del kwargs
        session_id, group_id = _structured_session_scope(message)
        if session_id and group_id:
            self.scope_map.remember(session_id, group_id)
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="qqbot_knowledge_injection",
        description="按最后 user 查询向当前模型请求临时注入本地源码证据",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=4000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_source_knowledge(
        self,
        messages: object = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, object]:
        if not self.config.plugin.enabled:
            return {"action": "continue"}

        started_at = time.monotonic()
        group_id = self.scope_map.resolve(str(session_id or ""))
        enabled_groups = {str(group).strip() for group in self.config.scope.enabled_group_ids if str(group).strip()}
        if enabled_groups and (not group_id or group_id not in enabled_groups):
            return {"action": "continue"}

        query = extract_last_user_query(messages, max_chars=self.config.search.max_query_chars)
        if not query:
            return {"action": "continue"}
        domains = resolve_domains(query, group_id, self.index.available_domains)
        if not domains:
            return {"action": "continue"}

        try:
            outcome = await asyncio.to_thread(self.index.search, query, domains)
        except Exception as exc:
            self.ctx.logger.warning("QQBot 源码知识检索失败: error_type=%s", type(exc).__name__)
            return {"action": "continue"}
        if not outcome.evidence:
            self.ctx.logger.info(
                "QQBot 源码知识无结果: domains=%s elapsed_ms=%s error_type=%s",
                ",".join(outcome.domains),
                int((time.monotonic() - started_at) * 1000),
                "SearchTimeout" if outcome.timed_out else "None",
            )
            return {"action": "continue"}

        modified_messages = inject_evidence(messages, outcome.evidence)
        if modified_messages is None:
            return {"action": "continue"}
        modified_kwargs = dict(kwargs)
        modified_kwargs["session_id"] = session_id
        modified_kwargs["messages"] = modified_messages
        self.ctx.logger.info(
            "QQBot 源码知识已注入: domains=%s results=%s chars=%s elapsed_ms=%s error_type=%s",
            ",".join(outcome.domains),
            outcome.result_count,
            outcome.char_count,
            int((time.monotonic() - started_at) * 1000),
            "SearchTimeout" if outcome.timed_out else "None",
        )
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @property
    def index(self) -> SourceIndex:
        if self._index is None:
            self._rebuild_runtime()
        assert self._index is not None
        return self._index

    @property
    def scope_map(self) -> _SessionScopeMap:
        if self._scope_map is None:
            self._rebuild_runtime()
        assert self._scope_map is not None
        return self._scope_map

    def _rebuild_runtime(self) -> None:
        search = self.config.search
        roots = tuple(
            RootSpec(domain=str(root.domain).strip().casefold(), configured_path=str(root.path).strip())
            for root in self.config.sources.roots
            if str(root.domain).strip() and str(root.path).strip()
        )
        if self._index is not None:
            self._index.clear()
        if self._scope_map is not None:
            self._scope_map.clear()
        self._index = SourceIndex(
            roots,
            SearchLimits(
                max_results=search.max_results,
                max_chars=search.max_chars,
                max_files_per_domain=search.max_files_per_domain,
                max_file_bytes=search.max_file_bytes,
                refresh_seconds=search.refresh_seconds,
                search_timeout_seconds=search.timeout_seconds,
                cache_ttl_seconds=search.cache_ttl_seconds,
                cache_max_entries=search.cache_max_entries,
            ),
        )
        self._scope_map = _SessionScopeMap(
            ttl_seconds=self.config.scope.session_ttl_seconds,
            max_entries=self.config.scope.session_max_entries,
        )


def _structured_session_scope(message: object) -> tuple[str, str]:
    if not isinstance(message, Mapping):
        return "", ""
    session_id = str(message.get("session_id") or "").strip()
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return "", ""
    group_info = message_info.get("group_info")
    if not isinstance(group_info, Mapping):
        return "", ""
    group_id = str(group_info.get("group_id") or "").strip()
    return session_id, group_id


def create_plugin() -> QQBotKnowledgePlugin:
    return QQBotKnowledgePlugin()
