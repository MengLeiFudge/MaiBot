from __future__ import annotations

from typing import Any, ClassVar, Mapping

import asyncio
import random

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .decision import PokeDecision
from .decision import build_poke_prompt
from .decision import parse_poke_decision
from .state import POKE_STATE_ARMED
from .state import POKE_STATE_MUTE
from .state import PokeObservation
from .state import PokeStateMachine


class PluginSection(PluginConfigBase):
    """戳一戳插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否接管发给当前机器人的拍一拍事件")
    config_version: str = Field(default="0.1.1", description="配置版本")


class StateSection(PluginConfigBase):
    """拍击压力与 AI 租约配置。"""

    __ui_label__ = "状态机"
    __ui_order__ = 1

    window_seconds: float = Field(default=60.0, ge=10.0, le=600.0, description="拍击压力聚合窗口")
    mute_threshold: int = Field(default=9, ge=6, le=30, description="自动进入 MUTE 的拍击数")
    mute_state_seconds: float = Field(default=30.0, ge=5.0, le=600.0, description="MUTE 状态持续时间")
    ai_lease_seconds: float = Field(default=120.0, ge=10.0, le=600.0, description="单群单机器人 AI 租约")
    ai_cooldown_seconds: float = Field(default=3.0, ge=0.0, le=60.0, description="AI 请求完成后的短冷却")


class ModerationSection(PluginConfigBase):
    """MUTE 本地禁言配置。"""

    __ui_label__ = "禁言"
    __ui_order__ = 2

    mute_duration_min_seconds: int = Field(default=30, ge=1, le=2592000, description="随机禁言最短秒数")
    mute_duration_max_seconds: int = Field(default=90, ge=1, le=2592000, description="随机禁言最长秒数")
    successful_mute_cooldown_seconds: float = Field(
        default=90.0,
        ge=0.0,
        le=600.0,
        description="成功禁言后忽略后续拍一拍的秒数",
    )


class RoutingSection(PluginConfigBase):
    """机器人消息过滤配置。"""

    __ui_label__ = "路由"
    __ui_order__ = 3

    bot_account_ids: list[str] = Field(default_factory=list, description="不会触发戳一戳互动的机器人 QQ")


class PokeConfig(PluginConfigBase):
    """戳一戳插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    state: StateSection = Field(default_factory=StateSection)
    moderation: ModerationSection = Field(default_factory=ModerationSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)


class QQBotPokePlugin(MaiBotPlugin):
    """在入站主链前消费 NapCat 拍一拍通知。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = PokeConfig

    async def on_load(self) -> None:
        """创建重载作用域内的状态机。"""

        self._mutex = asyncio.Lock()
        self._state = self._new_state_machine()
        self.ctx.logger.info("QQBot 戳一戳插件已加载，启用=%s", self.config.plugin.enabled)

    async def on_unload(self) -> None:
        """丢弃全部内存拍击状态。"""

        self._state = self._new_state_machine()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置更新时重建内存状态，避免混用不同阈值。"""

        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            async with self._mutex:
                self._state = self._new_state_machine()
            self.ctx.logger.info("QQBot 戳一戳配置已更新并清空内存状态: %s", version)

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_poke_notice_gate",
        description="消费拍一拍通知并阻止其进入普通聊天和记忆链",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=150000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_poke_notice(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        """只对拍击当前机器人的真人事件执行状态机，所有拍击通知均中止主链。"""

        del kwargs
        notice = _parse_poke_notice(message)
        if notice is None:
            return {"action": "continue"}
        try:
            if self.config.plugin.enabled:
                await self._handle_notice(notice, message)
        except Exception as exc:
            self.ctx.logger.warning("戳一戳处理失败并保持静默: error_type=%s", type(exc).__name__)
        return {"action": "abort"}

    async def _handle_notice(self, notice: dict[str, str], message: object) -> None:
        if not notice["group_id"] or notice["target_id"] != notice["self_id"]:
            return
        if notice["sender_id"] in self.config.routing.bot_account_ids:
            return

        async with self._mutex:
            if self._state.is_successful_mute_cooldown(notice["group_id"], notice["self_id"]):
                return
            observation = self._state.record(
                notice["group_id"],
                notice["self_id"],
                notice["sender_id"],
            )
            if observation.state == POKE_STATE_MUTE:
                await self._mute_sender(observation)
                return
            acquired = self._state.acquire_ai(
                notice["group_id"],
                notice["self_id"],
                lease_seconds=self.config.state.ai_lease_seconds,
            )
        if not acquired:
            return

        try:
            decision = await self._decide(observation)
            if decision is None:
                return
            async with self._mutex:
                if not self._state.is_current(
                    observation.group_id,
                    observation.self_id,
                    observation.generation,
                ):
                    return
                await self._execute_decision(decision, observation, _stream_id(message))
        finally:
            async with self._mutex:
                self._state.release_ai(
                    observation.group_id,
                    observation.self_id,
                    cooldown_seconds=self.config.state.ai_cooldown_seconds,
                )

    async def _decide(self, observation: PokeObservation) -> PokeDecision | None:
        display_name = await self._display_name(observation.group_id, observation.sender_id)
        personality = str(await self.ctx.config.get("personality.personality", "")).strip()
        if not personality:
            raise RuntimeError("主配置缺少 personality.personality")
        prompt = build_poke_prompt(
            personality=personality,
            poke_text=f"{display_name}拍了拍你",
            state=observation.state,
        )
        result = await self.ctx.llm.generate(prompt, temperature=0.4, max_tokens=120)
        if not isinstance(result, Mapping) or not result.get("success"):
            raise RuntimeError("拍一拍决策模型调用失败")
        response = str(result.get("response") or result.get("content") or "")
        decision = parse_poke_decision(response, allow_mute=observation.state == POKE_STATE_ARMED)
        if decision is None:
            self.ctx.logger.info("拍一拍决策格式无效，当前事件保持静默")
        return decision

    async def _execute_decision(
        self,
        decision: PokeDecision,
        observation: PokeObservation,
        stream_id: str,
    ) -> None:
        if decision.action == "skip":
            return
        if decision.action == "poke_back":
            await self._call_adapter(
                "adapter.napcat.message.send_poke",
                user_id=observation.sender_id,
                group_id=observation.group_id,
            )
            return
        if decision.action == "text":
            if not stream_id:
                raise RuntimeError("拍一拍文字回复缺少 stream_id")
            await self.ctx.send.text(decision.text, stream_id)
            return
        if decision.action == "mute" and observation.state == POKE_STATE_ARMED:
            await self._mute_sender(observation)
            self._state.enter_mute(observation.group_id, observation.self_id)
            return
        raise RuntimeError(f"不允许的拍一拍动作: {decision.action}")

    async def _mute_sender(self, observation: PokeObservation) -> None:
        low = max(1, self.config.moderation.mute_duration_min_seconds)
        high = max(low, self.config.moderation.mute_duration_max_seconds)
        duration = random.SystemRandom().randint(low, high)
        await self._call_adapter(
            "adapter.napcat.group.set_group_ban",
            group_id=observation.group_id,
            user_id=observation.sender_id,
            duration=duration,
        )
        self._state.start_successful_mute_cooldown(
            observation.group_id,
            observation.self_id,
            cooldown_seconds=self.config.moderation.successful_mute_cooldown_seconds,
        )

    async def _display_name(self, group_id: str, user_id: str) -> str:
        result = await self.ctx.api.call("qqbot.identity.display_name", group_id=group_id, user_id=user_id)
        payload = require_api_result(result, "拍一拍群昵称解析")
        name = str(payload.get("display_name") or "").strip() if isinstance(payload, Mapping) else ""
        if not name:
            raise RuntimeError("拍一拍群昵称为空")
        return name

    async def _call_adapter(self, api_name: str, **kwargs: object) -> None:
        result = await self.ctx.api.call(api_name, **kwargs)
        require_api_result(result, f"NapCat 动作 {api_name}")

    def _new_state_machine(self) -> PokeStateMachine:
        return PokeStateMachine(
            window_seconds=self.config.state.window_seconds,
            mute_threshold=self.config.state.mute_threshold,
            mute_state_seconds=self.config.state.mute_state_seconds,
        )


def _parse_poke_notice(message: object) -> dict[str, str] | None:
    if not isinstance(message, Mapping) or not bool(message.get("is_notify")):
        return None
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return None
    additional = message_info.get("additional_config")
    if not isinstance(additional, Mapping):
        return None
    if str(additional.get("napcat_notice_type") or "") != "notify":
        return None
    if str(additional.get("napcat_notice_sub_type") or "") != "poke":
        return None
    payload = additional.get("napcat_notice_payload")
    if not isinstance(payload, Mapping):
        return None
    return {
        "group_id": str(payload.get("group_id") or "").strip(),
        "sender_id": str(payload.get("user_id") or "").strip(),
        "target_id": str(payload.get("target_id") or "").strip(),
        "self_id": str(payload.get("self_id") or additional.get("self_id") or "").strip(),
    }


def _stream_id(message: object) -> str:
    return str(message.get("session_id") or "").strip() if isinstance(message, Mapping) else ""


def create_plugin() -> QQBotPokePlugin:
    """创建戳一戳状态机插件实例。"""

    return QQBotPokePlugin()
