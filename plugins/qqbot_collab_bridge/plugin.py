"""默认禁用的 MaiBot 对等桥接，运行启用方仍由账号合同决定。"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast
import asyncio
import contextlib
import re
import uuid

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .client import BridgeClient
from .queue import Queue


class PluginSection(PluginConfigBase):
    """对等插件必须默认禁用，避免两个账号执行相同副作用。"""
    enabled: bool = Field(default=False, description="默认禁用；迁移运行账号须另经用户授权")
    config_version: str = Field(default="0.1.0", description="配置版本")


class BridgeSection(PluginConfigBase):
    """与 Pi 本地配置逐项匹配的身份和有界批次参数。"""
    bridge_id: str = Field(default="", description="桥接UUID")
    database_id: str = Field(default="", description="collab数据库UUID")
    generation: str = Field(default="", description="当前世代UUID")
    task_id: str = Field(default="", description="目标房间UUID")
    platform_id: str = Field(default="qq", description="NapCat Host平台标识")
    bot_id: str = Field(default="2629227874", description="当前对等账号；Pi配置须一致")
    token: str = Field(default="", description="至少32位随机base64url密钥，不提交Git")
    port: int = Field(default=19191, ge=1024, le=65535, description="本机端口")
    model: str = Field(default="replyer", description="无工具文本生成任务名")
    batch_size: int = Field(default=10, ge=1, le=50, description="每群触发条数")
    batch_minutes: int = Field(default=30, ge=1, le=1440, description="每群最大等待分钟")
    daily_attempts: int = Field(default=24, ge=1, le=1000, description="UTC日模型尝试总数，失败也计数")
    max_raw_bytes: int = Field(default=16777216, ge=65536, le=268435456, description="原文容量字节数")


class BridgePluginConfig(PluginConfigBase):
    """配置只作用于本插件，不改框架或其他账号。"""
    plugin: PluginSection = Field(default_factory=PluginSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)


class QQBotCollabBridge(MaiBotPlugin):
    """通过公开 Hook 与 SDK 管理需求、主人决定和输出。"""
    config_model: ClassVar[type[PluginConfigBase] | None] = BridgePluginConfig

    def __init__(self):
        """资源由on_load创建，默认禁用时不打开队列和网络。"""
        super().__init__()
        self.queue: Queue | None = None
        self.worker: asyncio.Task | None = None

    async def on_load(self):
        """只有显式启用且绑定完整时才创建工作循环。"""
        if not cast(BridgePluginConfig, self.config).plugin.enabled:
            return
        config = cast(BridgePluginConfig, self.config).bridge
        for key in ("bridge_id", "database_id", "generation", "task_id"):
            value = getattr(config, key)
            if str(uuid.UUID(value)) != value:
                raise ValueError(f"{key} 无效")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", config.token) or config.platform_id != "qq" or not config.bot_id.isdecimal():
            raise ValueError("桥接密钥或NapCat平台身份无效")
        binding = {key: getattr(config, key) for key in ("bridge_id", "database_id", "generation", "task_id", "platform_id", "bot_id")}
        self.queue = Queue(Path(self.ctx.paths.data_dir) / "queue.sqlite3", binding, config.max_raw_bytes)
        self.client = BridgeClient(config, self.queue, self.summarize, self.send, self.ctx.logger)
        self.worker = asyncio.create_task(self.client.run())

    async def on_unload(self):
        """先停止工作循环，再释放本实例队列。"""
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.queue:
            self.queue.close()
        self.worker = self.queue = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str):
        """配置重载沿相同资源生命周期，旧绑定队列不自动改绑。"""
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self.on_unload()
            await self.on_load()

    async def summarize(self, prompt: str) -> str:
        """只调用SDK普通文本生成，绝不进入工具循环。"""
        result = await self.ctx.llm.generate(prompt, model=cast(BridgePluginConfig, self.config).bridge.model, max_tokens=2200)
        if not isinstance(result, Mapping) or not result.get("success"):
            raise RuntimeError("桥接汇总失败")
        return str(result.get("response") or result.get("content") or "").strip()

    async def send(self, target: str, body: str, private: bool):
        """通过NapCat公开消息API发送到已绑定的目标。"""
        if not target.isdecimal() or private and target != "605738729":
            raise ValueError("回传目标无效")
        name = "adapter.napcat.message.send_private_msg" if private else "adapter.napcat.group.send_group_msg"
        params = {"user_id" if private else "group_id": target, "message": [{"type": "text", "data": {"text": body}}]}
        require_api_result(await self.ctx.api.call(name, params=params), "桥接回传")

    @HookHandler("chat.receive.before_process", name="qqbot_collab_bridge", description="显式协作需求与主人确认", mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=10000, error_policy=ErrorPolicy.SKIP)
    async def collect(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        """只读取适配器结构化来源；命中后中止普通聊天处理。"""
        del kwargs
        if not cast(BridgePluginConfig, self.config).plugin.enabled or self.queue is None or not isinstance(message, Mapping):
            return {"action": "continue"}
        info = message.get("message_info", {})
        if not isinstance(info, Mapping):
            return {"action": "continue"}
        extra = info.get("additional_config", {})
        user = info.get("user_info", {})
        group_info = info.get("group_info", {})
        if not isinstance(extra, Mapping) or not isinstance(user, Mapping) or not isinstance(group_info, Mapping):
            return {"action": "continue"}
        config = cast(BridgePluginConfig, self.config).bridge
        sender, group = str(user.get("user_id", "")), str(group_info.get("group_id", ""))
        if message.get("platform") != config.platform_id or str(extra.get("self_id", "")) != config.bot_id or sender == config.bot_id:
            return {"action": "continue"}
        segments = message.get("raw_message", [])
        source_types = extra.get("napcat_segment_types", [])
        if not isinstance(segments, list) or not isinstance(source_types, list) or not source_types or any(kind not in ("text", "at") for kind in source_types):
            return {"action": "continue"}
        if any(not isinstance(part, Mapping) or part.get("type") not in ("text", "at") for part in segments):
            return {"action": "continue"}
        parts = cast(list[Mapping[str, Any]], segments)
        plain = "".join(str(part.get("data", "")) for part in parts if part.get("type") == "text").strip()
        mentioned = any(part.get("type") == "at" and isinstance(part.get("data"), Mapping) and str(part["data"].get("target_user_id")) == config.bot_id for part in parts)
        demand = bool(group) and mentioned and (plain == "需求" or plain.startswith("需求 ") or plain.startswith("需求\n"))
        private = not group and extra.get("napcat_message_type") == "private"
        confirmation = private and plain.startswith("确认 ")
        if not demand and not confirmation:
            return {"action": "continue"}
        try:
            source = str(message.get("message_id", ""))
            if not source:
                raise ValueError("缺少平台消息ID")
            if demand:
                self.queue.expire()
                self.queue.collect(group, source, sender, plain[2:].strip())
            elif sender == "605738729":
                match = re.fullmatch(r"确认 ([0-9a-f-]{36}) ([a-zA-Z0-9_-]{1,32})", plain)
                if not match:
                    await self.send(sender, "格式：确认 <决策ID> <选项>", True)
                else:
                    decision = str(uuid.UUID(match[1]))
                    payload = {"decision_id": decision, "option": match[2], "event": {"platform_id": config.platform_id, "bot_id": config.bot_id, "sender_id": sender, "message_type": "private", "group_id": "", "message_id": source, "body": plain}}
                    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"collab-reply:{config.bridge_id}:{config.generation}:{source}"))
                    self.queue.reply(identifier, payload)
                    await self.send(sender, "确认已排队，等待Pi核验；尚不表示执行完成。", True)
        except Exception as exc:
            self.ctx.logger.warning("桥接输入未确认接收：%s", type(exc).__name__)
            try:
                if demand:
                    await self.send(group, "协作收件失败：内容过长、队列已满或存储不可用。", False)
                elif sender == "605738729":
                    await self.send(sender, "协作确认未入队，请核对格式或存储状态。", True)
            except Exception as notify_error:
                self.ctx.logger.warning("桥接错误通知发送失败：%s", type(notify_error).__name__)
        return {"action": "abort"}
