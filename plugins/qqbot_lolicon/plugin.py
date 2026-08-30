from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import re
import secrets
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .service import LOLICON_API_URL, LoliconClient, LoliconImageItem, LoliconMode
from .service import parse_lolicon_command
from .storage import LoliconGroupConfig, LoliconGroupConfigStore, LoliconMetadataStore


LOLICON_ADMIN_PATTERN = r"^[开关](?:群色图|图片显示)$"
LOLICON_PATTERN = r"^(?:来点)?(?:[美色涩蛇]图|混合).*$"
_IMAGE_SUMMARIES = ("给你看看", "这张送到", "图片来了", "找到一张", "新图送到")
_R18_BLOCKED = "本群当前设置为群内只能查看非R18图片！\n请私聊发送指令QwQ"
_NO_RESULT = "没有找到符合你要求的图片呢QAQ\n尝试减少一些tag吧！"
_FETCH_FAILED = "Lolicon 美图获取失败，请稍后重试。"
_IMAGE_SEND_FAILED = "Lolicon 图片发送失败，请稍后重试。"


class PluginSection(PluginConfigBase):
    """Lolicon plugin registration settings."""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 Lolicon 固定命令")
    config_version: str = Field(default="0.1.0", description="配置版本")


class PermissionsSection(PluginConfigBase):
    """Owner-only group configuration settings."""

    __ui_label__ = "权限"
    __ui_order__ = 1

    owner_qq: str = Field(default="605738729", description="允许修改群 Lolicon 配置的主人 QQ")


class ApiSection(PluginConfigBase):
    """Public Lolicon API settings."""

    __ui_label__ = "Lolicon API"
    __ui_order__ = 2

    endpoint: str = Field(default=LOLICON_API_URL, description="Lolicon API v2 地址")
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0, description="API 请求超时秒数")


class StorageSection(PluginConfigBase):
    """Legacy-compatible runtime storage settings."""

    __ui_label__ = "存储"
    __ui_order__ = 3

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共插件的数据根")


class LoliconConfig(PluginConfigBase):
    """Complete Lolicon plugin configuration."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    permissions: PermissionsSection = Field(default_factory=PermissionsSection)
    api: ApiSection = Field(default_factory=ApiSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotLoliconPlugin(MaiBotPlugin):
    """Serve Lolicon images and owner-controlled group R18 settings."""

    config_model: ClassVar[type[PluginConfigBase] | None] = LoliconConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("QQBot Lolicon 插件已加载，启用=%s", self.config.plugin.enabled)

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot Lolicon 配置已更新: %s", version)

    @Command(
        "qqbot_lolicon_admin",
        description="主人开关当前群 Lolicon R18 和 R18 图片显示",
        pattern=LOLICON_ADMIN_PATTERN,
    )
    async def handle_admin_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        if not self.config.plugin.enabled:
            return True, "", True
        facts = _message_facts(message, group_id, user_id)
        await self._require_claim(command_text, facts)
        if not facts["claimed"]:
            return True, "", True

        if user_id != self.config.permissions.owner_qq:
            response = "只有作者才能调整美图配置哦！"
        elif not group_id:
            response = "这个指令只能在群聊中使用。"
        else:
            store = LoliconGroupConfigStore(await self._runtime_root())
            current = await asyncio.to_thread(store.get, group_id)
            response, updated = _apply_admin_command(command_text, current)
            await asyncio.to_thread(store.set, group_id, updated)
        await self._send_text(group_id, user_id, response, stream_id)
        return True, response, True

    @Command(
        "qqbot_lolicon_image",
        description="获取 Lolicon 非 R18、R18 或混合图片",
        pattern=LOLICON_PATTERN,
        timeout_ms=90_000,
    )
    async def handle_image_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        if not self.config.plugin.enabled:
            return True, "", True
        command = parse_lolicon_command(command_text)
        if command is None:
            return True, "", True

        facts = _message_facts(message, group_id, user_id)
        await self._require_claim(command_text, facts)
        if not facts["claimed"]:
            return True, "", True

        runtime_root = await self._runtime_root()
        show_image = True
        if group_id:
            group_config = await asyncio.to_thread(LoliconGroupConfigStore(runtime_root).get, group_id)
            if command.mode != LoliconMode.NON_R18 and not group_config.group_r18:
                await self._send_text(group_id, user_id, _R18_BLOCKED, stream_id)
                return True, _R18_BLOCKED, True
            show_image = group_config.show_image

        try:
            items = await asyncio.to_thread(self._client().fetch, command)
        except Exception as exc:
            self.ctx.logger.warning("Lolicon API 请求失败: error_type=%s", type(exc).__name__)
            await self._send_text(group_id, user_id, _FETCH_FAILED, stream_id)
            return True, _FETCH_FAILED, True
        if not items:
            await self._send_text(group_id, user_id, _NO_RESULT, stream_id)
            return True, _NO_RESULT, True

        store = LoliconMetadataStore(runtime_root)
        for index, item in enumerate(items, start=1):
            try:
                await asyncio.to_thread(store.upsert, item)
                await self._send_item(group_id, user_id, item, index, len(items), show_image)
            except Exception as exc:
                self.ctx.logger.warning("Lolicon 图片处理失败: error_type=%s", type(exc).__name__)
                await self._send_text(group_id, user_id, _IMAGE_SEND_FAILED, stream_id)
                return True, _IMAGE_SEND_FAILED, True
        return True, f"Lolicon 图片 {len(items)} 张", True

    async def _require_claim(self, text: str, facts: dict[str, Any]) -> None:
        if not facts["self_id"]:
            raise ValueError("Lolicon 命令缺少当前机器人 self_id")
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="lolicon",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "Lolicon 命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Lolicon 命令仲裁返回格式无效")
        facts["claimed"] = bool(payload.get("claimed"))

    async def _runtime_root(self) -> Path:
        override = self.config.storage.runtime_root_override.strip()
        if override:
            return Path(override).expanduser().resolve()
        result = await self.ctx.api.call("qqbot.storage.runtime_root")
        payload = require_api_result(result, "读取 QQBot 业务数据根")
        path = str(payload.get("path") or "").strip() if isinstance(payload, Mapping) else ""
        if not path:
            raise RuntimeError("QQBot 公共插件没有返回业务数据根")
        return Path(path).resolve()

    def _client(self) -> LoliconClient:
        return LoliconClient(
            endpoint=self.config.api.endpoint.strip() or LOLICON_API_URL,
            timeout_seconds=self.config.api.timeout_seconds,
        )

    async def _send_item(
        self,
        group_id: str,
        user_id: str,
        item: LoliconImageItem,
        index: int,
        total: int,
        show_image: bool,
    ) -> None:
        prefix = f"图片索引：{index} / {total}\n"
        suffix = (
            f"\n{item.title}(PID {item.pid})\nby {item.author}(UID {item.uid})"
            f"\nTags: {', '.join(item.tags) if item.tags else '-'}"
        )
        should_send_image = show_image or not item.r18
        segments: list[dict[str, object]] = [{"type": "text", "data": {"text": prefix}}]
        if should_send_image:
            segments.append(
                {
                    "type": "image",
                    "data": {"file": item.url, "summary": secrets.choice(_IMAGE_SUMMARIES)},
                }
            )
        else:
            segments.append({"type": "text", "data": {"text": item.url}})
        segments.append({"type": "text", "data": {"text": suffix}})
        await self._send_segments(group_id, user_id, segments, "发送 Lolicon 图片")

    async def _send_text(self, group_id: str, user_id: str, text: str, stream_id: str) -> None:
        try:
            await self._send_segments(
                group_id,
                user_id,
                [{"type": "text", "data": {"text": text}}],
                "发送 Lolicon 文本",
            )
        except Exception:
            if not stream_id:
                raise
            await self.ctx.send.text(text, stream_id)

    async def _send_segments(
        self,
        group_id: str,
        user_id: str,
        segments: list[dict[str, object]],
        label: str,
    ) -> None:
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": segments}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": segments}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, label)


def _apply_admin_command(
    text: str,
    current: LoliconGroupConfig,
) -> tuple[str, LoliconGroupConfig]:
    if text == "开群色图":
        return "已开启群色图！", LoliconGroupConfig(True, current.show_image)
    if text == "关群色图":
        return "已关闭群色图！", LoliconGroupConfig(False, current.show_image)
    if text == "开图片显示":
        return (
            "已开启图片显示！\n注意，开启此功能极有可能导致无法接收到消息！\n即使开启，r18图片也不会有缩略图显示~",
            LoliconGroupConfig(current.group_r18, True),
        )
    if text == "关图片显示":
        return "已关闭图片显示！", LoliconGroupConfig(current.group_r18, False)
    return "未知美图配置指令。", current


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    message_dict = message if isinstance(message, Mapping) else {}
    message_info = message_dict.get("message_info")
    message_info_dict = message_info if isinstance(message_info, Mapping) else {}
    additional = message_info_dict.get("additional_config")
    additional_dict = additional if isinstance(additional, Mapping) else {}
    raw_message = message_dict.get("raw_message")
    targets: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target and target not in targets:
                targets.append(target)
    try:
        timestamp = float(message_dict.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(additional_dict.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": targets,
        "claimed": False,
    }


def _text_segments(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return ""
    parts: list[str] = []
    for segment in raw_message:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        parts.append(str(data.get("text") or data.get("content") or "") if isinstance(data, Mapping) else str(data or ""))
    return "".join(parts).strip()


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def create_plugin() -> QQBotLoliconPlugin:
    return QQBotLoliconPlugin()
