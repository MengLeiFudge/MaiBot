from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping
from urllib.request import Request, build_opener

import asyncio
import base64
import binascii
import re
import secrets
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .draw_input import DOUDOUYAN_AVATAR_COMMAND, STANDARD_DRAW_PREFIX, doudouyan_command_parts, prepare_explicit_draw_request, preload_draw_references
from .draw_output import save_generated_image
from .draw_intent import DRAW_INTENT_SYSTEM, DrawIntent, DrawIntentRouter
from .draw_logic import RightCodesDrawClient
from .draw_logic import RightCodesDrawQuotaResult
from .draw_logic import RightCodesDrawQuotaStore
from .draw_logic import RightCodesDrawRequest
from .draw_logic import RightCodesDrawTimeoutError
from .draw_logic import extract_removed_rightcodes_draw_temporary_model
from .draw_logic import format_draw_quota_exceeded_message
from .draw_logic import format_draw_start_message
from .draw_logic import format_rightcodes_draw_failure
from .draw_logic import format_rightcodes_draw_missing_prompt_message
from .draw_logic import format_rightcodes_draw_model_help
from .draw_logic import format_rightcodes_draw_model_switch_invalid
from .draw_logic import format_rightcodes_draw_model_switch_success
from .draw_logic import format_rightcodes_draw_points_mutation_denied
from .draw_logic import format_rightcodes_draw_points_ranking
from .draw_logic import format_rightcodes_draw_points_status
from .draw_logic import format_rightcodes_draw_success
from .draw_logic import format_rightcodes_draw_temporary_model_removed
from .draw_logic import format_rightcodes_draw_timeout
from .draw_logic import looks_like_rightcodes_draw_help_command
from .draw_logic import looks_like_rightcodes_draw_invocation
from .draw_logic import looks_like_rightcodes_draw_points_mutation_request
from .draw_logic import looks_like_rightcodes_draw_points_query
from .draw_logic import looks_like_rightcodes_draw_points_ranking
from .draw_logic import parse_rightcodes_draw_command
from .draw_logic import parse_rightcodes_draw_model_switch
from .process_lock import InterProcessLock
from .rightcodes_catalog import extract_current_query
from .rightcodes_catalog import inject_catalog_into_messages
from .rightcodes_catalog import should_inject_draw_catalog
from .rightcodes_rewrite import RIGHTCODES_DRAW_REWRITE_SYSTEM_PROMPT
from .rightcodes_rewrite import DrawRewriteInput
from .rightcodes_rewrite import build_draw_rewrite_prompt
from .rightcodes_rewrite import format_draw_rewrite_failure
from .rightcodes_rewrite import format_draw_rewrite_missing_context
from .rightcodes_rewrite import parse_draw_rewrite_response
from .rightcodes_rewrite import should_rewrite_draw_prompt


DRAW_COMMAND_PATTERN = (
    r"^(?:@\S+\s*)?(?:"
    r"(?:文生图|图生图|头像生图|(?:棉花糖|棉花)\s*生图|生成)[\s\S]*|"
    r"(?:查|查询|查看|看)?(?:一下)?(?:我(?:的)?|当前)?(?:生图)?积分(?:余额|情况|多少)?|"
    r"(?:balance|points?)|积分排行(?:榜)?|"
    r"(?:生图|画图|棉花糖生图|棉花生图)(?:模型说明|模型|价格)|"
    r"切换\s*生图\s*模型[\s\S]*|生图\s*模型\s+\S+[\s\S]*|"
    r"(?:(?:加|增加|扣|扣除|减|减少|改|修改|设置|设定|送|赠|赠送|充值|充)[\s\S]*积分|"
    r"积分[\s\S]*(?:加|增加|扣|扣除|减|减少|改|修改|设置|设定|送|赠|赠送|充值|充))"
    r")$"
)
_IMAGE_SUMMARIES = ("给你看看", "这张完成啦", "新图送到", "画好啦", "成品来了")
_MAX_REFERENCE_IMAGES = 3
_MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
_REFERENCE_IMAGE_TIMEOUT_SECONDS = 15.0
_REWRITE_TIMEOUT_SECONDS = 30.0
_MEDIA_PLACEHOLDERS = ("[image]", "[图片]", "[emoji]", "[表情]", "[截图]")


class PluginSection(PluginConfigBase):
    """生图插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册生图固定命令插件")
    config_version: str = Field(default="0.2.0", description="配置版本")


class CutoverSection(PluginConfigBase):
    """生图与积分单写入接管门禁。"""

    __ui_label__ = "迁移接管"
    __ui_order__ = 1

    write_enabled: bool = Field(default=True, description="AstrBot 已停用，默认由 MaiBot 接管生图和积分写入")


class StorageSection(PluginConfigBase):
    """生图存储配置。"""

    __ui_label__ = "存储"
    __ui_order__ = 2

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共插件的数据根")


class RightCodesSection(PluginConfigBase):
    """CPA 生图入口与积分价格配置；保留原配置节名。"""

    __ui_label__ = "CPA 生图"
    __ui_order__ = 3

    api_key: str = Field(
        default="",
        description="CPA 客户端 API Key",
        json_schema_extra={"x-widget": "password", "x-icon": "key", "label": "API Key", "order": 0},
    )
    base_url: str = Field(default="http://127.0.0.1:8317/v1", description="CPA API 地址，包含 /v1")
    point_multiplier: int = Field(default=1000, ge=1, le=1_000_000, description="人民币价格换算积分倍率")
    draw_timeout_seconds: int = Field(default=240, ge=30, le=900, description="单次生图总超时秒数")


class PointsSection(PluginConfigBase):
    """群消息积分累计配置。"""

    __ui_label__ = "积分"
    __ui_order__ = 4

    owner_self_id: str = Field(default="3056830689", description="唯一负责普通群消息积分累计的机器人 QQ")
    bot_account_ids: list[str] = Field(default_factory=list, description="不参与积分累计的机器人 QQ 列表")


class DrawConfig(PluginConfigBase):
    """生图插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    cutover: CutoverSection = Field(default_factory=CutoverSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    rightcodes: RightCodesSection = Field(default_factory=RightCodesSection)
    points: PointsSection = Field(default_factory=PointsSection)


class QQBotDrawPlugin(MaiBotPlugin):
    """确定性处理 RightCodes 生图、积分和模型选择。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = DrawConfig

    async def on_load(self) -> None:
        """记录插件接管状态，不在日志中输出密钥。"""

        self._draw_intent_router = DrawIntentRouter()
        self.ctx.logger.info(
            "QQBot 生图插件已加载，业务写入=%s",
            self.config.cutover.write_enabled,
        )

    async def on_unload(self) -> None:
        """生图插件没有常驻 HTTP 会话。"""

        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """记录插件配置热更新。"""

        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot 生图配置已更新: %s", version)

    @Command("qqbot_draw", description="RightCodes 生图与积分入口", pattern=DRAW_COMMAND_PATTERN)
    async def handle_draw_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """按唯一执行者规则处理一次生图相关命令。"""

        del kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        facts = _message_facts(message, command_text, group_id, user_id)
        if not facts["self_id"]:
            response = "生图命令暂时无法确认当前机器人身份。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts)
        except Exception:
            response = "生图命令仲裁失败，请稍后重试。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "生图功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not self.config.cutover.write_enabled:
            response = "生图功能接管当前已关闭。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        runtime_root = await self._runtime_root()
        response = await self._handle_owned_command(
            command_text=command_text,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            message=message,
            runtime_root=runtime_root,
        )
        return True, response, True

    @HookHandler(
        "chat.receive.after_process",
        name="qqbot_draw_natural_intent",
        description="把明确唤醒后的自然语言请求归一到文生图或图生图",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1020000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_draw_intent(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        """只对启用且有唯一归属的自然语言请求分类，普通聊天继续原链路。"""
        del kwargs
        if not self.config.plugin.enabled or not self.config.cutover.write_enabled or not isinstance(message, Mapping):
            return {"action": "continue"}
        text = _text_segments(message)
        if re.fullmatch(DRAW_COMMAND_PATTERN, text):
            return {"action": "continue"}
        group_id, user_id = _message_scope(message)
        facts = _message_facts(message, text, group_id, user_id)
        self_id = facts["self_id"]
        stream_id = str(message.get("session_id") or "")
        if not self_id or not user_id or not stream_id or user_id == self_id or user_id in self.config.points.bot_account_ids:
            return {"action": "continue"}
        if group_id and not (message.get("is_at") or message.get("is_mentioned") or self_id in facts["at_target_ids"]):
            return {"action": "continue"}

        async def classify_intent(prompt: str) -> str:
            """通过公开LLM接口选用当前replyer任务，不带聊天历史。"""
            result = await self.ctx.llm.generate(
                prompt=[{"role": "system", "content": DRAW_INTENT_SYSTEM}, {"role": "user", "content": prompt}],
                model="replyer", temperature=0, max_tokens=1200,
            )
            if not isinstance(result, Mapping) or not result.get("success", True):
                raise ValueError("意图模型调用失败")
            self.ctx.logger.info("生图意图分类: task=replyer model=%s", result.get("model_name", "unknown"))
            return str(result.get("response") or result.get("content") or "")

        try:
            intent = await self._draw_intent_router.resolve(
                _message_parts(message), sender_id=user_id, self_id=self_id,
                call_action=self._call_image_action, classify=classify_intent,
            )
        except Exception as exc:
            self.ctx.logger.warning("生图意图解析不可用: error_type=%s", type(exc).__name__)
            return {"action": "continue"}
        if intent is None or intent.action == "none":
            return {"action": "continue"}
        if intent.action == "clarify":
            await self.ctx.send.text(f"{intent.clarification} 本次没有扣积分。", stream_id)
            return {"action": "abort"}
        if not await self._claim(text, facts):
            return {"action": "continue"}
        prepared_message = dict(message)
        prepared_message["raw_message"] = intent.command_parts()
        runtime_root = await self._runtime_root()
        await self._run_draw(RightCodesDrawRequest(prompt=intent.prompt), stream_id, group_id, user_id,
                             prepared_message, runtime_root, intent=intent)
        return {"action": "abort"}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="qqbot_draw_catalog_injection",
        description="按当前问题临时注入 RightCodes 官方接口知识",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_rightcodes_catalog(
        self,
        messages: object = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        """仅在 replyer 当前目标问题命中时改写本次模型消息。"""

        if not self.config.plugin.enabled:
            return {"action": "continue"}
        query = extract_current_query(messages)
        if not should_inject_draw_catalog(query):
            return {"action": "continue"}
        modified_messages = inject_catalog_into_messages(messages)
        if modified_messages is None:
            return {"action": "continue"}
        modified_kwargs = dict(kwargs)
        modified_kwargs["messages"] = modified_messages
        self.ctx.logger.info("已为当前 replyer 请求注入 RightCodes 生图接口知识")
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @HookHandler(
        "chat.receive.after_process",
        name="qqbot_draw_group_points",
        description="由固定 MaiBot 账号累计普通群消息生图积分",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def record_group_points(
        self,
        message: object = None,
        **kwargs: Any,
    ) -> None:
        """每条符合条件的群消息只在一个 MaiBot 实例中累计一次。"""

        del kwargs
        if not self.config.cutover.write_enabled:
            return None
        group_id, user_id = _message_scope(message)
        if not group_id or not user_id:
            return None
        facts = _message_facts(message, "", group_id, user_id)
        if facts["self_id"] != self.config.points.owner_self_id:
            return None
        if user_id in self.config.points.bot_account_ids:
            return None
        runtime_root = await self._runtime_root()
        await asyncio.to_thread(self._record_points_locked, runtime_root, user_id)
        return None

    async def _handle_owned_command(
        self,
        *,
        command_text: str,
        stream_id: str,
        group_id: str,
        user_id: str,
        message: object,
        runtime_root: Path,
    ) -> str:
        if not STANDARD_DRAW_PREFIX.match(command_text) and looks_like_rightcodes_draw_points_mutation_request(command_text):
            response = format_rightcodes_draw_points_mutation_denied()
            await self.ctx.send.text(response, stream_id)
            return response

        if looks_like_rightcodes_draw_points_query(command_text):
            balance = await asyncio.to_thread(self._get_balance_locked, runtime_root, user_id)
            response = format_rightcodes_draw_points_status(balance)
            await self.ctx.send.text(response, stream_id)
            return response

        if looks_like_rightcodes_draw_points_ranking(command_text):
            ranking = await asyncio.to_thread(self._get_ranking_locked, runtime_root)
            names: dict[str, str] = {}
            for balance in ranking:
                names[balance.user_id] = await self._display_name(group_id, balance.user_id)
            response = format_rightcodes_draw_points_ranking(
                ranking,
                resolve_display_name=lambda target_id: names.get(target_id, target_id),
            )
            await self.ctx.send.text(response, stream_id)
            return response

        if looks_like_rightcodes_draw_help_command(command_text):
            balance = await asyncio.to_thread(self._get_balance_locked, runtime_root, user_id)
            response = format_rightcodes_draw_model_help(
                balance.model,
                multiplier=self.config.rightcodes.point_multiplier,
            )
            await self.ctx.send.text(response, stream_id)
            return response

        if re.match(r"^(?:切换\s*生图\s*模型|生图\s*模型\s+)", command_text):
            model = parse_rightcodes_draw_model_switch(command_text)
            if model is None:
                candidate = re.sub(r"^(?:切换\s*生图\s*模型|生图\s*模型)\s*", "", command_text).strip()
                response = format_rightcodes_draw_model_switch_invalid(candidate)
            else:
                balance = await asyncio.to_thread(self._set_model_locked, runtime_root, user_id, model)
                response = format_rightcodes_draw_model_switch_success(balance)
            await self.ctx.send.text(response, stream_id)
            return response

        if not looks_like_rightcodes_draw_invocation(command_text):
            raise ValueError(f"未识别的生图命令: {command_text}")
        removed_model = extract_removed_rightcodes_draw_temporary_model(command_text)
        if removed_model is not None:
            response = format_rightcodes_draw_temporary_model_removed(removed_model)
            await self.ctx.send.text(response, stream_id)
            return response
        request = parse_rightcodes_draw_command(command_text)
        if request is None:
            response = format_rightcodes_draw_missing_prompt_message()
            await self.ctx.send.text(response, stream_id)
            return response
        if (STANDARD_DRAW_PREFIX.match(command_text) and not STANDARD_DRAW_PREFIX.match(_text_segments(message))) or (
            command_text == DOUDOUYAN_AVATAR_COMMAND and _text_segments(message) != DOUDOUYAN_AVATAR_COMMAND
        ):
            response = "无法读取这条生图指令的原始消息组件，本次没有扣积分，请重新发送。"
            await self.ctx.send.text(response, stream_id)
            return response
        return await self._run_draw(request, stream_id, group_id, user_id, message, runtime_root)

    async def _run_draw(
        self,
        request: RightCodesDrawRequest,
        stream_id: str,
        group_id: str,
        user_id: str,
        message: object,
        runtime_root: Path,
        *,
        intent: DrawIntent | None = None,
    ) -> str:
        balance = await asyncio.to_thread(self._get_balance_locked, runtime_root, user_id)
        preparation_error = ""
        try:
            parts = _message_parts(message)
            preset_parts = await doudouyan_command_parts(parts, call_action=self._call_image_action)
            if STANDARD_DRAW_PREFIX.match(_text_segments(message)) or preset_parts is not None:
                prepared_request = await prepare_explicit_draw_request(
                    preset_parts if preset_parts is not None else parts,
                    sender_id=user_id, model=balance.model, call_action=self._call_image_action,
                )
            else:
                prepared_request, preparation_error = await self._prepare_draw_request(
                    request=RightCodesDrawRequest(prompt=request.prompt, model=balance.model),
                    message=message,
                )
                if not preparation_error:
                    prepared_request = await preload_draw_references(prepared_request)
        except Exception as exc:
            self.ctx.logger.warning("生图输入准备失败: error_type=%s", type(exc).__name__)
            detail = str(exc) if isinstance(exc, ValueError) else "原图读取失败或超时，请重新附图或引用"
            preparation_error = f"{detail}。本次没有扣积分。"
        if preparation_error:
            await self._send_quoted_result(
                group_id=group_id,
                user_id=user_id,
                message_id=_message_id(message),
                text=preparation_error,
                image_url="",
            )
            return preparation_error

        quota = await asyncio.to_thread(self._reserve_locked, runtime_root, user_id, balance.model)
        if not quota.allowed:
            response = format_draw_quota_exceeded_message(quota)
            await self.ctx.send.text(response, stream_id)
            return response

        try:
            async with asyncio.timeout(30):
                start_message = format_draw_start_message(quota)
                start_message += ("\n提示词来源：豆豆眼预设（完整固定原文）" if preset_parts is not None
                                  else "\n提示词来源：用户话语（无预设）")
                if preset_parts is not None:
                    start_message += "\n本次按固定原文生成，不追加其他描述。"
                    if intent is not None and intent.source is not None:
                        start_message += f"\n本次来源：{intent.source.label}"
                    elif _text_segments({"raw_message": preset_parts}).startswith("头像生图"):
                        start_message += "\n本次来源：头像生图指定的头像"
                    else:
                        target = "当前附图" if any(part.get("type") == "image" for part in preset_parts) else "引用图片"
                        start_message += f"\n本次来源：{target}"
                elif intent is not None:
                    start_message += intent.start_detail()
                await self.ctx.send.text(start_message, stream_id)
            client = RightCodesDrawClient(
                api_key=self.config.rightcodes.api_key,
                base_url=self.config.rightcodes.base_url,
                timeout_seconds=float(self.config.rightcodes.draw_timeout_seconds),
            )
            result = await client.draw(prepared_request)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self._refund_locked, runtime_root, quota))
            raise
        except Exception as exc:
            await asyncio.to_thread(self._refund_locked, runtime_root, quota)
            if isinstance(exc, RightCodesDrawTimeoutError):
                response = format_rightcodes_draw_timeout(exc.timeout_seconds)
            else:
                response = format_rightcodes_draw_failure(exc)
            await self._send_quoted_result(
                group_id=group_id,
                user_id=user_id,
                message_id=_message_id(message),
                text=response,
                image_url="",
            )
            return response

        saved = None
        try:
            saved = await asyncio.to_thread(save_generated_image, result.image_bytes, runtime_root)
        except Exception as exc:
            self.ctx.logger.error("生图结果保存失败: error_type=%s", type(exc).__name__)
        response = format_rightcodes_draw_success(result, model=quota.model)
        if saved is not None:
            source = saved.resolve().as_uri()
        else:
            response += "\n图片未保存到本地，已尝试直接交付，请及时保存。"
            source = f"base64://{base64.b64encode(result.image_bytes).decode('ascii')}"
        try:
            await self._send_quoted_result(
                group_id=group_id, user_id=user_id, message_id=_message_id(message),
                text=response, image_url=source,
            )
            return response
        except Exception as exc:
            self.ctx.logger.warning("生图发送失败: error_type=%s saved=%s", type(exc).__name__, saved)
        if saved is None:
            try:
                await asyncio.shield(asyncio.to_thread(self._refund_locked, runtime_root, quota))
                response = "图片已生成，但保存和发送均失败，本次积分已退回。"
            except Exception as exc:
                self.ctx.logger.error("生图积分退款失败: error_type=%s", type(exc).__name__)
                response = "图片已生成，但保存和发送均失败；积分退款失败，请联系管理员处理。"
        else:
            response = f"图片已生成并保存，但发送失败。请联系管理员取回：{saved.name}。本次未退积分。"
        try:
            await self._send_quoted_result(
                group_id=group_id, user_id=user_id, message_id="", text=response, image_url="",
            )
        except Exception as exc:
            self.ctx.logger.warning("生图失败提示发送失败: error_type=%s", type(exc).__name__)
        return response

    async def _call_image_action(self, action: str, **params: object) -> object:
        """通过 NapCat 公开插件 API 读取图片来源，并解开 OneBot 响应。"""
        if action == "get_msg":
            result = await self.ctx.api.call("adapter.napcat.message.get_msg", **params)
        elif action == "get_image":
            result = await self.ctx.api.call("adapter.napcat.file.get_image", params=params)
        else:
            raise ValueError("不支持的图片读取动作")
        payload = require_api_result(result, "读取图片来源")
        if isinstance(payload, Mapping) and ("status" in payload or "retcode" in payload):
            return payload.get("data")
        return payload

    async def _prepare_draw_request(
        self,
        *,
        request: RightCodesDrawRequest,
        message: object,
    ) -> tuple[RightCodesDrawRequest, str]:
        """收集当前/引用上下文，并在扣分前按需调用当前 replyer 模型整理提示词。"""

        reply_texts = list(_reply_preview_texts(message))
        image_sources, unresolved_media_context = _message_image_sources(message)
        for reply_message_id in _reply_message_ids(message):
            try:
                result = await self.ctx.api.call(
                    "adapter.napcat.message.get_msg",
                    message_id=reply_message_id,
                )
                detail = require_api_result(result, "读取被引用消息")
            except Exception as exc:
                self.ctx.logger.warning("读取生图引用消息失败: error_type=%s", type(exc).__name__)
                unresolved_media_context = unresolved_media_context or any(
                    _contains_media_placeholder(text) for text in reply_texts
                )
                continue
            if not isinstance(detail, Mapping):
                unresolved_media_context = True
                continue
            detail_text = _text_segments(detail)
            if detail_text and not _is_only_media_placeholder(detail_text) and detail_text not in reply_texts:
                reply_texts.append(detail_text)
            detail_sources, detail_unresolved = _message_image_sources(detail)
            for source in detail_sources:
                if source not in image_sources:
                    image_sources.append(source)
            unresolved_media_context = unresolved_media_context or detail_unresolved

        reference_images: list[str] = []
        for source in image_sources[:_MAX_REFERENCE_IMAGES]:
            try:
                normalized = await _normalize_reference_image(source)
            except Exception as exc:
                self.ctx.logger.warning("读取生图参考图失败: error_type=%s", type(exc).__name__)
                unresolved_media_context = True
                continue
            if normalized and normalized not in reference_images:
                reference_images.append(normalized)

        prepared = RightCodesDrawRequest(
            prompt=request.prompt,
            model=request.model,
            image_urls=tuple(reference_images),
        )
        normalized_reply_texts = tuple(text for text in reply_texts if text and not _is_only_media_placeholder(text))
        if unresolved_media_context and not reference_images and not normalized_reply_texts:
            return prepared, format_draw_rewrite_missing_context()
        if not should_rewrite_draw_prompt(
            prepared.prompt,
            reply_texts=normalized_reply_texts,
            reference_image_count=len(reference_images),
        ):
            return prepared, ""

        rewrite_prompt = build_draw_rewrite_prompt(
            DrawRewriteInput(
                prompt=prepared.prompt,
                model=prepared.model,
                current_text=_text_segments(message),
                reply_texts=normalized_reply_texts,
                reference_image_count=len(reference_images),
                unresolved_media_context=unresolved_media_context,
            )
        )
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=[
                        {"role": "system", "content": RIGHTCODES_DRAW_REWRITE_SYSTEM_PROMPT},
                        {"role": "user", "content": rewrite_prompt},
                    ],
                    model="replyer",
                    temperature=0.2,
                    max_tokens=600,
                ),
                timeout=_REWRITE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self.ctx.logger.warning("生图提示词整理调用失败: error_type=%s", type(exc).__name__)
            return prepared, format_draw_rewrite_failure()
        if not isinstance(result, Mapping) or not bool(result.get("success", True)):
            return prepared, format_draw_rewrite_failure()
        rewritten_prompt = parse_draw_rewrite_response(
            str(result.get("response") or result.get("content") or "")
        )
        if not rewritten_prompt:
            if unresolved_media_context and not reference_images:
                return prepared, format_draw_rewrite_missing_context()
            return prepared, format_draw_rewrite_failure()
        self.ctx.logger.info(
            "生图提示词已在扣分前整理: images=%s prompt_chars=%s",
            len(reference_images),
            len(rewritten_prompt),
        )
        return RightCodesDrawRequest(
            prompt=rewritten_prompt,
            model=prepared.model,
            image_urls=prepared.image_urls,
        ), ""

    async def _send_quoted_result(
        self,
        *,
        group_id: str,
        user_id: str,
        message_id: str,
        text: str,
        image_url: str,
    ) -> None:
        segments: list[dict[str, object]] = []
        if message_id:
            segments.append({"type": "reply", "data": {"id": message_id}})
        segments.append({"type": "text", "data": {"text": text}})
        if image_url:
            if image_url.startswith("data:image/"):
                image_url = f"base64://{image_url.partition(',')[2]}"
            segments.append(
                {
                    "type": "image",
                    "data": {"file": image_url, "summary": secrets.choice(_IMAGE_SUMMARIES)},
                }
            )
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": segments}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": segments}
        async with asyncio.timeout(60):
            result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送生图结果")

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="draw",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "生图命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("生图命令仲裁返回格式无效")
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

    async def _display_name(self, group_id: str, user_id: str) -> str:
        result = await self.ctx.api.call("qqbot.identity.display_name", group_id=group_id, user_id=user_id)
        payload = require_api_result(result, "解析积分排行显示名")
        return str(payload.get("display_name") or user_id) if isinstance(payload, Mapping) else user_id

    def _quota_store(self, runtime_root: Path) -> RightCodesDrawQuotaStore:
        return RightCodesDrawQuotaStore(
            runtime_root,
            multiplier=self.config.rightcodes.point_multiplier,
        )

    def _get_balance_locked(self, runtime_root: Path, user_id: str):
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            return self._quota_store(runtime_root).get_balance(user_id)

    def _get_ranking_locked(self, runtime_root: Path):
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            return self._quota_store(runtime_root).get_points_ranking(limit=10)

    def _set_model_locked(self, runtime_root: Path, user_id: str, model: str):
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            return self._quota_store(runtime_root).set_model(user_id, model)

    def _reserve_locked(self, runtime_root: Path, user_id: str, model: str) -> RightCodesDrawQuotaResult:
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            return self._quota_store(runtime_root).reserve(user_id, model=model)

    def _refund_locked(self, runtime_root: Path, quota: RightCodesDrawQuotaResult) -> None:
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            self._quota_store(runtime_root).refund(quota)

    def _record_points_locked(self, runtime_root: Path, user_id: str) -> None:
        with InterProcessLock(runtime_root / ".maibot_locks" / "draw.lock"):
            self._quota_store(runtime_root).record_group_message(user_id)


def _message_scope(message: object) -> tuple[str, str]:
    if not isinstance(message, Mapping):
        return "", ""
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return "", ""
    group_info = message_info.get("group_info")
    user_info = message_info.get("user_info")
    group_id = str(group_info.get("group_id") or "").strip() if isinstance(group_info, Mapping) else ""
    user_id = str(user_info.get("user_id") or "").strip() if isinstance(user_info, Mapping) else ""
    return group_id, user_id


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
        timestamp = time.time()
    return {
        "text": text,
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(additional_dict.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": at_target_ids,
    }


def _text_segments(message: object) -> str:
    parts: list[str] = []
    for segment in _message_parts(message):
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping):
            parts.append(str(data.get("text") or data.get("content") or ""))
        else:
            parts.append(str(data or ""))
    return "".join(parts).strip()


def _message_parts(message: object) -> list[Mapping[str, object]]:
    if not isinstance(message, Mapping):
        return []
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        raw_message = message.get("message")
    if not isinstance(raw_message, list):
        return []
    return [segment for segment in raw_message if isinstance(segment, Mapping)]


def _image_urls(message: object) -> list[str]:
    """Return raw current-message image sources for compatibility and tests."""

    sources, _ = _message_image_sources(message)
    return sources


def _message_image_sources(message: object) -> tuple[list[str], bool]:
    sources: list[str] = []
    unresolved = False
    for segment in _message_parts(message):
        segment_type = str(segment.get("type") or "").lower()
        data = segment.get("data")
        if segment_type == "reply":
            preview = _reply_preview_text(segment)
            unresolved = unresolved or _contains_media_placeholder(preview)
            continue
        if segment_type != "image":
            continue
        source = ""
        if isinstance(data, Mapping):
            source = str(data.get("url") or data.get("file") or "").strip()
        elif isinstance(data, str):
            source = data.strip()
        binary_base64 = str(segment.get("binary_data_base64") or "").strip()
        if binary_base64:
            try:
                source = _data_url_from_base64(binary_base64)
            except (ValueError, binascii.Error):
                unresolved = True
                continue
        if source.startswith(("http://", "https://", "data:image/", "base64://")):
            if source not in sources:
                sources.append(source)
        else:
            unresolved = True
    return sources, unresolved


def _reply_message_ids(message: object) -> tuple[str, ...]:
    result: list[str] = []
    for segment in _message_parts(message):
        if str(segment.get("type") or "").lower() != "reply":
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        message_id = str(data.get("target_message_id") or data.get("id") or "").strip()
        if message_id and message_id not in result:
            result.append(message_id)
    return tuple(result)


def _reply_preview_texts(message: object) -> tuple[str, ...]:
    result: list[str] = []
    for segment in _message_parts(message):
        if str(segment.get("type") or "").lower() != "reply":
            continue
        text = _reply_preview_text(segment)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _reply_preview_text(segment: Mapping[str, object]) -> str:
    data = segment.get("data")
    if not isinstance(data, Mapping):
        return ""
    return str(
        data.get("target_message_content")
        or data.get("message_str")
        or data.get("text")
        or ""
    ).strip()


def _contains_media_placeholder(text: str) -> bool:
    normalized = str(text or "").lower()
    return any(placeholder in normalized for placeholder in _MEDIA_PLACEHOLDERS)


def _is_only_media_placeholder(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    for placeholder in _MEDIA_PLACEHOLDERS:
        normalized = normalized.replace(placeholder, "")
    return not normalized.strip()


async def _normalize_reference_image(source: str) -> str:
    normalized = str(source or "").strip()
    if normalized.startswith("data:image/"):
        return _validate_data_url(normalized)
    if normalized.startswith("base64://"):
        return _data_url_from_base64(normalized.removeprefix("base64://"))
    if normalized.startswith(("http://", "https://")):
        return await asyncio.to_thread(_download_image_data_url, normalized)
    raise ValueError("unsupported reference image source")


def _download_image_data_url(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "image/*",
            "User-Agent": "QQBot-MaiBot-RightCodes/0.2",
        },
    )
    with build_opener().open(request, timeout=_REFERENCE_IMAGE_TIMEOUT_SECONDS) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > _MAX_REFERENCE_IMAGE_BYTES:
                raise ValueError("reference image is too large")
        image_bytes = response.read(_MAX_REFERENCE_IMAGE_BYTES + 1)
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if len(image_bytes) > _MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("reference image is too large")
    mime_type = content_type if content_type.startswith("image/") else _image_mime_type(image_bytes)
    if not mime_type.startswith("image/"):
        raise ValueError("reference payload is not an image")
    return _data_url_from_bytes(image_bytes, mime_type)


def _validate_data_url(data_url: str) -> str:
    header, separator, payload = data_url.partition(",")
    if not separator or ";base64" not in header.lower():
        raise ValueError("reference image Data URL is invalid")
    mime_type = header[5:].split(";", 1)[0].strip().lower()
    if not mime_type.startswith("image/"):
        raise ValueError("reference image Data URL has an invalid MIME type")
    image_bytes = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    if len(image_bytes) > _MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("reference image is too large")
    detected_type = _image_mime_type(image_bytes)
    return _data_url_from_bytes(image_bytes, detected_type or mime_type)


def _data_url_from_base64(payload: str) -> str:
    image_bytes = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    if len(image_bytes) > _MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("reference image is too large")
    mime_type = _image_mime_type(image_bytes)
    if not mime_type:
        raise ValueError("reference payload is not a supported image")
    return _data_url_from_bytes(image_bytes, mime_type)


def _data_url_from_bytes(image_bytes: bytes, mime_type: str) -> str:
    if not image_bytes:
        raise ValueError("reference image is empty")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _message_id(message: object) -> str:
    return str(message.get("message_id") or "").strip() if isinstance(message, Mapping) else ""


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def create_plugin() -> QQBotDrawPlugin:
    """创建 RightCodes 生图插件实例。"""

    return QQBotDrawPlugin()
