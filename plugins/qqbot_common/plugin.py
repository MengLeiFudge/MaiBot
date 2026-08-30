from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .coordination import CoordinationStore
from .message_facts import message_user_names


class PluginSection(PluginConfigBase):
    """公共插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 QQBot 迁移公共能力")
    config_version: str = Field(default="0.1.2", description="配置版本")


class StorageSection(PluginConfigBase):
    """迁移期共享存储配置。"""

    __ui_label__ = "存储"
    __ui_order__ = 1

    legacy_runtime_root: str = Field(
        default="",
        description="业务数据根；留空时使用当前 MaiBot 插件数据目录",
    )
    coordination_db_name: str = Field(
        default="maibot_coordination.sqlite3",
        description="MaiBot 多实例仲裁数据库文件名",
    )


class RoutingSection(PluginConfigBase):
    """多机器人命令仲裁配置。"""

    __ui_label__ = "路由"
    __ui_order__ = 2

    bot_account_ids: list[str] = Field(default_factory=list, description="参与命令仲裁的机器人 QQ 列表")
    claim_ttl_seconds: int = Field(default=12, ge=2, le=120, description="命令唯一执行声明有效期")
    claim_bucket_seconds: int = Field(default=4, ge=1, le=30, description="无统一消息 ID 时的时间桶长度")


class CommonConfig(PluginConfigBase):
    """公共插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)


class QQBotCommonPlugin(MaiBotPlugin):
    """为拆分后的 QQBot 功能插件提供跨实例公共能力。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = CommonConfig

    async def on_load(self) -> None:
        """初始化协调数据库。"""

        if self.config.plugin.enabled:
            self._store()
            self.ctx.logger.info("QQBot 迁移公共能力已加载，数据根=%s", self._runtime_root())

    async def on_unload(self) -> None:
        """公共插件没有常驻资源需要释放。"""

        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """记录插件自身配置热更新。"""

        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot 迁移公共配置已更新: %s", version)

    @API("qqbot.route.claim", description="为固定命令选择唯一 MaiBot 执行实例", version="1", public=True)
    async def claim_command(
        self,
        feature: str,
        text: str,
        user_id: str,
        group_id: str,
        self_id: str,
        timestamp: float = 0.0,
        at_target_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """返回当前 self_id 是否拥有这次命令执行权。"""

        if not self.config.plugin.enabled:
            return {"claimed": False, "owner_self_id": ""}
        claimed = self._store().claim_command(
            feature=feature,
            text=text,
            user_id=user_id,
            group_id=group_id,
            self_id=self_id,
            timestamp=timestamp,
            at_target_ids=at_target_ids or [],
            bot_account_ids=self.config.routing.bot_account_ids,
            ttl_seconds=self.config.routing.claim_ttl_seconds,
            bucket_seconds=self.config.routing.claim_bucket_seconds,
        )
        return {"claimed": claimed, "owner_self_id": self_id if claimed else ""}

    @API("qqbot.identity.display_name", description="解析群维度用户显示名", version="1", public=True)
    async def resolve_display_name(self, group_id: str, user_id: str) -> dict[str, str]:
        """返回当前群优先的群名片或 QQ 昵称。"""

        return {"display_name": self._store().resolve_display_name(group_id.strip(), user_id.strip())}

    @API("qqbot.storage.runtime_root", description="返回迁移期 QQBot 业务数据根", version="1", public=True)
    async def runtime_root(self) -> dict[str, str]:
        """向依赖插件公开当前业务数据根。"""

        return {"path": str(self._runtime_root())}

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_bot_sender_gate",
        description="阻止已配置机器人账号触发命令、聊天和记忆链",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_bot_sender(
        self,
        message: object = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        """在入站主链最前面丢弃姐妹机器人发送的消息。"""

        del kwargs
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if should_block_configured_bot_sender(message, self.config.routing.bot_account_ids):
            return {"action": "abort"}
        return {"action": "continue"}

    @HookHandler(
        "chat.receive.after_process",
        name="qqbot_group_name_cache",
        description="缓存群消息中的群名片和 QQ 昵称",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def cache_group_name(
        self,
        message: object = None,
        **kwargs: Any,
    ) -> None:
        """异步更新共享群昵称缓存，不干预消息链。"""

        del kwargs
        if not self.config.plugin.enabled:
            return None
        group_id, user_id = _message_scope(message)
        if not group_id or not user_id:
            return None
        nickname, cardname = message_user_names(message)
        self._store().remember_name(
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            cardname=cardname,
            updated_at=0.0,
        )
        return None

    def _runtime_root(self) -> Path:
        root = self.config.storage.legacy_runtime_root.strip()
        return Path(root).expanduser().resolve() if root else self.ctx.paths.data_dir.resolve()

    def _store(self) -> CoordinationStore:
        db_name = Path(self.config.storage.coordination_db_name).name
        if not db_name:
            raise ValueError("coordination_db_name 不能为空")
        return CoordinationStore(self._runtime_root() / db_name)


def should_block_configured_bot_sender(message: object, bot_account_ids: list[str]) -> bool:
    """普通机器人消息需要中止，结构化协议事件留给对应业务 Hook。"""

    if isinstance(message, dict) and bool(message.get("is_notify")):
        return False
    return is_configured_bot_sender(message, bot_account_ids)


def is_configured_bot_sender(message: object, bot_account_ids: list[str]) -> bool:
    """判断消息发送者是否属于当前配置的机器人账号集合。"""

    _, user_id = _message_scope(message)
    bot_ids = {str(item).strip() for item in bot_account_ids if str(item).strip()}
    return bool(user_id) and user_id in bot_ids


def _message_scope(message: object) -> tuple[str, str]:
    if not isinstance(message, dict):
        return "", ""
    message_info = message.get("message_info")
    if not isinstance(message_info, dict):
        return "", ""
    group_info = message_info.get("group_info")
    user_info = message_info.get("user_info")
    group_id = str(group_info.get("group_id") or "").strip() if isinstance(group_info, dict) else ""
    user_id = str(user_info.get("user_id") or "").strip() if isinstance(user_info, dict) else ""
    return group_id, user_id


def create_plugin() -> QQBotCommonPlugin:
    """创建 QQBot 公共能力插件实例。"""

    return QQBotCommonPlugin()
