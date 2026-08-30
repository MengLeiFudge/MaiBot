from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用动态引用控制")
    config_version: str = Field(default="0.1.0", description="配置版本")


class QuoteSection(PluginConfigBase):
    __ui_label__ = "引用距离"
    __ui_order__ = 1

    max_nearby_messages: int = Field(
        default=5,
        ge=0,
        le=100,
        description="目标消息之后不超过此数量的可见消息时取消引用",
    )


class DynamicQuoteConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    quote: QuoteSection = Field(default_factory=QuoteSection)


class QQBotDynamicQuotePlugin(MaiBotPlugin):
    """Suppress nearby reply quotes immediately before Platform IO sends."""

    config_model: ClassVar[type[PluginConfigBase] | None] = DynamicQuoteConfig

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "QQBot 动态引用插件已加载，启用=%s，近距离阈值=%s",
            self.config.plugin.enabled,
            self.config.quote.max_nearby_messages,
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info(
                "QQBot 动态引用配置已更新: version=%s enabled=%s max_nearby_messages=%s",
                version,
                self.config.plugin.enabled,
                self.config.quote.max_nearby_messages,
            )

    @HookHandler(
        "send_service.before_send",
        name="qqbot_dynamic_quote_control",
        description="按同会话可见消息距离动态取消近距离引用",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def control_quote(
        self,
        message: object = None,
        set_reply: bool = False,
        reply_message_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        if not self.config.plugin.enabled or not set_reply:
            return {"action": "continue"}

        target_message_id = str(reply_message_id or "").strip()
        session_id = _session_id(message)
        if not target_message_id or not session_id:
            return {"action": "continue"}

        threshold = self.config.quote.max_nearby_messages
        try:
            recent_messages = await self.ctx.db.query(
                "Messages",
                filters={"session_id": session_id, "is_notify": False},
                order_by=["-id"],
                limit=threshold + 1,
            )
        except Exception as exc:
            self.ctx.logger.warning("动态引用距离查询失败: error_type=%s", type(exc).__name__)
            return {"action": "continue"}

        if not isinstance(recent_messages, list):
            self.ctx.logger.warning(
                "动态引用距离查询返回格式无效: result_type=%s",
                type(recent_messages).__name__,
            )
            return {"action": "continue"}

        distance = _intervening_message_count(recent_messages, target_message_id)
        if distance is None or distance > threshold:
            return {"action": "continue"}

        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "message": message,
                "set_reply": False,
                "reply_message_id": "",
            },
        }


def _session_id(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    return str(message.get("session_id") or "").strip()


def _intervening_message_count(recent_messages: list[object], target_message_id: str) -> int | None:
    for distance, message in enumerate(recent_messages):
        if not isinstance(message, Mapping):
            continue
        if str(message.get("message_id") or "").strip() == target_message_id:
            return distance
    return None


def create_plugin() -> QQBotDynamicQuotePlugin:
    return QQBotDynamicQuotePlugin()
