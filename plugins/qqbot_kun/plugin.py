from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import re
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .process_lock import InterProcessLock
from .service import KunService


KUN_COMMAND_PATTERN = (
    r"^(?:@\S+\s*)?(?:"
    r"[养摸抓捕][鲲鱼]|属性|洗练.+\d+|挑战|(?:查看)?[Bb]oss(?:属性)?|"
    r"等级排行(?:榜)?|(?:财富|萌泪币|金钱)排行(?:榜)?|道具|背包|命名.*|商城|"
    r"(?:购买|买|出售|卖)(?:改名卡|洗练卡|挑战券|查看卡)\d*|签到|"
    r"设置重置时间\s*\d+|[开关]新赛季提示|(?:更改|修改)(?:萌泪币|等级)\d+|"
    r"赠送全部\s*\d+|(?:查看|进击)\s*@.+|赠送\s*@.+\d+"
    r")$"
)


class PluginSection(PluginConfigBase):
    """养鲲插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册养鲲命令插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class CutoverSection(PluginConfigBase):
    """养鲲单写入接管门禁。"""

    __ui_label__ = "迁移接管"
    __ui_order__ = 1

    write_enabled: bool = Field(default=True, description="AstrBot 已停用，默认由 MaiBot 接管养鲲写入")


class StorageSection(PluginConfigBase):
    """养鲲存储配置。"""

    __ui_label__ = "存储"
    __ui_order__ = 2

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共插件的数据根")


class PermissionSection(PluginConfigBase):
    """养鲲管理权限配置。"""

    __ui_label__ = "权限"
    __ui_order__ = 3

    owner_qq: str = Field(default="605738729", description="允许使用养鲲管理命令的 QQ")


class KunConfig(PluginConfigBase):
    """养鲲插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    cutover: CutoverSection = Field(default_factory=CutoverSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    permissions: PermissionSection = Field(default_factory=PermissionSection)


class QQBotKunPlugin(MaiBotPlugin):
    """使用现有养鲲存档的 MaiBot 原生命令插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = KunConfig

    async def on_load(self) -> None:
        """记录插件接管状态。"""

        self.ctx.logger.info(
            "QQBot 养鲲插件已加载，业务写入=%s",
            self.config.cutover.write_enabled,
        )

    async def on_unload(self) -> None:
        """养鲲插件不持有常驻资源。"""

        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """记录插件配置热更新。"""

        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot 养鲲配置已更新: %s", version)

    @Command("qqbot_kun", description="养鲲完整命令入口", pattern=KUN_COMMAND_PATTERN)
    async def handle_kun_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """仲裁并执行一次确定性养鲲命令。"""

        del kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        facts = _message_facts(message, command_text, group_id, user_id)
        if not facts["self_id"]:
            response = "养鲲命令暂时无法确认当前机器人身份。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts)
        except Exception:
            response = "养鲲命令仲裁失败，请稍后重试。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "养鲲功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not self.config.cutover.write_enabled:
            response = "养鲲功能接管当前已关闭。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        runtime_root = await self._runtime_root()
        target_ids = [item for item in facts["at_target_ids"] if item != facts["self_id"]]
        now_millis = int((facts["timestamp"] or time.time()) * 1000)
        response, ranked_user_ids = await asyncio.to_thread(
            self._execute_locked,
            runtime_root,
            command_text,
            int(user_id),
            now_millis,
            bool(group_id),
            int(target_ids[0]) if target_ids else None,
            user_id == self.config.permissions.owner_qq,
            int(group_id or 0),
        )
        if ranked_user_ids and response is not None:
            display_names = await self._resolve_display_names(group_id, ranked_user_ids)
            for ranked_user_id, display_name in display_names.items():
                response = response.replace(_display_name_token(ranked_user_id), display_name)
        if response is None:
            return True, "养鲲命令在当前会话不可用", True
        await self.ctx.send.text(response, stream_id)
        return True, response, True

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="kun",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "养鲲命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("养鲲命令仲裁返回格式无效")
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

    async def _resolve_display_names(self, group_id: str, user_ids: list[int]) -> dict[int, str]:
        async def resolve(user_id: int) -> tuple[int, str]:
            fallback = str(user_id)
            try:
                result = await self.ctx.api.call(
                    "qqbot.identity.display_name",
                    group_id=group_id,
                    user_id=fallback,
                )
                payload = require_api_result(result, "解析养鲲排行显示名")
                display_name = str(payload.get("display_name") or "").strip() if isinstance(payload, Mapping) else ""
                return user_id, display_name or fallback
            except Exception:
                return user_id, fallback

        return dict(await asyncio.gather(*(resolve(user_id) for user_id in user_ids)))

    @staticmethod
    def _execute_locked(
        runtime_root: Path,
        text: str,
        user_id: int,
        now_millis: int,
        is_group: bool,
        at_id: int | None,
        is_admin: bool,
        group_id: int,
    ) -> tuple[str | None, list[int]]:
        with InterProcessLock(runtime_root / ".maibot_locks" / "kun.lock"):
            service = KunService(runtime_root / "db" / "kun" / "users.json")
            ranked_user_ids: list[int] = []

            def capture_ranked_user(_group_id: int, qq: int) -> str:
                ranked_user_ids.append(qq)
                return _display_name_token(qq)

            response = service.handle_command(
                text,
                user_id,
                now_millis,
                is_group=is_group,
                at_id=at_id,
                is_admin=is_admin,
                group_id=group_id,
                resolve_display_name=capture_ranked_user,
            )
            return response, ranked_user_ids


def _display_name_token(user_id: int) -> str:
    return f"\x00qqbot-kun-display-name:{user_id}\x00"


def _message_facts(
    message: object,
    text: str,
    group_id: str,
    user_id: str,
) -> dict[str, Any]:
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
        "text": text,
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


def create_plugin() -> QQBotKunPlugin:
    """创建养鲲插件实例。"""

    return QQBotKunPlugin()
