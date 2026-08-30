from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import asyncio
import base64
import re
import time
import urllib.request
from urllib.parse import urlparse

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .commands import COMMAND_PATTERN, MemeCommand, parse_command
from .remote_sync import RemoteSync, create_remote_sync
from .storage import MemeStore
from .web_server import MemeWebServer


DEFAULT_AUTO_SEND_CATEGORIES = (
    "affection_kiss", "agreement_yes", "angry", "angry_dislike", "apology_sorry",
    "awkward_silence", "baka", "boss_feed", "boss_worship", "color", "confused",
    "confused_question", "cpu", "cute_begging_trade", "fear_panic", "food_eat", "fool",
    "funny_laugh", "game_invite", "givemoney", "happy", "happy_cheer", "hug_comfort",
    "like", "meow", "morning", "no_need", "polite_awkward_smile", "reject_no", "reply",
    "rhythm_game_pressure", "sad", "sad_cry", "see", "self_noob", "shocked_surprise",
    "shy", "shy_flirt", "sigh", "slap_warning", "sleep", "sleep_rest", "surprised",
    "tired_defeated", "touch_rua", "troll_funny", "work",
)


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="启用表情管理固定命令")
    config_version: str = Field(default="0.2.0", description="配置版本")
    admin_user_ids: list[str] = Field(default_factory=lambda: ["605738729"], description="允许管理图库的 QQ 号")


class StorageSection(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_order__ = 1
    runtime_root: str = Field(default="", description="共享 qqbot_features_runtime 根；留空时从 qqbot_common 获取")


class AutoSendSection(PluginConfigBase):
    __ui_label__ = "自动表情"
    __ui_order__ = 2
    enabled: bool = Field(default=True, description="允许主聊天模型用粗类别标签请求本地表情")
    max_text_chars: int = Field(default=80, ge=1, le=500, description="允许自动配图的清洗后回复最大字符数")
    recent_history_size: int = Field(default=8, ge=0, le=100, description="每个会话近期不重复使用的图片数量")
    safe_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_SEND_CATEGORIES),
        description="仅这些轻松日常、玩梗、吐槽、撒娇或短情绪类别可自动发送",
    )


class WebUISection(PluginConfigBase):
    __ui_label__ = "管理后台"
    __ui_order__ = 3
    bind_host: str = Field(default="127.0.0.1", description="后台监听地址；对外暴露时必须配合防火墙")
    port: int = Field(default=5000, ge=1, le=65535, description="后台监听端口")


class RemoteSection(PluginConfigBase):
    __ui_label__ = "云端同步"
    __ui_order__ = 4
    provider: str = Field(default="", description="留空禁用；可选 stardots 或 cloudflare_r2")
    stardots_key: str = Field(default="", description="StarDots Key", json_schema_extra={"x-widget": "password"})
    stardots_secret: str = Field(default="", description="StarDots Secret", json_schema_extra={"x-widget": "password"})
    stardots_space: str = Field(default="memes", description="StarDots 空间")
    r2_account_id: str = Field(default="", description="Cloudflare Account ID")
    r2_access_key_id: str = Field(default="", description="R2 Access Key ID", json_schema_extra={"x-widget": "password"})
    r2_secret_access_key: str = Field(default="", description="R2 Secret Access Key", json_schema_extra={"x-widget": "password"})
    r2_bucket_name: str = Field(default="", description="R2 Bucket")


class MemeConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    auto_send: AutoSendSection = Field(default_factory=AutoSendSection)
    webui: WebUISection = Field(default_factory=WebUISection)
    remote: RemoteSection = Field(default_factory=RemoteSection)


class QQBotMemePlugin(MaiBotPlugin):
    """Native MaiBot command and lifecycle implementation for meme management."""

    config_model: ClassVar[type[PluginConfigBase] | None] = MemeConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: MemeStore | None = None
        self._web_server: MemeWebServer | None = None
        self._upload_states: dict[str, tuple[str, float]] = {}
        self._confirm_states: dict[str, tuple[str, str, float]] = {}
        self._recent_images: dict[str, deque[str]] = {}
        self._pending_images: dict[str, tuple[Path, dict[str, str], str, float]] = {}
        self._remote_lock = asyncio.Lock()

    async def on_load(self) -> None:
        await self._initialize_store()
        self.ctx.logger.info("QQBot 表情管理插件已加载，默认启用=%s，数据目录=%s", self.config.plugin.enabled, self.store.root)

    async def on_unload(self) -> None:
        if self._web_server is not None:
            await asyncio.to_thread(self._web_server.stop)
        self._web_server = None
        self._upload_states.clear()
        self._confirm_states.clear()
        self._recent_images.clear()
        self._pending_images.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self.on_unload()
        await self._initialize_store()
        self.ctx.logger.info("QQBot 表情管理配置已更新: %s", version)

    @Command("qqbot_meme_manager", description="管理本地表情图库和云端同步", pattern=COMMAND_PATTERN, timeout_ms=120000)
    async def handle_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del stream_id
        command_text = _text_segments(message) or _strip_leading_mention(text)
        command = parse_command(command_text)
        if command is None:
            return False, "", False
        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            raise ValueError("表情管理命令缺少当前机器人 self_id")
        if not await self._claim(command_text, facts):
            return True, "", True
        if not self.config.plugin.enabled:
            response = "表情管理功能当前已关闭。"
            await self._send(facts, response)
            return True, response, True
        if command.admin_only and not await self._is_admin(platform, user_id, facts["self_id"], kwargs):
            response = "这个表情管理指令只允许管理员使用。"
            await self._send(facts, response)
            return True, response, True
        try:
            response = await self._execute(command, facts)
        except Exception as exc:
            self.ctx.logger.warning(
                "表情管理命令失败: action=%s error_type=%s",
                command.action,
                type(exc).__name__,
            )
            response = _safe_command_error(exc)
        if response:
            await self._send(facts, response)
        return True, response, True

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_meme_upload_waiter",
        description="在聊天链前处理表情上传和危险操作确认",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_waiting_message(
        self,
        message: object = None,
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        del kwargs
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        facts = _message_facts(message, group_id, user_id)
        key = _state_key(facts)
        now = time.monotonic()
        confirmation = self._confirm_states.get(key)
        text = _text_segments(message)
        if confirmation is not None:
            action, argument, expires_at = confirmation
            if now > expires_at:
                self._confirm_states.pop(key, None)
                await self._send(facts, "等待确认超时，操作已取消。")
                return {"action": "abort"}
            if text in {"取消", "退出"}:
                self._confirm_states.pop(key, None)
                await self._send(facts, "已取消本次操作。")
                return {"action": "abort"}
            if text in {"确认", "确定"}:
                self._confirm_states.pop(key, None)
                try:
                    response = self._perform_dangerous(action, argument)
                except Exception as exc:
                    response = f"表情管理操作失败：{exc}"
                await self._send(facts, response)
                return {"action": "abort"}
            await self._send(facts, "请回复“确认”继续执行，或回复“取消”终止本次操作。")
            return {"action": "abort"}

        upload = self._upload_states.get(key)
        if upload is None:
            return {"action": "continue"}
        category, expires_at = upload
        if now > expires_at:
            self._upload_states.pop(key, None)
            await self._send(facts, "图片上传等待已超时，请重新发送添加表情指令。")
            return {"action": "abort"}
        images = _image_sources(message)
        if not images:
            await self._send(facts, "请发送图片文件来进行上传。")
            return {"action": "abort"}
        saved = 0
        errors: list[str] = []
        for index, source in enumerate(images, 1):
            try:
                filename, content = await asyncio.to_thread(_read_image_source, source, index)
                await asyncio.to_thread(self.store.add_bytes, category, filename, content)
                saved += 1
            except Exception as exc:
                self.ctx.logger.warning(
                    "表情上传读取失败: index=%s error_type=%s",
                    index,
                    type(exc).__name__,
                )
                errors.append("图片读取或保存失败")
        self._upload_states.pop(key, None)
        response = f"已成功收录 {saved} 张新表情到「{category}」图库。"
        if errors:
            response += f" 另有 {len(errors)} 张失败：{errors[0]}"
        await self._send(facts, response)
        return {"action": "abort"}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="qqbot_meme_category_contract",
        description="向主聊天模型注入可选的本地表情粗类别合同",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_category_contract(
        self,
        messages: list[object] | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        if not self.config.plugin.enabled or not self.config.auto_send.enabled or not isinstance(messages, list):
            return {"action": "continue"}
        categories = await asyncio.to_thread(self._selectable_categories)
        if not categories:
            return {"action": "continue"}
        names = "、".join(categories)
        contract = (
            "表情请求格式：仅当回复属于轻松日常、玩梗、吐槽、撒娇或短情绪，且确实适合配一张表情时，"
            f"可在有意义的纯文本回复末尾追加一个粗类别标记 &&类别&&。可用类别只有：{names}。"
            "不需要表情时不要输出标记；不得输出多个标记；支付、交易、敏感色情、严肃安全或待复核内容不得请求表情。"
            "标记是内部控制信息，不要解释。"
        )
        modified_messages = list(messages)
        modified_messages.append({"role": "system", "content": contract})
        return {
            "action": "continue",
            "modified_kwargs": {**kwargs, "messages": modified_messages},
        }

    @HookHandler(
        "send_service.before_send",
        name="qqbot_meme_clean_and_select",
        description="清理模型表情标签并从唯一索引选择本地图片",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def clean_and_select_before_send(
        self,
        message: object = None,
        storage_message: bool = False,
        **kwargs: Any,
    ) -> dict[str, object]:
        if not self.config.plugin.enabled or not isinstance(message, Mapping):
            return {"action": "continue"}
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return {"action": "continue"}
        aliases = await asyncio.to_thread(self._category_aliases)
        requested_category = ""
        changed = False
        cleaned_segments: list[object] = []
        cleaned_text_parts: list[str] = []
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "text":
                cleaned_segments.append(segment)
                continue
            original = str(segment.get("data") or "")
            cleaned, category = _clean_meme_tags(original, aliases)
            if category and not requested_category:
                requested_category = category
            changed = changed or cleaned != original
            cleaned_text_parts.append(cleaned)
            cleaned_segments.append({**segment, "data": cleaned})
        if not changed:
            return {"action": "continue"}

        now = time.monotonic()
        self._pending_images = {
            key: pending
            for key, pending in self._pending_images.items()
            if now - pending[3] <= 120
        }
        modified_message = dict(message)
        modified_message["raw_message"] = cleaned_segments
        cleaned_text = "".join(cleaned_text_parts).strip()
        modified_message["processed_plain_text"] = cleaned_text
        if (
            storage_message
            and self.config.auto_send.enabled
            and requested_category in self._safe_categories()
            and 0 < len(cleaned_text) <= self.config.auto_send.max_text_chars
        ):
            message_id = str(message.get("message_id") or "").strip()
            session_id = str(message.get("session_id") or message_id).strip()
            recent = self._recent_images.setdefault(
                session_id,
                deque(maxlen=self.config.auto_send.recent_history_size),
            )
            reserved = {
                str(path.relative_to(self.store.memes_dir))
                for path, _, pending_session_id, _ in self._pending_images.values()
                if pending_session_id == session_id
            }
            selected = await asyncio.to_thread(
                self.store.select_image,
                requested_category,
                cleaned_text,
                set(recent) | reserved,
            )
            facts = _outbound_facts(message)
            if selected is not None and message_id and (facts["group_id"] or facts["user_id"]):
                self._pending_images[message_id] = (selected, facts, session_id, now)
        return {
            "action": "continue",
            "modified_kwargs": {**kwargs, "message": modified_message, "storage_message": storage_message},
        }

    @HookHandler(
        "send_service.after_send",
        name="qqbot_meme_send_selected",
        description="主文本发送成功后经 NapCat 发送已选中的本地表情",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=30000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def send_selected_after_text(
        self,
        message: object = None,
        sent: bool = False,
        **kwargs: Any,
    ) -> dict[str, str]:
        del kwargs
        message_id = str(message.get("message_id") or "").strip() if isinstance(message, Mapping) else ""
        pending = self._pending_images.pop(message_id, None)
        if pending is None or not sent:
            return {"action": "continue"}
        path, facts, session_id, _ = pending
        try:
            encoded = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode("ascii")
            segments = [{"type": "image", "data": {"file": f"base64://{encoded}"}}]
            if facts["group_id"]:
                api_name = "adapter.napcat.group.send_group_msg"
                params = {"group_id": facts["group_id"], "message": segments}
            else:
                api_name = "adapter.napcat.message.send_private_msg"
                params = {"user_id": facts["user_id"], "message": segments}
            require_api_result(await self.ctx.api.call(api_name, params=params), "发送自动表情")
            recent = self._recent_images.setdefault(
                session_id,
                deque(maxlen=self.config.auto_send.recent_history_size),
            )
            recent.append(str(path.relative_to(self.store.memes_dir)))
        except Exception as exc:
            self.ctx.logger.warning(
                "自动表情发送失败: category=%s file=%s error_type=%s",
                path.parent.name,
                path.name,
                type(exc).__name__,
            )
        return {"action": "continue"}

    def _safe_categories(self) -> set[str]:
        return {str(item).strip() for item in self.config.auto_send.safe_categories if str(item).strip()}

    def _selectable_categories(self) -> list[str]:
        safe = self._safe_categories()
        return sorted(
            name
            for name, metadata in self.store.categories().items()
            if name in safe and metadata.get("auto_send_enabled", True) is not False
        )

    def _category_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for name, metadata in self.store.categories().items():
            aliases[name.casefold()] = name
            label = str(metadata.get("label") or "").strip()
            if label:
                aliases[label.casefold()] = name
        return aliases

    async def _execute(self, command: MemeCommand, facts: dict[str, Any]) -> str:
        action, argument = command.action, command.argument.strip()
        if action == "list_emotions":
            categories = self.store.categories()
            if not categories:
                return "当前图库为空。"
            return "当前图库：\n" + "\n".join(f"{name}: {meta.get('description') or '暂无描述'}" for name, meta in categories.items())
        if action == "library_stats":
            counts = self.store.category_counts()
            total = sum(counts.values())
            details = "\n".join(f"{name}: {count} 张" for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
            remote = "已配置" if self.config.remote.provider.strip() else "未配置"
            return f"表情图库统计\n总文件数: {total}\n分类数: {len(counts)}\n云端图库: {remote}" + (f"\n{details}" if details else "")
        if action == "upload_meme":
            if not argument:
                return "用法：表情管理 添加表情 [类别名称]"
            if argument not in self.store.categories():
                return f"不存在的表情类别「{argument}」，可先发送 表情管理 查看图库。"
            self._upload_states[_state_key(facts)] = (argument, time.monotonic() + 30)
            return f"请在30秒内发送要添加到「{argument}」类别的图片，可一次发送多张。"
        if action == "restore_default_memes":
            result = await asyncio.to_thread(self.store.restore_defaults, argument)
            if not result["source_exists"]:
                return "未找到插件内置默认表情包资源。"
            return f"默认表情包恢复完成，新增 {result['copied']} 张，跳过重复 {result['duplicates']} 张。"
        if action in {"clear_category", "delete_category"}:
            if not argument:
                label = "清空指定类型" if action == "clear_category" else "删除类型本身"
                return f"用法：表情管理 {label} [类别名称]"
            if argument not in self.store.categories():
                return f"不存在的表情类别「{argument}」。"
            count = self.store.category_counts().get(argument, 0)
            self._confirm_states[_state_key(facts)] = (action, argument, time.monotonic() + 30)
            verb = "清空并保留类型" if action == "clear_category" else "删除类型本身"
            return f"即将{verb}「{argument}」，涉及 {count} 张表情。请在30秒内回复“确认”或“取消”。"
        if action == "clear_all":
            count = sum(self.store.category_counts().values())
            if count == 0:
                return "当前没有可清空的表情文件。"
            self._confirm_states[_state_key(facts)] = (action, "", time.monotonic() + 30)
            return f"即将清空全部 {count} 张表情并保留分类。请在30秒内回复“确认”或“取消”。"
        if action == "start_webui":
            if facts["group_id"]:
                return "该指令仅限私聊使用，请私聊发送“表情管理 开启管理后台”。"
            if self._web_server is None:
                self._web_server = MemeWebServer(
                    self.store,
                    Path(__file__).resolve().parent,
                    self.config.webui.bind_host.strip() or "127.0.0.1",
                    self.config.webui.port,
                    remote_status=self._web_remote_status if self.config.remote.provider.strip() else None,
                    remote_task=self._web_remote_task if self.config.remote.provider.strip() else None,
                )
            if not self._web_server.running:
                await asyncio.to_thread(self._web_server.start)
            return f"管理后台已就绪：http://{self.config.webui.bind_host or '127.0.0.1'}:{self._web_server.port}\n临时密钥：{self._web_server.key}\n请勿分享给未授权用户。"
        if action == "stop_webui":
            if self._web_server is None or not self._web_server.running:
                return "管理后台当前未运行。"
            await asyncio.to_thread(self._web_server.stop)
            self._web_server = None
            return "管理后台已关闭。"
        if action == "sync_status":
            status = await self._remote_status()
            return f"图床同步状态\n服务: {status['provider_label']}\n待上传: {status['upload_count']}\n待下载: {status['download_count']}\n云端文件: {status['remote_image_count']}"
        if action in {"sync_to_remote", "sync_from_remote", "overwrite_to_remote", "overwrite_from_remote"}:
            task = {"sync_to_remote": "upload", "sync_from_remote": "download", "overwrite_to_remote": "overwrite_to_remote", "overwrite_from_remote": "overwrite_from_remote"}[action]
            result = await self._run_remote(task)
            return f"云端同步完成：上传 {result['uploaded']}，下载 {result['downloaded']}，删除 {result['deleted']}。"
        return "未知的表情管理指令。"

    def _perform_dangerous(self, action: str, argument: str) -> str:
        if action == "clear_category":
            count = self.store.clear_category(argument)
            return f"已清空类型「{argument}」，共删除 {count} 张表情。"
        if action == "delete_category":
            count = self.store.delete_category(argument)
            return f"已删除类型「{argument}」及其中 {count} 张表情。"
        if action == "clear_all":
            count = self.store.clear_all()
            return f"已清空全部表情，共删除 {count} 张，分类配置已保留。"
        raise ValueError("未知确认操作")

    async def _initialize_store(self) -> None:
        configured = self.config.storage.runtime_root.strip()
        if configured:
            runtime_root = Path(configured).expanduser().resolve()
        else:
            result = await self.ctx.api.call("qqbot.storage.runtime_root")
            payload = require_api_result(result, "读取 QQBot 共享数据根")
            if not isinstance(payload, Mapping) or not str(payload.get("path") or "").strip():
                raise RuntimeError("QQBot 共享数据根返回格式无效")
            runtime_root = Path(str(payload["path"])).expanduser().resolve()
        self._store = MemeStore(runtime_root / "meme_manager", Path(__file__).resolve().parent / "memes")

    @property
    def store(self) -> MemeStore:
        if self._store is None:
            raise RuntimeError("表情存储尚未初始化")
        return self._store

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call("qqbot.route.claim", feature="meme_manager", text=text, user_id=facts["user_id"], group_id=facts["group_id"], self_id=facts["self_id"], timestamp=facts["timestamp"], at_target_ids=facts["at_target_ids"])
        payload = require_api_result(result, "表情管理命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("表情管理命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

    async def _is_admin(self, platform: str, user_id: str, self_id: str, kwargs: dict[str, Any]) -> bool:
        if kwargs.get("is_local_operator") is True or user_id == self_id:
            return True
        if user_id in {str(item).strip() for item in self.config.plugin.admin_user_ids}:
            return True
        permissions = await self.ctx.config.get("plugin.permission")
        normalized = {str(item).strip().lower() for item in permissions} if isinstance(permissions, list) else set()
        return f"{platform.strip().lower()}:{user_id}" in normalized

    async def _send(self, facts: dict[str, Any], text: str) -> None:
        segments = [{"type": "text", "data": {"text": text}}]
        if facts["group_id"]:
            api_name, params = "adapter.napcat.group.send_group_msg", {"group_id": facts["group_id"], "message": segments}
        else:
            api_name, params = "adapter.napcat.message.send_private_msg", {"user_id": facts["user_id"], "message": segments}
        require_api_result(await self.ctx.api.call(api_name, params=params), "发送表情管理消息")

    def _remote_config(self) -> dict[str, str]:
        if self.config.remote.provider.strip().lower() in {"r2", "cloudflare_r2"}:
            return {"account_id": self.config.remote.r2_account_id, "access_key_id": self.config.remote.r2_access_key_id, "secret_access_key": self.config.remote.r2_secret_access_key, "bucket_name": self.config.remote.r2_bucket_name}
        return {"key": self.config.remote.stardots_key, "secret": self.config.remote.stardots_secret, "space": self.config.remote.stardots_space}

    def _new_remote(self) -> RemoteSync:
        return create_remote_sync(self.store, self.config.remote.provider, self._remote_config())

    async def _remote_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._new_remote().status)

    async def _run_remote(self, task: str) -> dict[str, int]:
        async with self._remote_lock:
            return await asyncio.to_thread(self._new_remote().run, task)

    def _web_remote_status(self) -> dict[str, Any]:
        return self._new_remote().status()

    def _web_remote_task(self, task: str) -> None:
        self._new_remote().run(task)


def _safe_command_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, ValueError) and message.startswith(
        ("图床服务未配置", "Cloudflare R2 配置缺少", "StarDots 配置缺少")
    ):
        return message
    return "表情管理操作失败，请查看运行日志。"


def _outbound_facts(message: Mapping[str, Any]) -> dict[str, str]:
    info = message.get("message_info") if isinstance(message.get("message_info"), Mapping) else {}
    group = info.get("group_info") if isinstance(info.get("group_info"), Mapping) else {}
    additional = info.get("additional_config") if isinstance(info.get("additional_config"), Mapping) else {}
    return {
        "group_id": str(additional.get("platform_io_target_group_id") or group.get("group_id") or "").strip(),
        "user_id": str(additional.get("platform_io_target_user_id") or "").strip(),
    }


def _clean_meme_tags(text: str, aliases: Mapping[str, str]) -> tuple[str, str]:
    requested = ""

    def capture(match: re.Match[str]) -> str:
        nonlocal requested
        token = match.group("token").strip().casefold()
        if not requested and token in aliases:
            requested = aliases[token]
        return ""

    cleaned = re.sub(r"&&\s*(?P<token>[^&\r\n]{1,40}?)\s*&&", capture, text)
    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        malformed = re.compile(
            rf"(?<![\w&])(?:&&\s*{escaped}\s*&?|&\s*{escaped}\s*&{{1,2}}|{escaped}\s*&&)(?![\w&])",
            re.IGNORECASE,
        )
        if malformed.search(cleaned) and not requested:
            requested = aliases[alias]
        cleaned = malformed.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+(?=\r?$)", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), requested


def _state_key(facts: Mapping[str, Any]) -> str:
    return f"{facts.get('group_id') or 'private'}:{facts.get('user_id')}"


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    root = message if isinstance(message, Mapping) else {}
    info = root.get("message_info") if isinstance(root.get("message_info"), Mapping) else {}
    group_info = info.get("group_info") if isinstance(info.get("group_info"), Mapping) else {}
    user_info = info.get("user_info") if isinstance(info.get("user_info"), Mapping) else {}
    resolved_group_id = str(group_id or group_info.get("group_id") or "").strip()
    resolved_user_id = str(user_id or user_info.get("user_id") or "").strip()
    additional = info.get("additional_config") if isinstance(info.get("additional_config"), Mapping) else {}
    targets = []
    raw = root.get("raw_message")
    if isinstance(raw, list):
        for segment in raw:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data") if isinstance(segment.get("data"), Mapping) else {}
            target = str(data.get("target_user_id") or data.get("qq") or "").strip()
            if target and target not in targets:
                targets.append(target)
    try:
        timestamp = float(root.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {"group_id": resolved_group_id, "user_id": resolved_user_id, "self_id": str(additional.get("self_id") or "").strip(), "timestamp": timestamp, "at_target_ids": targets}


def _text_segments(message: object) -> str:
    if not isinstance(message, Mapping) or not isinstance(message.get("raw_message"), list):
        return ""
    parts = []
    for segment in message["raw_message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        parts.append(str(data.get("text") or data.get("content") or "") if isinstance(data, Mapping) else str(data or ""))
    return "".join(parts).strip()


def _image_sources(message: object) -> list[dict[str, str]]:
    if not isinstance(message, Mapping) or not isinstance(message.get("raw_message"), list):
        return []
    result = []
    for segment in message["raw_message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "image":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), Mapping) else {}
        result.append({key: str(data.get(key) or "") for key in ("file", "url", "file_name", "filename", "base64")})
    return result


def _read_image_source(source: dict[str, str], index: int) -> tuple[str, bytes]:
    name = Path(source.get("file_name") or source.get("filename") or f"upload_{int(time.time())}_{index}.img").name
    file_value = source.get("file", "")
    encoded = source.get("base64") or (file_value.removeprefix("base64://") if file_value.startswith("base64://") else "")
    if encoded:
        return name, base64.b64decode(encoded, validate=True)
    url = source.get("url") or source.get("file")
    if not url.startswith(("http://", "https://")):
        raise ValueError("图片没有可读取的 URL 或 base64 数据")
    request = urllib.request.Request(url, headers={"User-Agent": "QQBot-Meme/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("图片超过 20 MiB 限制")
        remote_name = Path(urlparse(url).path).name
        return name if name.endswith(tuple([".png", ".jpg", ".jpeg", ".gif", ".webp"])) else (remote_name or name), content


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", str(text or "").strip(), count=1)


def create_plugin() -> QQBotMemePlugin:
    return QQBotMemePlugin()
