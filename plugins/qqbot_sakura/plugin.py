from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import re

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .process_lock import InterProcessLock
from .service import SakuraService


SAKURA_COMMAND_PATTERN = (
    r"^(?:@\S+\s*)?(?:落樱之都|更新日志|玩法|注册.+|改名.+|个人信息|"
    r"加经验[0-9]+|嘤[0-9]+|恢复|回复|加[0-9]+(?:力量|智力|体质|敏捷|魅力))$"
)


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册落樱之都固定命令")
    config_version: str = Field(default="0.1.0", description="配置版本")


class CutoverSection(PluginConfigBase):
    __ui_label__ = "迁移接管"
    __ui_order__ = 1

    write_enabled: bool = Field(default=True, description="由 MaiBot 接管落樱之都状态写入")


class StorageSection(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_order__ = 2

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共插件的数据根")


class SakuraConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    cutover: CutoverSection = Field(default_factory=CutoverSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotSakuraPlugin(MaiBotPlugin):
    """在普通聊天链前处理落樱之都确定性命令。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = SakuraConfig

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "QQBot 落樱之都插件已加载，业务写入=%s",
            self.config.cutover.write_enabled,
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot 落樱之都配置已更新: %s", version)

    @Command("qqbot_sakura", description="落樱之都完整命令入口", pattern=SAKURA_COMMAND_PATTERN)
    async def handle_sakura_command(
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
            response = "落樱之都功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not self.config.cutover.write_enabled:
            response = "落樱之都当前已暂停使用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            raise ValueError("落樱之都命令缺少当前机器人 self_id")
        if not await self._claim(command_text, facts):
            return True, "", True

        runtime_root = await self._runtime_root()
        response = await asyncio.to_thread(
            self._execute_locked,
            runtime_root,
            command_text,
            int(user_id),
        )
        if response is None:
            response = "落樱之都命令参数无效。"
        await self.ctx.send.text(response, stream_id)
        return True, response, True

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="sakura",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "落樱之都命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("落樱之都命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

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

    @staticmethod
    def _execute_locked(runtime_root: Path, text: str, user_id: int) -> str | None:
        with InterProcessLock(runtime_root / ".maibot_locks" / "sakura.lock"):
            service = SakuraService(runtime_root / "db" / "sakura" / "players.json")
            return service.handle_command(text, user_id)


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
        timestamp = float(message_dict.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
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


def create_plugin() -> QQBotSakuraPlugin:
    return QQBotSakuraPlugin()
