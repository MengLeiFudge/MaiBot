from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import asyncio

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .command_filter import looks_like_command
from .state import REREAD_COOLDOWN_SECONDS, REREAD_DUPLICATE_WINDOW_SECONDS, RereadRepeatState


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用群聊概率复读")
    config_version: str = Field(default="0.1.0", description="配置版本")


class StateSection(PluginConfigBase):
    __ui_label__ = "复读状态"
    __ui_order__ = 1

    cooldown_seconds: float = Field(
        default=REREAD_COOLDOWN_SECONDS,
        ge=0.0,
        le=3600.0,
        description="同一文本成功复读后的冷却秒数",
    )
    duplicate_window_seconds: float = Field(
        default=REREAD_DUPLICATE_WINDOW_SECONDS,
        ge=0.0,
        le=60.0,
        description="同一发送者与文本的多实例重复事件去重窗口",
    )


class RoutingSection(PluginConfigBase):
    __ui_label__ = "路由"
    __ui_order__ = 2

    bot_account_ids: list[str] = Field(default_factory=list, description="不会触发复读的机器人 QQ")


class RereadConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    state: StateSection = Field(default_factory=StateSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)


class QQBotRereadPlugin(MaiBotPlugin):
    """Repeat consecutive human group text before it reaches the chat chain."""

    config_model: ClassVar[type[PluginConfigBase] | None] = RereadConfig

    async def on_load(self) -> None:
        self._mutex = asyncio.Lock()
        self._state = self._new_state()
        self.ctx.logger.info("QQBot 自动复读插件已加载，启用=%s", self.config.plugin.enabled)

    async def on_unload(self) -> None:
        self._state = self._new_state()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            async with self._mutex:
                self._state = self._new_state()
            self.ctx.logger.info("QQBot 自动复读配置已更新并清空内存状态: %s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_reread_gate",
        description="群聊连续纯文本概率复读并在触发时中止聊天和记忆链",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=10000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reread(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        del kwargs
        facts = _message_facts(message)
        if not self.config.plugin.enabled or _should_skip(facts, self.config.routing.bot_account_ids):
            return {"action": "continue"}

        async with self._mutex:
            should_repeat = self._state.observe(
                facts["group_id"],
                facts["text"],
                message_id=facts["message_id"],
                sender_id=facts["user_id"],
                event_timestamp=facts["timestamp"],
            )
        if not should_repeat:
            return {"action": "continue"}

        try:
            claimed = await self._claim(facts)
        except Exception as exc:
            self.ctx.logger.warning("自动复读仲裁失败: error_type=%s", type(exc).__name__)
            return {"action": "abort"}
        if not claimed:
            return {"action": "abort"}

        try:
            await self._send_group(facts["group_id"], facts["text"])
        except Exception as exc:
            self.ctx.logger.warning("自动复读发送失败: error_type=%s", type(exc).__name__)
        return {"action": "abort"}

    async def _claim(self, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="reread",
            text=facts["text"],
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=[],
        )
        payload = require_api_result(result, "自动复读仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("自动复读仲裁返回格式无效")
        return bool(payload.get("claimed"))

    async def _send_group(self, group_id: str, text: str) -> None:
        result = await self.ctx.api.call(
            "adapter.napcat.group.send_group_msg",
            params={
                "group_id": group_id,
                "message": [{"type": "text", "data": {"text": text}}],
            },
        )
        require_api_result(result, "发送自动复读")

    def _new_state(self) -> RereadRepeatState:
        return RereadRepeatState(
            cooldown_seconds=self.config.state.cooldown_seconds,
            duplicate_window_seconds=self.config.state.duplicate_window_seconds,
        )


def _message_facts(message: object) -> dict[str, Any]:
    source = message if isinstance(message, Mapping) else {}
    info = source.get("message_info")
    info = info if isinstance(info, Mapping) else {}
    user_info = info.get("user_info")
    user_info = user_info if isinstance(user_info, Mapping) else {}
    group_info = info.get("group_info")
    group_info = group_info if isinstance(group_info, Mapping) else {}
    additional = info.get("additional_config")
    additional = additional if isinstance(additional, Mapping) else {}
    raw_message = source.get("raw_message")
    raw_segments = raw_message if isinstance(raw_message, list) else []
    raw_types = tuple(
        str(segment.get("type") or "").strip().lower()
        for segment in raw_segments
        if isinstance(segment, Mapping)
    )
    source_types_raw = additional.get("napcat_segment_types")
    source_types = (
        tuple(str(item or "").strip().lower() for item in source_types_raw)
        if isinstance(source_types_raw, (list, tuple))
        else ()
    )
    try:
        timestamp = float(source.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
    return {
        "text": str(source.get("processed_plain_text") or _text_segments(raw_segments)).strip(),
        "user_id": str(user_info.get("user_id") or "").strip(),
        "group_id": str(group_info.get("group_id") or "").strip(),
        "self_id": str(additional.get("self_id") or "").strip(),
        "message_id": str(source.get("message_id") or "").strip(),
        "timestamp": timestamp,
        "raw_types": raw_types,
        "source_types": source_types,
        "is_notify": bool(source.get("is_notify")),
        "is_at": bool(source.get("is_at") or source.get("is_mentioned")),
        "is_command": bool(source.get("is_command")),
    }


def _should_skip(facts: Mapping[str, Any], bot_account_ids: list[str]) -> bool:
    text = str(facts.get("text") or "")
    bot_ids = {str(item).strip() for item in bot_account_ids if str(item).strip()}
    if not text or not facts.get("group_id") or not facts.get("self_id"):
        return True
    if str(facts.get("user_id") or "") in bot_ids:
        return True
    if facts.get("is_notify") or facts.get("is_at") or facts.get("is_command"):
        return True
    raw_types = tuple(facts.get("raw_types") or ())
    source_types = tuple(facts.get("source_types") or ())
    if not raw_types or any(item != "text" for item in raw_types):
        return True
    if source_types and any(item != "text" for item in source_types):
        return True
    return looks_like_command(text)


def _text_segments(raw_segments: list[object]) -> str:
    parts: list[str] = []
    for segment in raw_segments:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        value = data.get("text") if isinstance(data, Mapping) else data
        if value is not None:
            parts.append(str(value))
    return "".join(parts)


def create_plugin() -> QQBotRereadPlugin:
    return QQBotRereadPlugin()
