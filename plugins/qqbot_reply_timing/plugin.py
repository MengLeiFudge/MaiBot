from __future__ import annotations

from asyncio import CancelledError, create_task
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any, ClassVar, cast

import asyncio
import time

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


class PluginSection(PluginConfigBase):
    """首条消息等待修正的启用状态。"""

    enabled: bool = Field(default=False, description="首条回复的打字等待扣除已经花掉的处理时间")
    config_version: str = Field(default="0.1.0", description="配置版本")


class TimingConfig(PluginConfigBase):
    """只管理插件启用；发送速度继续使用框架现有配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


@dataclass
class ReplyTurn:
    """仅保存本轮源消息标识和时钟，不保存聊天正文。"""

    source_id: str
    started_at: float
    first_delivery_id: str = ""
    wait_task: asyncio.Task[None] | None = None


def normal_delay(text: str, speed: float, is_emoji: bool = False) -> float:
    """沿用原生发送器的字符计时，包括单汉字和表情的特殊规则。"""
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    if chinese == 1 and len(text.strip()) == 1:
        return 1.2
    if speed <= 0:
        return 0.0
    return (1.0 if is_emoji else chinese * 0.3 + (len(text) - chinese) * 0.15) * speed


class QQBotReplyTimingPlugin(MaiBotPlugin):
    """在公开发送 Hook 中修正首条等待，后续消息交还原生发送器。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = TimingConfig

    def __init__(self) -> None:
        """创建有界瞬时状态，支持加载失败后的安全卸载。"""
        super().__init__()
        self._received: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._turns: OrderedDict[str, ReplyTurn] = OrderedDict()
        self._active = False

    async def on_load(self) -> None:
        """启用首条等待修正，不执行外部请求。"""
        self._active = True
        self.ctx.logger.info("首条回复等待修正已加载，启用=%s", cast(TimingConfig, self.config).plugin.enabled)

    async def on_unload(self) -> None:
        """停止接纳新修正并释放瞬时状态。"""
        self._active = False
        self._received.clear()
        self._turns.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置更新后放弃旧轮次，下一条真实消息重新建立时钟。"""
        del scope, config_data, version
        self._received.clear()
        self._turns.clear()

    def _enabled(self) -> bool:
        return self._active and cast(TimingConfig, self.config).plugin.enabled

    @HookHandler(
        "chat.receive.before_process", name="qqbot_reply_timing_receive",
        description="记录真实接收消息的处理起点，不保存正文",
        mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def receive(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """只有结构化 QQ 消息才能成为可抵扣的起点。"""
        del kwargs
        if self._enabled() and isinstance(message, Mapping) and message.get("platform") == "qq":
            session_id, message_id = message.get("session_id"), message.get("message_id")
            if isinstance(session_id, str) and session_id and isinstance(message_id, str) and message_id:
                self._received[session_id] = (message_id, time.monotonic())
                self._received.move_to_end(session_id)
                if len(self._received) > 128:
                    self._received.popitem(last=False)
                turn = self._turns.get(session_id)
                if turn is not None and turn.source_id != message_id and turn.wait_task is not None:
                    turn.wait_task.cancel()
        return {"action": "continue"}

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_reply_timing_turn",
        description="将已进入本次模型上下文的接收消息关联到回复轮次",
        mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare(
        self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """工具续轮复用原时钟，只有新的真实源消息进入上下文才重新计时。"""
        del kwargs
        source = self._received.get(session_id)
        if not self._enabled() or source is None or item_schema_version != 1 or not isinstance(items, list):
            return {"action": "continue"}
        prefix = f'<message msg_id="{escape(source[0], quote=True)}" '
        present = False
        for item in items:
            if not isinstance(item, Mapping) or item.get("item_type") != "UserMessageItem":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                text = part.get("text") if isinstance(part, Mapping) else None
                if isinstance(text, str) and text.startswith(prefix):
                    present = True
                    break
        turn = self._turns.get(session_id)
        if present and (turn is None or turn.source_id != source[0]):
            self._turns[session_id] = ReplyTurn(*source)
            self._turns.move_to_end(session_id)
            if len(self._turns) > 128:
                self._turns.popitem(last=False)
        return {"action": "continue"}

    @HookHandler(
        "send_service.after_build_message", name="qqbot_reply_timing_native",
        description="让已完成后处理的回复首段进入统一发送计时",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_segment(
        self, message: object = None, processed_plain_text: str = "", typing: bool = False, **kwargs: Any,
    ) -> dict[str, object]:
        """原生回复首段默认关闭 typing；分段已完成时统一由首条抵扣决定补等。"""
        if (self._enabled() and not typing and processed_plain_text and isinstance(message, Mapping)
                and message.get("platform") == "qq" and message.get("session_id") in self._turns):
            return {"action": "continue", "modified_kwargs": {
                **kwargs, "message": message, "processed_plain_text": processed_plain_text, "typing": True,
            }}
        return {"action": "continue"}

    @HookHandler(
        "send_service.before_send", name="qqbot_reply_route",
        description="为本实例缺少连接标识的 QQ 出站消息补齐 NapCat 路由",
        mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=5000, error_policy=ErrorPolicy.ABORT,
    )
    async def prepare_route(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """只补本账号缺失的 scope，已有路由、目标与消息内容保持原样。"""
        if not self._enabled() or not isinstance(message, Mapping) or message.get("platform") != "qq":
            return {"action": "continue"}
        info = message.get("message_info")
        if not isinstance(info, Mapping):
            return {"action": "continue"}
        sender = info.get("user_info")
        extra = info.get("additional_config") or {}
        if not isinstance(sender, Mapping) or not isinstance(extra, Mapping):
            return {"action": "continue"}
        if any(extra.get(key) for key in ("platform_io_scope", "route_scope", "adapter_scope", "connection_id")):
            return {"action": "continue"}
        account = str(await self.ctx.config.get("bot.qq_account", ""))
        if not account or str(sender.get("user_id", "")) != account:
            return {"action": "continue"}
        accounts = [extra.get(key) for key in ("platform_io_account_id", "account_id", "self_id", "bot_account") if extra.get(key)]
        if any(str(value) != account for value in accounts):
            return {"action": "continue"}
        adapter = await self.ctx.config.get_plugin("maibot-team.napcat-adapter")
        server = adapter.get("napcat_server")
        scope = server.get("connection_id") if isinstance(server, Mapping) else None
        if not isinstance(scope, str) or not scope.strip():
            return {"action": "continue"}
        routed = {**message, "message_info": {**info, "additional_config": {
            **extra, "platform_io_account_id": account, "platform_io_scope": scope.strip(),
        }}}
        self.ctx.logger.info("已为本账号出站消息补齐 NapCat 连接标识")
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": routed}}

    @HookHandler(
        "send_service.before_send", name="qqbot_reply_timing_send",
        description="首条打字等待扣除本轮已经经过的处理时间",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=180000, error_policy=ErrorPolicy.ABORT,
    )
    async def before_send(self, message: object = None, typing: bool = False, **kwargs: Any) -> dict[str, object]:
        """原子占用首条资格；取消或未知发送结果不会让另一条消息重复抵扣。"""
        if not self._enabled() or not typing or not isinstance(message, Mapping) or message.get("platform") != "qq":
            return {"action": "continue"}
        session_id, delivery_id = message.get("session_id"), message.get("message_id")
        turn = self._turns.get(session_id) if isinstance(session_id, str) else None
        if turn is None or turn.first_delivery_id or not isinstance(delivery_id, str) or not delivery_id:
            return {"action": "continue"}
        # 先读取配置，再检查并占用，避免 RPC 让两个并发发送都取得首条资格。
        speed = float(await self.ctx.config.get("response_post_process.typing_speed", 1.0))
        if turn is not self._turns.get(session_id) or turn.first_delivery_id:
            return {"action": "continue"}
        turn.first_delivery_id = delivery_id
        text = message.get("processed_plain_text")
        delay = normal_delay(text if isinstance(text, str) else "", speed, message.get("is_emoji") is True)
        elapsed = max(0.0, time.monotonic() - turn.started_at)
        wait = max(0.0, delay - elapsed)
        received = self._received.get(session_id)
        if wait and (received is None or received[0] == turn.source_id):
            turn.wait_task = create_task(asyncio.sleep(wait))
            try:
                await turn.wait_task
            except CancelledError:
                received = self._received.get(session_id)
                if received is None or received[0] == turn.source_id:
                    raise
            finally:
                turn.wait_task = None
        if not self._enabled():
            return {"action": "abort"}
        received = self._received.get(session_id)
        if received is not None and received[0] != turn.source_id:
            # 此处尚未调用平台，旧首段可以明确放弃，不影响下一轮首条资格。
            turn.first_delivery_id = ""
            self.ctx.logger.info("首条等待期间收到新消息，取消旧首段发送")
            return {"action": "abort"}
        self.ctx.logger.info("首条回复等待: 原等待=%.3fs 已处理=%.3fs 补等=%.3fs", delay, elapsed, wait)
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": message, "typing": False}}

    @HookHandler(
        "send_service.after_send", name="qqbot_reply_timing_receipt",
        description="明确发送失败时释放首条资格，成功和未知结果均保留",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def after_send(self, message: object = None, sent: bool = False, **kwargs: Any) -> dict[str, object]:
        """只消费平台明确返回的失败；不猜测被取消的网络请求是否已经送达。"""
        del kwargs
        session_id = message.get("session_id") if isinstance(message, Mapping) else None
        if isinstance(message, Mapping) and isinstance(session_id, str):
            turn = self._turns.get(session_id)
            if turn is not None and not sent and turn.first_delivery_id == message.get("message_id"):
                turn.first_delivery_id = ""
        return {"action": "continue"}


def create_plugin() -> QQBotReplyTimingPlugin:
    """由公开 Runner 创建插件。"""
    return QQBotReplyTimingPlugin()
