"""将真实聊天会话标识交给 CPA，生成仍使用 MaiBot 现有 Responses 客户端。"""

from collections import OrderedDict
from dataclasses import asdict, replace
from typing import Any, ClassVar, cast
from urllib.parse import quote
from uuid import uuid4

import asyncio

from maibot_sdk import API, Field, HookHandler, LLMProvider, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from src.config.model_configs import APIProvider
from src.llm_models.model_client.base_client import ResponseRequest
from src.llm_models.model_client.openai_responses_client import OpenAIResponsesClient
from src.llm_models.payload_content.context_item import ContextItem, ReasoningItem
from src.llm_models.request_snapshot import (
    deserialize_context_items_snapshot,
    deserialize_model_info_snapshot,
    deserialize_response_format_snapshot,
    deserialize_tool_options_snapshot,
    serialize_context_item_snapshot,
    serialize_context_items_snapshot,
)

from .interruption import ReplyInterruption, ReplyTurn


class PluginSection(PluginConfigBase):
    """CPA 会话透传插件的启用配置。"""

    enabled: bool = Field(default=True, description="启用 CPA 会话透传")
    config_version: str = Field(default="0.1.0", description="配置版本")


class SessionConfig(PluginConfigBase):
    """只声明插件自身配置，地址、密钥和生成参数沿用模型配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)


class CPASessionPlugin(MaiBotPlugin):
    """通过请求 Hook 关联会话，复用原生客户端完成模型调用。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = SessionConfig
    CACHE_LIMIT = 4096

    def __init__(self) -> None:
        """仅保存有界的请求关联与原生回放项，不保存聊天或凭据文件。"""
        super().__init__()
        self._sessions: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        self._replay: OrderedDict[str, ContextItem] = OrderedDict()
        self._interruption = ReplyInterruption()

    async def on_load(self) -> None:
        """报告本地 Provider 已就绪，不在加载时调用模型。"""
        self.ctx.logger.info("CPA 会话透传已加载：client_type=cpa_responses")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """SDK 更新插件配置后报告版本，不维护额外配置副本。"""
        del scope, config_data
        self.ctx.logger.info("CPA 会话透传配置已更新：version=%s", version)

    @HookHandler("chat.receive.after_process", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def receive(self, message: Any = None, **_kwargs: Any) -> dict:
        """新消息只更新本地轮次并取消过期请求，不向框架重复投递消息。"""
        if isinstance(message, dict):
            self._interruption.observe(message)
        return {"action": "continue"}

    @HookHandler("maisaka.planner.after_response", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def discard_old_plan(self, session_id: str = "", **kwargs: Any) -> dict:
        """模型返回与新消息交错时，不执行旧快照产生的工具调用。"""
        turn = self._interruption.turns.get(session_id)
        if turn is not None and turn.stale:
            await self._resume_cancelled(session_id, turn)
            self.ctx.logger.info("新补充已取代旧规划，已请求原生循环重新处理")
            return {"action": "continue", "modified_kwargs": {**kwargs, "session_id": session_id, "output_items": []}}
        return {"action": "continue"}

    @HookHandler("maisaka.reply.before_post_process", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def discard_old_reply(self, session_id: str = "", **kwargs: Any) -> dict:
        """尚未发出首条时，过期正文不进入分段和发送。"""
        turn = self._interruption.reply_turns.get(session_id)
        if turn is not None and turn.stale:
            await self._resume_cancelled(session_id, turn)
            self.ctx.logger.info("新补充已取代旧正文，已请求原生回复器重写")
            return {"action": "continue", "modified_kwargs": {**kwargs, "session_id": session_id, "response": ""}}
        return {"action": "continue"}

    @HookHandler("send_service.before_send", mode=HookMode.BLOCKING, order=HookOrder.EARLY, error_policy=ErrorPolicy.ABORT)
    async def check_before_send(self, message: Any = None, **_kwargs: Any) -> dict:
        """只拦截尚未公开答复的旧轮次，已经发出首段的回复继续发送。"""
        session_id = message.get("session_id") if isinstance(message, dict) else None
        turn = self._interruption.reply_turns.get(session_id)
        if turn is not None and turn.stale:
            await self._resume_cancelled(session_id, turn)
            return {"action": "abort"}
        return {"action": "continue"}

    @HookHandler("send_service.after_send", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def mark_first_sent(self, message: Any = None, sent: bool = False, **_kwargs: Any) -> dict:
        """只有明确成功回执才关闭本轮取消资格，失败不冒充已发。"""
        session_id = message.get("session_id") if isinstance(message, dict) else None
        turn = self._interruption.reply_turns.get(session_id)
        if sent and turn is not None:
            turn.first_sent = True
        return {"action": "continue"}

    @API("resume_cancelled_reply", description="重新排队被同发送者补充取消的待发送回复", version="1", public=True)
    async def resume_cancelled_reply(self, session_id: str, **_kwargs: Any) -> dict:
        """供首条等待插件复用同一个轮次的排队资格。"""
        turn = self._interruption.reply_turns.get(session_id)
        if turn is None or not turn.stale:
            return {"queued": False}
        await self._resume_cancelled(session_id, turn)
        return {"queued": turn.resume_queued}

    async def _resume_cancelled(self, session_id: str | None, turn: ReplyTurn) -> None:
        """每个被补充取代的轮次只排队一次，绕过空响应引起的空闲等待。"""
        if not isinstance(session_id, str) or not session_id or turn.resume_queued or self._interruption.closing:
            return
        turn.resume_queued = True
        try:
            async with asyncio.timeout(5):
                result = await self.ctx.maisaka.trigger_proactive(
                    session_id,
                    "上一轮答复因同一发送者补充消息而取消，请结合原会话完整上下文重新处理尚未回答的呼叫和补充。",
                    reason="新补充取代尚未发送的回复",
                    metadata={"source_message_id": turn.source_id, "latest_message_id": turn.received[-1]},
                )
            if not isinstance(result, dict) or result.get("queued") is not True:
                raise RuntimeError("框架未确认重新排队")
        except Exception as exc:
            turn.resume_queued = False
            self.ctx.logger.error("取消后重新排队失败: session_id=%s error=%s", session_id, exc)

    @HookHandler("maisaka.planner.before_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def planner_session(
        self, session_id: str = "", items: Any = None, tool_definitions: Any = None, **_kwargs: Any,
    ) -> dict:
        """登记真实会话；只有带原生回复工具的主规划建立回复轮次。"""
        self._remember_session(session_id, items, False)
        if isinstance(tool_definitions, list) and any(
            isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == "reply" for tool in tool_definitions
        ):
            self._interruption.bind(session_id, items, False)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_model_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def replyer_session(self, session_id: str = "", items: Any = None, **_kwargs: Any) -> dict:
        """登记 Replyer 已构造请求的真实会话。"""
        self._remember_session(session_id, items, True)
        self._interruption.bind(session_id, items, True)
        return {"action": "continue"}

    def _remember_session(self, session_id: str, items: Any, replyer: bool) -> None:
        """按框架分配的 Item ID 关联会话；不从正文或昵称推断身份。"""
        if not session_id or not isinstance(items, list):
            raise ValueError("CPA 会话透传需要 Hook 提供 session_id 和 items")
        for item in items:
            item_id = item["meta"]["item_id"]
            self._sessions[item_id] = (session_id, replyer)
            self._sessions.move_to_end(item_id)
        while len(self._sessions) > self.CACHE_LIMIT:
            self._sessions.popitem(last=False)

    @LLMProvider("cpa_responses", name="CPA 会话透传", version="0.1.1")
    async def generate(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """还原公开请求类型，加会话头后直接交给原生 Responses 客户端。"""
        if operation != "response" or request["item_schema_version"] != 1:
            raise ValueError("CPA 会话 Provider 只接收 Context Item schema 1 的生成请求")
        items = deserialize_context_items_snapshot(request["context_items"])
        session = next(
            (self._sessions[item.meta.item_id] for item in reversed(items) if item.meta.item_id in self._sessions),
            None,
        )
        session_id, replyer = session if session is not None else (request.get("logical_turn_id") or uuid4().hex, False)

        # RPC 快照不携带 replay。相同 Item 未经 Hook 改写时复用本插件的原生回放项。
        for index, item in enumerate(items):
            cached = self._replay.get(item.meta.item_id)
            if cached is not None:
                before = serialize_context_item_snapshot(cached)
                current = serialize_context_item_snapshot(item)
                before.pop("meta")
                current.pop("meta")
                if before == current:
                    items[index] = replace(cached, meta=item.meta)
                    self._replay.move_to_end(item.meta.item_id)

        extra = dict(request["extra_params"])
        headers = {
            key: value for key, value in (extra.get("headers") or {}).items()
            if key.lower() not in {"x-client-request-id", "x-opencode-session", "user-agent"}
        }
        headers.update({
            "x-opencode-session": quote(session_id, safe=":"),
            "User-Agent": "QQBot/MaiBot-cpa-session/0.1.1",
        })
        extra["headers"] = headers
        native_request = ResponseRequest(
            model_info=deserialize_model_info_snapshot(request["model_info"]),
            context_items=items,
            tool_options=deserialize_tool_options_snapshot(request["tool_options"]),
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            response_format=deserialize_response_format_snapshot(request["response_format"]),
            extra_params=extra,
            logical_turn_id=request.get("logical_turn_id") or uuid4().hex,
        )
        client = OpenAIResponsesClient(APIProvider.model_validate(request["api_provider"]))
        turn = (self._interruption.reply_turns if replyer else self._interruption.turns).get(session_id)
        try:
            usages = []
            # 保留原生 Replyer 仅有推理时续写一次的行为，两次请求均可被新消息取代。
            for attempt in range(2):
                response = await self._interruption.request(client, native_request, turn)
                if response is None:
                    await self._resume_cancelled(session_id, cast(ReplyTurn, turn))
                    self.ctx.logger.info("同发送者的新补充已取消旧模型请求，已请求重新处理")
                    return {"item_schema_version": 1, "output_items": [], "status": "cancelled",
                            "usage": None, "raw_data": {"cancelled_by_new_message": True}}
                if response.usage is not None:
                    usages.append(asdict(response.usage))
                if (attempt or not replyer or not any(isinstance(item, ReasoningItem) for item in response.output_items)
                        or (response.content or "").strip()):
                    break
                native_request.context_items.extend(response.output_items)
            for item in response.output_items:
                self._replay[item.meta.item_id] = item
            while len(self._replay) > self.CACHE_LIMIT:
                self._replay.popitem(last=False)
            return {
                "item_schema_version": 1,
                "output_items": serialize_context_items_snapshot(response.output_items),
                "response_id": response.generation_trace.response_id if response.generation_trace else None,
                "status": response.generation_trace.status if response.generation_trace else "completed",
                "usage": {
                    key: sum(usage[key] for usage in usages)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                } if usages else None,
                "raw_data": response.provider_response,
            }
        finally:
            await cast(Any, client).client.close()

    async def on_unload(self) -> None:
        """释放本插件的会话关联及原生回放项。"""
        await self._interruption.close()
        self._sessions.clear()
        self._replay.clear()


def create_plugin() -> MaiBotPlugin:
    """由 Runner 通过公开 SDK 加载本地 Provider 插件。"""
    return CPASessionPlugin()
