from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import asyncio
import re
import time

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .factorio_http import FactorioDownloadError
from .factorio_http import fetch_factorio_space_age_windows_link


FACTORIO_DOWNLOAD_PATTERN = (
    r"(?i)^.*(?:factorio|异星|太空时代|space\s*age|spaceage).*(?:下载|安装包).*(?:链接|地址)?$"
)
_NOT_CONFIGURED_TEXT = "Factorio 下载凭据尚未配置，请在插件配置中填写 username 和 token。"


class PluginSection(PluginConfigBase):
    """Factorio command registration settings."""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 Factorio Space Age 下载链接固定命令")
    config_version: str = Field(default="0.1.0", description="配置版本")


class FactorioSection(PluginConfigBase):
    """Factorio.com account credentials and HTTP settings."""

    __ui_label__ = "Factorio 官网凭据"
    __ui_order__ = 1

    username: str = Field(default="", description="Factorio.com 用户名")
    token: str = Field(
        default="",
        description="Factorio.com 下载 Token",
        json_schema_extra={"x-widget": "password", "x-icon": "key"},
    )
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0, description="Factorio 官网请求超时秒数")


class FactorioConfig(PluginConfigBase):
    """Complete Factorio download plugin configuration."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    factorio: FactorioSection = Field(default_factory=FactorioSection)


class QQBotFactorioPlugin(MaiBotPlugin):
    """Resolve the current Space Age Windows installer before the chat pipeline."""

    config_model: ClassVar[type[PluginConfigBase] | None] = FactorioConfig

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "QQBot Factorio 插件已加载: enabled=%s, configured=%s",
            self.config.plugin.enabled,
            self._is_configured(),
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data
        self.ctx.logger.info("QQBot Factorio 配置已更新: %s", version)

    @Command(
        "qqbot_factorio_download",
        description="获取 Factorio Space Age Windows 安装包下载链接",
        pattern=FACTORIO_DOWNLOAD_PATTERN,
        timeout_ms=130000,
    )
    async def handle_factorio_download(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Claim and answer one Factorio download command without entering Planner."""

        del stream_id, kwargs
        started_at = time.monotonic()
        command_text = _text_segments(message) or _strip_leading_mention(text)
        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            response = "Factorio 下载命令暂时无法确认当前机器人身份。"
            await self._send(group_id, user_id, response)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts)
        except Exception:
            response = "Factorio 下载命令仲裁失败，请稍后重试。"
            await self._send(group_id, user_id, response)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "Factorio 下载功能当前未启用。"
            await self._send(group_id, user_id, response)
            return True, response, True

        if not self._is_configured():
            await self._send(group_id, user_id, _NOT_CONFIGURED_TEXT)
            self._log_result("not_configured", group_id, started_at)
            return True, _NOT_CONFIGURED_TEXT, True

        try:
            link = await asyncio.to_thread(
                fetch_factorio_space_age_windows_link,
                self.config.factorio.username,
                self.config.factorio.token,
                timeout_seconds=self.config.factorio.timeout_seconds,
            )
        except FactorioDownloadError as exc:
            response = f"Factorio: 没获取到 Space Age Windows 下载链接：{exc}"
            await self._send(group_id, user_id, response)
            self._log_result(exc.code.value, group_id, started_at)
            return True, response, True
        except Exception:
            response = "Factorio: 没获取到 Space Age Windows 下载链接：请求处理失败"
            await self._send(group_id, user_id, response)
            self._log_result("unexpected_error", group_id, started_at)
            return True, response, True

        response = f"Factorio: Space Age Windows {link.version} 下载链接：\n{link.url}"
        await self._send(group_id, user_id, response)
        self._log_result("success", group_id, started_at)
        return True, response, True

    def _is_configured(self) -> bool:
        return bool(self.config.factorio.username.strip() and self.config.factorio.token.strip())

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="factorio_download",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "Factorio 下载命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Factorio 下载命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

    async def _send(self, group_id: str, user_id: str, text: str) -> None:
        message = [{"type": "text", "data": {"text": text}}]
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": message}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": message}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送 Factorio 下载链接")

    def _log_result(self, status: str, group_id: str, started_at: float) -> None:
        self.ctx.logger.info(
            "Factorio 下载命令已消费: chat_type=%s, status=%s, duration_ms=%.1f",
            "group" if group_id else "private",
            status,
            (time.monotonic() - started_at) * 1000,
        )


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    message_dict = message if isinstance(message, Mapping) else {}
    message_info = message_dict.get("message_info")
    message_info_dict = message_info if isinstance(message_info, Mapping) else {}
    additional = message_info_dict.get("additional_config")
    additional_dict = additional if isinstance(additional, Mapping) else {}
    raw_message = message_dict.get("raw_message")
    at_target_ids: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target_id = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target_id and target_id not in at_target_ids:
                at_target_ids.append(target_id)
    try:
        timestamp = float(message_dict.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(additional_dict.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": at_target_ids,
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
        if isinstance(data, Mapping):
            parts.append(str(data.get("text") or data.get("content") or ""))
        else:
            parts.append(str(data or ""))
    return "".join(parts).strip()


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def create_plugin() -> QQBotFactorioPlugin:
    """Create the QQBot Factorio plugin."""

    return QQBotFactorioPlugin()
