from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, ClassVar, cast

import asyncio

from maibot_sdk import ON_BOT_CONFIG_RELOAD, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


class PluginSection(PluginConfigBase):
    """入站图片门控的启用与配置版本。"""

    enabled: bool = Field(default=True, description="是否只让定向消息进入原生识图链")
    config_version: str = Field(default="0.1.0", description="配置版本")


class VisualConfig(PluginConfigBase):
    """本人身份和称呼复用 bot 配置，不复制到插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


def _mask_media(segments: list[Any]) -> bool:
    """递归屏蔽非定向媒体，保留已有描述与原组件的 hash。"""
    changed = False
    for value in segments:
        if not isinstance(value, dict):
            continue
        part = cast(dict[str, Any], value)
        kind = part.get("type")
        if kind in {"image", "emoji"}:
            content = str(part.get("data") or "").strip()
            if not content or content in {"[图片，识别中.....]", "[image]"}:
                part["data"] = "[图片]" if kind == "image" else "[表情包]"
                changed = True
            if "binary_data_base64" in part:
                del part["binary_data_base64"]
                changed = True
        elif kind == "forward" and isinstance(part.get("data"), list):
            for node in part["data"]:
                if isinstance(node, dict) and isinstance(node.get("content"), list):
                    changed = _mask_media(node["content"]) or changed
    return changed


class QQBotVisualPlugin(MaiBotPlugin):
    """在公开接收 Hook 中门控入站图片，不接管框架内部后台识图。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = VisualConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        """只保存当前 bot 配置的本人账号和称呼，不缓存消息或图像。"""
        super().__init__()
        self._bot_id = ""
        self._names: tuple[str, ...] = ()

    async def _reload_bot_identity(self) -> None:
        """通过公开 config.get 读取本人身份，完整校验后一起替换。"""
        async with asyncio.timeout(3):
            platform, bot_id, nickname, aliases = await asyncio.gather(
                self.ctx.config.get("bot.platform"),
                self.ctx.config.get("bot.qq_account"),
                self.ctx.config.get("bot.nickname"),
                self.ctx.config.get("bot.alias_names"),
            )
        if platform != "qq" or not isinstance(bot_id, str) or not bot_id.isascii() or not bot_id.isdecimal():
            raise ValueError("定向看图插件需要 bot.platform=qq 和真实 bot.qq_account")
        if not isinstance(nickname, str) or not isinstance(aliases, list) or not all(isinstance(name, str) for name in aliases):
            raise ValueError("bot.nickname 和 bot.alias_names 配置类型不合法")
        self._bot_id = bot_id
        self._names = tuple(dict.fromkeys(name.strip() for name in [nickname, *aliases] if name.strip()))

    async def on_load(self) -> None:
        """读取原生身份配置，加载阶段不识图。"""
        await self._reload_bot_identity()
        self.ctx.logger.info("QQBot 定向看图已加载，昵称和别名来自 bot 配置；引用图片仅复用已有描述")

    async def on_unload(self) -> None:
        """清空配置副本；无后台任务或媒体缓存需要释放。"""
        self._bot_id = ""
        self._names = ()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """跟随 bot 配置更新本人账号和称呼。"""
        del config_data
        if scope == ON_BOT_CONFIG_RELOAD:
            await self._reload_bot_identity()
            self.ctx.logger.info("QQBot 定向看图身份已更新: version=%s", version)

    def _is_directed(self, message: Mapping[str, Any]) -> bool:
        """只读取当前顶层正文及结构化 @/回复，不用转发内容触发识图。"""
        info = message.get("message_info")
        if not isinstance(info, Mapping):
            raise ValueError("接收消息缺少 message_info")
        sender = info.get("user_info")
        if not isinstance(sender, Mapping) or not sender.get("user_id"):
            raise ValueError("接收消息缺少真实发送者")
        if sender["user_id"] == self._bot_id:
            return False
        if info.get("group_info") is None:
            return True
        additional = info.get("additional_config")
        if message.get("is_at") is True or isinstance(additional, Mapping) and additional.get("at_bot") is True:
            return True
        for part in message["raw_message"]:
            if not isinstance(part, Mapping):
                continue
            data = part.get("data")
            if part.get("type") == "text" and isinstance(data, str) and any(name in data for name in self._names):
                return True
            if isinstance(data, Mapping):
                if part.get("type") == "at" and data.get("target_user_id") == self._bot_id:
                    return True
                if part.get("type") == "reply" and data.get("target_message_sender_id") == self._bot_id:
                    return True
        return False

    async def _reuse_quoted_descriptions(self, message: dict[str, Any]) -> bool:
        """把同会话引用图片的已有描述补入回复组件，不发起新的识图。"""
        session_id = message.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return False
        changed = False
        for part in message["raw_message"]:
            if not isinstance(part, dict) or part.get("type") != "reply" or not isinstance(part.get("data"), dict):
                continue
            data = part["data"]
            target_id = data.get("target_message_id")
            if not isinstance(target_id, str) or not target_id:
                continue
            quoted = await self.ctx.message.get_by_id(target_id, chat_id=session_id, include_binary_data=False)
            if (
                not isinstance(quoted, Mapping)
                or quoted.get("session_id") != session_id
                or quoted.get("message_id") != target_id
                or quoted.get("platform") != message.get("platform")
            ):
                continue
            segments = quoted.get("raw_message")
            if not isinstance(segments, list):
                continue
            descriptions = [
                str(segment["data"]).strip()
                for segment in segments
                if isinstance(segment, Mapping)
                and segment.get("type") in {"image", "emoji"}
                and isinstance(segment.get("data"), str)
                and str(segment["data"]).strip()
                not in {"", "[图片]", "[image]", "[图片，识别中.....]", "[表情包]"}
            ]
            content = str(data.get("target_message_content") or quoted.get("processed_plain_text") or "")
            updated = False
            for description in descriptions:
                if description not in content:
                    content = f"{content}\n[引用图片已有描述] {description}".strip()
                    updated = True
            if updated:
                data["target_message_content"] = content
                changed = True
        return changed

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_visual_receive_gate",
        description="非定向媒体使用中性占位；定向附件放行，引用图片只复用已有描述",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def gate_visuals(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """只改返回的消息副本，保留原发送者、路由、时间和其他组件。"""
        if not cast(VisualConfig, self.config).plugin.enabled:
            return {"action": "continue"}
        if not isinstance(message, dict) or message.get("platform") != "qq" or message.get("is_notify") is True:
            return {"action": "continue"}
        current = cast(dict[str, Any], message)
        if not isinstance(current.get("raw_message"), list):
            raise ValueError("接收消息的 raw_message 必须为列表")
        modified = deepcopy(current)
        if self._is_directed(current):
            try:
                async with asyncio.timeout(3):
                    changed = await self._reuse_quoted_descriptions(modified)
            except Exception as exc:
                self.ctx.logger.warning("引用图片描述查询失败，保持原引用: error_type=%s", type(exc).__name__)
                return {"action": "continue"}
        else:
            changed = _mask_media(modified["raw_message"])
        if not changed:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": modified}}


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 管理生命周期的定向看图插件。"""
    return QQBotVisualPlugin()
