"""将真实聊天会话标识交给 CPA，生成仍使用 MaiBot 现有 Responses 客户端。"""

from collections import OrderedDict
from dataclasses import asdict, replace
from typing import Any, ClassVar, cast
from urllib.parse import quote
from uuid import uuid4

from maibot_sdk import Field, HookHandler, LLMProvider, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

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

    async def on_load(self) -> None:
        """报告本地 Provider 已就绪，不在加载时调用模型。"""
        self.ctx.logger.info("CPA 会话透传已加载：client_type=cpa_responses")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """SDK 更新插件配置后报告版本，不维护额外配置副本。"""
        del scope, config_data
        self.ctx.logger.info("CPA 会话透传配置已更新：version=%s", version)

    @HookHandler("maisaka.planner.before_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def planner_session(self, session_id: str = "", items: Any = None, **_kwargs: Any) -> dict:
        """登记 Planner 的真实会话，不改动提示词或 Context Items。"""
        self._remember_session(session_id, items, False)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_model_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def replyer_session(self, session_id: str = "", items: Any = None, **_kwargs: Any) -> dict:
        """登记 Replyer 已构造请求的真实会话。"""
        self._remember_session(session_id, items, True)
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

    @LLMProvider("cpa_responses", name="CPA 会话透传", version="0.1.0")
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
            if key.lower() not in {"x-client-request-id", "user-agent"}
        }
        headers.update({
            "X-Client-Request-Id": quote(session_id, safe=":"),
            "User-Agent": "QQBot/MaiBot-cpa-session/0.1.0",
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
        try:
            response = await client.get_response(native_request)
            usages = [asdict(response.usage)] if response.usage is not None else []
            # 原生 Replyer 仅对内置 client_type 续写；在适配器中保留同一模型续写一次的行为。
            if replyer and any(isinstance(item, ReasoningItem) for item in response.output_items) and not (response.content or "").strip():
                native_request.context_items.extend(response.output_items)
                response = await client.get_response(native_request)
                if response.usage is not None:
                    usages.append(asdict(response.usage))
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
        self._sessions.clear()
        self._replay.clear()


def create_plugin() -> MaiBotPlugin:
    """由 Runner 通过公开 SDK 加载本地 Provider 插件。"""
    return CPASessionPlugin()
