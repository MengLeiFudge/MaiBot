from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, ClassVar, cast

import asyncio
import uuid

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


REPLY_RULE = (
    "[公开答复规则]send_reply 直接发送你已经写好的对外答复，不再调用模型改写；"
    "text 必须符合完整人设和当前真实身份，依据上下文、记忆和知识证据，是可独立阅读的正文，不是内部分析。"
    "普通聊天已经能可靠回答时可直接用它；需要原生回复器组织表达、处理图片、引用或附件时继续用 reply。"
    "同一段答复在 send_reply 和 reply 之间二选一，已经发出的内容不要再复述。"
    "复杂任务可以先发送有帮助且已确认的部分，phase=progress，再继续查询或研究，有新增证据后补充；"
    "不要把待核实的事实提前当结论，不用空泛的‘正在思考’凑首条回复。简单聊天不强制分段。"
    "发送工具不会结束研究；已能回答的部分不必等无依赖的记账或工具发现完成，依赖工具的结论仍须等待结果。"
    "联网时先发现并实际调用 web_search；tool_search 只发现本地工具。历史经历只依据实际上下文或检索结果。"
    "查询失败时说明已有结论和缺失证据；只有新线索、不同核验方法或可恢复的临时错误才重试。"
    "默认 phase=answer，仍有明确研究待做才用 progress。决定保持安静时不调用发送工具。[/公开答复规则]"
)


class PluginSection(PluginConfigBase):
    """直接公开答复的启用状态。"""

    enabled: bool = Field(default=False, description="允许发送已经写好的公开答复及研究中的可靠阶段结论")
    config_version: str = Field(default="0.1.0", description="配置版本")


class GuardSection(PluginConfigBase):
    """限制插件运行的机器人实例。"""

    bot_qq: str = Field(default="2629227874", pattern=r"^[1-9][0-9]{4,11}$", description="目标机器人 QQ")


class GuardConfig(PluginConfigBase):
    """本机启用配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    guard: GuardSection = Field(default_factory=GuardSection)


@dataclass
class ReplyTurn:
    """只保存经核验的目标和本轮发送回执。"""

    message_id: str
    user_id: str
    group_id: str
    receipts: dict[str, dict[str, object]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class QQBotReplyGuardPlugin(MaiBotPlugin):
    """通过公开工具发送 Planner 已写好的正文，原生 Replyer 继续可用。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = GuardConfig

    def __init__(self) -> None:
        """创建瞬时状态；加载中途失败时也能安全卸载。"""
        super().__init__()
        self._active = False
        self._turns: OrderedDict[str, ReplyTurn] = OrderedDict()

    async def on_load(self) -> None:
        """加载期间不发送消息或调用模型。"""
        self._active = True
        self.ctx.logger.info("公开答复工具已加载，启用=%s", cast(GuardConfig, self.config).plugin.enabled)

    async def on_unload(self) -> None:
        """不再接纳新发送；已交给平台的请求不自动重试。"""
        self._active = False
        self._turns.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置更新使旧目标失效；后续规划必须重新核验真实消息。"""
        del scope, config_data, version
        self._turns.clear()

    async def _source(self, session_id: str) -> tuple[str, str, str] | None:
        """从真实消息与会话 API 核验最新人类消息，群聊和私聊使用同一边界。"""
        config = cast(GuardConfig, self.config)
        if not self._active or not config.plugin.enabled or not session_id:
            return None
        if str(await self.ctx.config.get("bot.qq_account", "")) != config.guard.bot_qq:
            return None
        messages = await self.ctx.call_capability(
            "message.get_recent", chat_id=session_id, limit=20, filter_mai=False, include_binary_data=False,
        )
        if not isinstance(messages, list):
            raise ValueError("公开答复目标查询没有返回消息列表")
        for message in reversed(messages):
            if not isinstance(message, Mapping) or message.get("session_id") != session_id or message.get("platform") != "qq":
                return None
            info = message.get("message_info")
            sender = info.get("user_info") if isinstance(info, Mapping) else None
            user_id = sender.get("user_id") if isinstance(sender, Mapping) else None
            if user_id == config.guard.bot_qq:
                continue
            message_id = message.get("message_id")
            if not isinstance(user_id, str) or not user_id or not isinstance(message_id, str) or not message_id:
                return None
            # 需要媒体或引用理解时保留原生回复器及其完整预处理路径。
            segments = message.get("raw_message")
            if not isinstance(segments, list) or any(
                not isinstance(part, Mapping) or part.get("type") not in ("text", "at") for part in segments
            ):
                return None
            group = info.get("group_info") if isinstance(info, Mapping) else None
            group_id = group.get("group_id") if isinstance(group, Mapping) else ""
            if not isinstance(group_id, str):
                return None
            stream = (await self.ctx.chat.get_stream_by_group_id(group_id, platform="qq") if group_id
                      else await self.ctx.chat.get_stream_by_user_id(user_id, platform="qq"))
            if not isinstance(stream, Mapping) or stream.get("stream_id") != session_id or stream.get("platform") != "qq":
                return None
            if str(stream.get("group_id") or "") != group_id or (not group_id and stream.get("user_id") != user_id):
                return None
            if config is not self.config or not self._active:
                return None
            return message_id, user_id, group_id
        return None

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_reply_guard_prepare",
        description="向已具有原生回复资格的当前会话提供直接公开答复",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=8000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_reply(
        self, session_id: str = "", items: object = None, tool_definitions: object = None,
        item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """源消息必须同时存在于真实会话和本次上下文，不扩大原生回复资格。"""
        if item_schema_version != 1 or not isinstance(items, list) or not isinstance(tool_definitions, list):
            return {"action": "continue"}
        tools, direct = [], []
        for tool in tool_definitions:
            function = tool.get("function") if isinstance(tool, Mapping) else None
            (direct if isinstance(function, Mapping) and function.get("name") == "send_reply" else tools).append(tool)
        modified = {**kwargs, "session_id": session_id, "items": items,
                    "tool_definitions": tools, "item_schema_version": item_schema_version}
        if not any(isinstance(tool, Mapping) and isinstance(tool.get("function"), Mapping)
                   and tool["function"].get("name") == "reply" for tool in tools):
            return {"action": "continue", "modified_kwargs": modified}
        async with asyncio.timeout(5):
            source = await self._source(session_id)
        if source is None:
            self._turns.pop(session_id, None)
            return {"action": "continue", "modified_kwargs": modified}
        message_id = source[0]
        prefix = f'<message msg_id="{escape(message_id, quote=True)}" '
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
        if not present:
            self._turns.pop(session_id, None)
            return {"action": "continue", "modified_kwargs": modified}
        turn = self._turns.get(session_id)
        if turn is None or (turn.message_id, turn.user_id, turn.group_id) != source:
            self._turns[session_id] = ReplyTurn(*source)
            self._turns.move_to_end(session_id)
            if len(self._turns) > 128:
                self._turns.popitem(last=False)
        rule = {"item_type": "SystemMessageItem", "meta": {
            "item_id": uuid.uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat(),
        }, "parts": [{"type": "text", "text": REPLY_RULE}]}
        return {"action": "continue", "modified_kwargs": {
            **modified, "items": [*items, rule], "tool_definitions": [*tools, *direct],
        }}

    @Tool(
        "send_reply", description="直接发送已经写好的公开答复；可先发可靠阶段结论，再继续研究。不要发送内部分析。",
        parameters={
            "msg_id": {"type": "string", "required": True, "description": "本轮最新真实消息的 msg_id，续答仍用相同 ID"},
            "text": {"type": "string", "required": True, "minLength": 1, "maxLength": 2000,
                     "description": "符合完整人设和证据的公开正文；长文、引用或附件可用原生 reply"},
            "phase": {"type": "string", "enum": ["progress", "answer"], "default": "answer",
                      "description": "仍有明确研究待做时用 progress，否则用 answer"},
        }, visibility="visible", timeout_ms=180000,
    )
    async def send_reply(
        self, msg_id: object = "", text: object = "", phase: object = "answer", **kwargs: Any,
    ) -> dict[str, object]:
        """发送前再次核验最新源消息，同文并发只发送一次，未知结果不重发。"""
        if not isinstance(msg_id, str) or not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
            return {"success": False, "content": "需要真实消息 ID 和 1–2000 字公开正文；长文请使用 reply。"}
        if phase not in ("progress", "answer"):
            return {"success": False, "content": "答复阶段只能是 progress 或 answer。"}
        session_id = str(kwargs.get("stream_id") or "")
        turn = self._turns.get(session_id)
        if turn is None or turn.message_id != msg_id or str(kwargs.get("group_id") or "") != turn.group_id:
            return {"success": False, "content": "只能答复本轮已经核验的当前会话。"}
        text = text.strip()
        async with turn.lock:
            async with asyncio.timeout(5):
                source = await self._source(session_id)
            if source != (turn.message_id, turn.user_id, turn.group_id) or self._turns.get(session_id) is not turn:
                return {"success": False, "content": "最新消息或配置已经变化，旧答复未发送；请结合最新消息重新判断。"}
            if text in turn.receipts:
                return turn.receipts[text]
            uncertain: dict[str, object] = {"success": False, "content": "发送结果未确认，不要重复相同正文，请先查看最新消息。"}
            turn.receipts[text] = uncertain
            try:
                async with asyncio.timeout(150):
                    result = await self.ctx.send.text(
                        text, session_id, typing=True, set_reply=False, storage_message=True,
                        sync_to_maisaka_history=True, maisaka_source_kind="outbound_send", return_details=True, timeout_ms=150000,
                    )
            except Exception as exc:
                self.ctx.logger.warning("公开答复发送结果未确认: error_type=%s", type(exc).__name__)
                return uncertain
            if not isinstance(result, Mapping) or result.get("sent") is not True:
                turn.receipts[text] = {"success": False, "content": "平台没有确认发送成功，请先查看最新消息。"}
            else:
                turn.receipts[text] = {
                    "success": True, "message_id": result.get("message_id"), "phase": phase,
                    "content": "答复已发送并写入聊天历史；继续尚未完成的研究，有新增结论再补充，不要复述。"
                    if phase == "progress" else "答复已发送并写入聊天历史；需求完成时结束，不要重复发送。",
                }
                self.ctx.logger.info("公开答复已发送: phase=%s chars=%s", phase, len(text))
            return turn.receipts[text]

    @HookHandler(
        "maisaka.planner.after_response", name="qqbot_reply_guard_deduplicate",
        description="移除同批同目标重复的纯文字最终回复，保留研究工具、阶段续答和附件",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def deduplicate(
        self, session_id: str = "", output_items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """只对同时选择两个最终发送入口的同一批输出去重。"""
        turn = self._turns.get(session_id)
        if turn is None or item_schema_version != 1 or not isinstance(output_items, list):
            return {"action": "continue"}
        redundant, direct_answer = [], False
        for item in output_items:
            call = item.get("tool_call") if isinstance(item, Mapping) and item.get("item_type") == "FunctionCallItem" else None
            if not isinstance(call, Mapping):
                continue
            args = call.get("args")
            if not isinstance(args, Mapping) or args.get("msg_id") != turn.message_id:
                continue
            text = args.get("text")
            if (call.get("func_name") == "send_reply" and args.get("phase", "answer") == "answer"
                    and isinstance(text, str) and 0 < len(text.strip()) <= 2000):
                direct_answer = True
            elif call.get("func_name") == "reply" and not any(args.get(name) for name in ("attach_pic", "attach_emoji", "attach_at")):
                redundant.append(item)
        if not direct_answer or not redundant:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "item_schema_version": item_schema_version,
            "output_items": [item for item in output_items if item not in redundant],
        }}


def create_plugin() -> QQBotReplyGuardPlugin:
    """返回由公开 Runner 管理的插件。"""
    return QQBotReplyGuardPlugin()
