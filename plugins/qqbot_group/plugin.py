from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import random
import re
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .cleanup import GroupFileCleanupService
from .social import WELCOME_EXPRESSIONS
from .social import format_member_welcome
from .social import format_self_join_notice
from .social import parse_group_increase_notice
from .social import parse_onebot_request
from .state import GroupStateStore


GROUP_FILE_CLEANUP_PATTERN = (
    r"^(?:通知)?(?:大家|全员|群友)?(?:清理|整理)(?:群)?文件$|"
    r"^(?:群)?文件(?:清理|整理)(?:通知)?$"
)


class PluginSection(PluginConfigBase):
    """Group plugin registration settings."""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册 QQBot 群务插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class SocialSection(PluginConfigBase):
    """OneBot social request settings."""

    __ui_label__ = "好友与邀请"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否处理好友申请、群邀请和入群事件")
    auto_approve_friend_requests: bool = Field(default=True, description="自动同意好友申请")
    auto_approve_group_invites: bool = Field(default=True, description="自动同意群邀请")
    owner_qq: str = Field(default="605738729", description="无法解析邀请者时接收通知的 QQ")


class WelcomeSection(PluginConfigBase):
    """Per-account deterministic welcome settings."""

    __ui_label__ = "入群欢迎"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否欢迎真人新成员")
    bot_account_ids: list[str] = Field(default_factory=list, description="不会触发欢迎的机器人 QQ")
    bot_display_names: dict[str, str] = Field(default_factory=dict, description="self_id 对应显示名")
    templates: dict[str, str] = Field(default_factory=dict, description="self_id 对应欢迎模板")


class CleanupSection(PluginConfigBase):
    """Group-file cleanup cutover settings."""

    __ui_label__ = "群文件清理"
    __ui_order__ = 3

    write_enabled: bool = Field(default=True, description="AstrBot 已停用，默认由 MaiBot 接管群文件清理")
    owner_qq: str = Field(default="605738729", description="允许执行清理命令的 QQ")
    old_file_grace_days: int = Field(default=7, ge=1, le=365, description="外层群文件宽限天数")
    fetch_count: int = Field(default=10_000, ge=1, le=50_000, description="单次群文件读取上限")
    message_interval_seconds: float = Field(default=1.0, ge=0.0, le=10.0, description="清理通知发送间隔")
    timezone: str = Field(default="Asia/Shanghai", description="时间判断时区")


class StorageSection(PluginConfigBase):
    """Shared SQLite settings."""

    __ui_label__ = "存储"
    __ui_order__ = 4

    runtime_root_override: str = Field(default="", description="留空时读取 QQBot 公共插件数据根")
    database_path: str = Field(default="db/qqbot_features.sqlite3", description="相对业务数据根的 SQLite 路径")


class GroupConfig(PluginConfigBase):
    """QQBot group plugin configuration."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    social: SocialSection = Field(default_factory=SocialSection)
    welcome: WelcomeSection = Field(default_factory=WelcomeSection)
    cleanup: CleanupSection = Field(default_factory=CleanupSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotGroupPlugin(MaiBotPlugin):
    """Handle OneBot social events and the controlled group-file cleanup command."""

    config_model: ClassVar[type[PluginConfigBase] | None] = GroupConfig

    async def on_load(self) -> None:
        self._store_instance: GroupStateStore | None = None
        self.ctx.logger.info(
            "QQBot 群务插件已加载，社交事件=%s，群文件接管=%s",
            self.config.social.enabled,
            self.config.cleanup.write_enabled,
        )

    async def on_unload(self) -> None:
        self._store_instance = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._store_instance = None
            self.ctx.logger.info("QQBot 群务配置已更新: %s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_group_protocol_gate",
        description="处理 OneBot 好友、邀请和入群事件并阻止其进入普通聊天",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=30_000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_protocol_event(
        self,
        message: object = None,
        **kwargs: Any,
    ) -> dict[str, str]:
        """Consume supported structured protocol events before chat and memory."""

        del kwargs
        request = parse_onebot_request(message)
        if request is not None:
            if not self.config.plugin.enabled:
                return {"action": "abort"}
            try:
                if self.config.social.enabled:
                    await self._handle_request(request)
            except Exception as exc:
                self.ctx.logger.warning(
                    "OneBot 请求处理失败并保持静默: request_type=%s error_type=%s",
                    request.request_type,
                    type(exc).__name__,
                )
            return {"action": "abort"}

        notice = parse_group_increase_notice(message)
        if notice is not None:
            if not self.config.plugin.enabled:
                return {"action": "abort"}
            try:
                if self.config.social.enabled:
                    await self._handle_group_increase(notice)
            except Exception as exc:
                self.ctx.logger.warning(
                    "OneBot 入群事件处理失败并保持静默: self_id=%s error_type=%s",
                    notice.self_id,
                    type(exc).__name__,
                )
            return {"action": "abort"}
        return {"action": "continue"}

    @Command(
        "qqbot_group_file_cleanup",
        description="通知清理超期外层群文件",
        pattern=GROUP_FILE_CLEANUP_PATTERN,
        timeout_ms=120_000,
    )
    async def handle_group_file_cleanup(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Run one owner-only, uniquely claimed group-file cleanup scan."""

        del kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            response = "群文件清理暂时无法确认当前机器人身份。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts)
        except Exception:
            response = "群文件清理命令仲裁失败，请稍后重试。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "群务功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not self.config.cleanup.write_enabled:
            response = "群文件清理接管当前已关闭。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if user_id != self.config.cleanup.owner_qq:
            response = "只有主人可以使用群文件清理。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not group_id:
            response = "群文件清理只能在群聊中使用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        try:
            service = GroupFileCleanupService(
                caller=_PluginActionCaller(self),
                store=await self._state_store(),
                group_id=group_id,
                old_file_grace_days=self.config.cleanup.old_file_grace_days,
                fetch_count=self.config.cleanup.fetch_count,
                message_interval_seconds=self.config.cleanup.message_interval_seconds,
                timezone_name=self.config.cleanup.timezone,
            )
            result = await service.run()
            response = (
                f"群文件扫描完成：外层 {result.root_file_count} 个，"
                f"超期 {result.violating_file_count} 个，禁言 {result.muted_user_count} 人，"
                f"禁言失败 {result.failed_mute_count} 人。"
            )
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        except Exception as exc:
            self.ctx.logger.warning("群文件清理失败: error_type=%s", type(exc).__name__)
            response = "群文件清理失败，请查看运行日志。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

    async def _handle_request(self, request) -> None:
        if request.request_type == "friend":
            if not self.config.social.auto_approve_friend_requests:
                await self._record_protocol_audit(
                    event_kind="friend_request",
                    action="set_friend_add_request",
                    sub_type=request.sub_type,
                    outcome="skipped",
                    failure_reason="disabled",
                    flag=request.flag,
                    self_id=request.self_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                )
            elif not request.flag:
                await self._record_protocol_audit(
                    event_kind="friend_request",
                    action="set_friend_add_request",
                    sub_type=request.sub_type,
                    outcome="skipped",
                    failure_reason="missing_flag",
                    flag="",
                    self_id=request.self_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                )
            else:
                await self._call_audited_action(
                    event_kind="friend_request",
                    action_name="set_friend_add_request",
                    sub_type=request.sub_type,
                    flag=request.flag,
                    self_id=request.self_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                    params={"flag": request.flag, "approve": True},
                )
            return
        if request.request_type != "group" or request.sub_type != "invite":
            return
        if not self.config.social.auto_approve_group_invites:
            await self._record_protocol_audit(
                event_kind="group_request",
                action="set_group_add_request",
                sub_type=request.sub_type,
                outcome="skipped",
                failure_reason="disabled",
                flag=request.flag,
                self_id=request.self_id,
                group_id=request.group_id,
                user_id=request.user_id,
            )
            return
        if not request.flag:
            await self._record_protocol_audit(
                event_kind="group_request",
                action="set_group_add_request",
                sub_type=request.sub_type,
                outcome="skipped",
                failure_reason="missing_flag",
                flag="",
                self_id=request.self_id,
                group_id=request.group_id,
                user_id=request.user_id,
            )
            return
        await self._call_audited_action(
            event_kind="group_request",
            action_name="set_group_add_request",
            sub_type=request.sub_type,
            flag=request.flag,
            self_id=request.self_id,
            group_id=request.group_id,
            user_id=request.user_id,
            params={"flag": request.flag, "sub_type": "invite", "approve": True},
        )
        if request.self_id and request.group_id and request.user_id:
            try:
                store = await self._state_store()
                await asyncio.to_thread(
                    store.remember_inviter,
                    request.self_id,
                    request.group_id,
                    request.user_id,
                )
            except Exception as exc:
                await self._record_protocol_audit(
                    event_kind="group_request",
                    action="remember_inviter",
                    sub_type=request.sub_type,
                    outcome="failure",
                    failure_reason=_safe_failure_reason(exc),
                    flag=request.flag,
                    self_id=request.self_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                )
                raise
            await self._record_protocol_audit(
                event_kind="group_request",
                action="remember_inviter",
                sub_type=request.sub_type,
                outcome="success",
                failure_reason="",
                flag=request.flag,
                self_id=request.self_id,
                group_id=request.group_id,
                user_id=request.user_id,
            )

    async def _handle_group_increase(self, notice) -> None:
        if notice.user_id == notice.self_id:
            await self._notify_self_joined(notice.self_id, notice.group_id, notice.sub_type)
            return
        bot_ids = {str(item).strip() for item in self.config.welcome.bot_account_ids if str(item).strip()}
        if not self.config.welcome.enabled or notice.user_id in bot_ids:
            await self._record_protocol_audit(
                event_kind="group_increase_notice",
                action="send_group_msg",
                sub_type=notice.sub_type,
                outcome="skipped",
                failure_reason="disabled" if not self.config.welcome.enabled else "bot_account",
                flag="",
                self_id=notice.self_id,
                group_id=notice.group_id,
                user_id=notice.user_id,
            )
            return
        template = self.config.welcome.templates.get(notice.self_id, "")
        message = format_member_welcome(template, random.SystemRandom().choice(WELCOME_EXPRESSIONS))
        await self._call_audited_action(
            event_kind="group_increase_notice",
            action_name="send_group_msg",
            sub_type=notice.sub_type,
            flag="",
            self_id=notice.self_id,
            group_id=notice.group_id,
            user_id=notice.user_id,
            params={
                "group_id": int(notice.group_id),
                "message": [
                    {"type": "at", "data": {"qq": notice.user_id}},
                    {"type": "text", "data": {"text": message}},
                ],
            },
        )

    async def _notify_self_joined(self, self_id: str, group_id: str, sub_type: str) -> None:
        store = await self._state_store()
        inviter_id = await asyncio.to_thread(store.pop_inviter, self_id, group_id)
        target_user_id = inviter_id or self.config.social.owner_qq
        try:
            group_info = await self._call_audited_action(
                event_kind="group_increase_notice",
                action_name="get_group_info",
                sub_type=sub_type,
                flag="",
                self_id=self_id,
                group_id=group_id,
                user_id=self_id,
                params={"group_id": int(group_id), "no_cache": True},
            )
        except Exception:
            group_info = {}
        group_name = str(group_info.get("group_name") or "").strip() if isinstance(group_info, Mapping) else ""
        bot_name = self.config.welcome.bot_display_names.get(self_id, self_id)
        await self._call_audited_action(
            event_kind="group_increase_notice",
            action_name="send_private_msg",
            sub_type=sub_type,
            flag="",
            self_id=self_id,
            group_id=group_id,
            user_id=target_user_id,
            params={
                "user_id": int(target_user_id),
                "message": format_self_join_notice(bot_name, group_name, group_id),
            },
        )

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="group_file_cleanup",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "群文件命令仲裁")
        return bool(payload.get("claimed")) if isinstance(payload, Mapping) else False

    async def _call_audited_action(
        self,
        *,
        event_kind: str,
        action_name: str,
        sub_type: str,
        flag: str,
        self_id: str,
        group_id: str,
        user_id: str,
        params: dict[str, object],
    ) -> object:
        try:
            payload = await self._call_action(action_name, params)
        except Exception as exc:
            await self._record_protocol_audit(
                event_kind=event_kind,
                action=action_name,
                sub_type=sub_type,
                outcome="failure",
                failure_reason=_safe_failure_reason(exc),
                flag=flag,
                self_id=self_id,
                group_id=group_id,
                user_id=user_id,
            )
            raise
        await self._record_protocol_audit(
            event_kind=event_kind,
            action=action_name,
            sub_type=sub_type,
            outcome="success",
            failure_reason="",
            flag=flag,
            self_id=self_id,
            group_id=group_id,
            user_id=user_id,
        )
        return payload

    async def _record_protocol_audit(
        self,
        *,
        event_kind: str,
        action: str,
        sub_type: str,
        outcome: str,
        failure_reason: str,
        flag: str,
        self_id: str,
        group_id: str,
        user_id: str,
    ) -> None:
        try:
            store = await self._state_store()
            await asyncio.to_thread(
                store.record_protocol_audit,
                event_kind=event_kind,
                action=action,
                sub_type=sub_type,
                outcome=outcome,
                failure_reason=failure_reason,
                flag=flag,
                self_id=self_id,
                group_id=group_id,
                user_id=user_id,
            )
        except Exception as exc:
            self.ctx.logger.warning(
                "OneBot 协议审计写入失败: event_kind=%s action=%s error_type=%s",
                event_kind,
                action,
                type(exc).__name__,
            )

    async def _call_action(self, action_name: str, params: dict[str, object]) -> object:
        result = await self.ctx.api.call(
            "adapter.napcat.action.call",
            action_name=action_name,
            params=params,
        )
        payload = require_api_result(result, f"NapCat 动作 {action_name}")
        if isinstance(payload, Mapping):
            status = str(payload.get("status") or "ok").lower()
            retcode = int(payload.get("retcode") or 0)
            if status not in {"ok", "success"} or retcode != 0:
                raise RuntimeError(f"NapCat 动作失败: {action_name}")
            return payload.get("data")
        return payload

    async def _state_store(self) -> GroupStateStore:
        if self._store_instance is not None:
            return self._store_instance
        root = self.config.storage.runtime_root_override.strip()
        if root:
            runtime_root = Path(root).expanduser().resolve()
        else:
            result = await self.ctx.api.call("qqbot.storage.runtime_root")
            payload = require_api_result(result, "读取 QQBot 业务数据根")
            path = str(payload.get("path") or "").strip() if isinstance(payload, Mapping) else ""
            if not path:
                raise RuntimeError("QQBot 公共插件没有返回业务数据根")
            runtime_root = Path(path).resolve()
        relative_path = Path(self.config.storage.database_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("database_path 必须是业务数据根内的相对路径")
        self._store_instance = GroupStateStore(runtime_root / relative_path)
        return self._store_instance



class _PluginActionCaller:
    def __init__(self, plugin: QQBotGroupPlugin) -> None:
        self.plugin = plugin

    async def call_action(self, action_name: str, params: dict[str, object]) -> object:
        return await self.plugin._call_action(action_name, params)


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    message_dict = message if isinstance(message, Mapping) else {}
    message_info = message_dict.get("message_info")
    info = message_info if isinstance(message_info, Mapping) else {}
    additional = info.get("additional_config")
    additional_dict = additional if isinstance(additional, Mapping) else {}
    targets: list[str] = []
    raw_message = message_dict.get("raw_message")
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target_id = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target_id and target_id not in targets:
                targets.append(target_id)
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


def _safe_failure_reason(exc: Exception) -> str:
    return f"action_failed:{type(exc).__name__}"


def create_plugin() -> QQBotGroupPlugin:
    """Create the QQBot group plugin."""

    return QQBotGroupPlugin()
