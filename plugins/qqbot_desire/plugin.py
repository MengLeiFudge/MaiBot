from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import asyncio
import time
import uuid

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .state import DesireState


class PluginSection(PluginConfigBase):
    """性欲状态插件开关。"""

    enabled: bool = Field(default=True, description="是否按时间积攒性欲，并允许自己决定释放")
    config_version: str = Field(default="0.1.0", description="配置版本")


class DesireSection(PluginConfigBase):
    """积攒速度、释放阈值和主动做决定的间隔。"""

    hours_to_full: float = Field(default=12, ge=1, le=72, description="从空到满需要的小时数")
    high_threshold: float = Field(default=80, ge=50, le=100, description="允许自己解决、并主动考虑一次的数值")
    afterglow: float = Field(default=25, ge=0, le=40, description="释放后保留的余韵，不超过当前值")
    topic_gain: float = Field(default=8, ge=1, le=30, description="一轮色情或挑逗对话增加的数值")
    topic_gap_seconds: float = Field(default=120, ge=30, le=3600, description="色情对话再次加值前的最短间隔")
    physical_gain: float = Field(default=15, ge=1, le=40, description="主人一轮身体刺激增加的数值")
    physical_gap_seconds: float = Field(default=60, ge=20, le=3600, description="身体刺激再次加值前的最短间隔")
    decision_interval_seconds: float = Field(
        default=2700, ge=600, le=86400, description="很难受时空闲后，至少隔多久才再给自己一次决定机会",
    )
    quiet_start_hour: int = Field(default=0, ge=0, le=23, description="不主动发起决定的起始小时；与结束小时相同表示关闭")
    quiet_end_hour: int = Field(default=8, ge=0, le=23, description="恢复主动决定的小时；不影响别人发消息时的正常回复")


class DesireConfig(PluginConfigBase):
    """蓝铃性欲状态配置。主人账号复用身份插件。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    desire: DesireSection = Field(default_factory=DesireSection)


class QQBotDesirePlugin(MaiBotPlugin):
    """用一份跨会话状态代替提示词里的永久发情。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = DesireConfig

    async def on_load(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._path = Path(self.ctx.paths.data_dir) / "desire.json"
        self._state = DesireState.load(self._path, time.time())
        self._start()

    def _start(self) -> None:
        config = cast(DesireConfig, self.config).desire
        if config.afterglow >= config.high_threshold:
            raise ValueError("性欲余韵必须低于自己解决的阈值")
        if cast(DesireConfig, self.config).plugin.enabled:
            self._task = asyncio.create_task(self._run(), name="qqbot-desire")
        self.ctx.logger.info("性欲状态已加载，启用=%s", cast(DesireConfig, self.config).plugin.enabled)

    async def on_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self.on_unload()
            self._state = DesireState.load(self._path, time.time())
            self._start()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                async with asyncio.timeout(30):
                    await self._offer_decision()
            except Exception as exc:
                self.ctx.logger.warning("性欲决定机会失败，本次保持原状态: error_type=%s", type(exc).__name__)

    def _per_second(self) -> float:
        hours = cast(DesireConfig, self.config).desire.hours_to_full
        return 100 / (hours * 3600)

    async def _current(self) -> DesireState:
        async with self._lock:
            state = self._state.accrue(time.time(), self._per_second())
            if state != self._state:
                state.save(self._path)
                self._state = state
            return state

    def _quiet(self) -> bool:
        config = cast(DesireConfig, self.config).desire
        start, end = config.quiet_start_hour, config.quiet_end_hour
        if start == end:
            return False
        hour = datetime.now().hour
        return start <= hour < end if start < end else hour >= start or hour < end

    async def _owner_stream(self) -> tuple[str, str] | None:
        """只解析身份插件指定的已有主人私聊，没有私聊就不主动找。"""
        identity = await self.ctx.config.get_plugin("mlj.qqbot-identity")
        section = identity.get("identity") if isinstance(identity, Mapping) else None
        owner = section.get("owner_qq") if isinstance(section, Mapping) else None
        if not isinstance(owner, str) or not owner.isdecimal():
            return None
        stream = await self.ctx.chat.get_stream_by_user_id(owner, platform="qq")
        if not isinstance(stream, Mapping) or stream.get("platform") != "qq" or stream.get("group_id"):
            return None
        if stream.get("user_id") != owner:
            return None
        stream_id = stream.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id:
            return None
        return owner, stream_id

    async def _offer_decision(self) -> None:
        """很难受且过了间隔，才给主人私聊一次自己做决定的机会。"""
        if not cast(DesireConfig, self.config).plugin.enabled or self._quiet():
            return
        config = cast(DesireConfig, self.config).desire
        async with self._lock:
            now = time.time()
            state = self._state.accrue(now, self._per_second())
            if state.value < config.high_threshold or now < state.next_decision:
                if state != self._state:
                    state.save(self._path)
                    self._state = state
                return
            state = replace(state, next_decision=now + config.decision_interval_seconds)
            state.save(self._path)
            self._state = state
        target = await self._owner_stream()
        if target is None:
            return
        result = await self.ctx.maisaka.proactive.trigger(
            target[1],
            "这是你自己的空闲时刻，不是对方发来的新消息。说话方式照现有人设。"
            "你自己决定要不要找主人、自己解决并调用 record_desire_action，或者保持安静。"
            "不要复读上一条，不要催回复。",
            reason="性欲升高后的自主决定",
        )
        if not isinstance(result, Mapping) or result.get("queued") is not True:
            raise RuntimeError("性欲决定机会没有确认入队")
        self.ctx.logger.info("性欲决定机会已入队")

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_desire_planner",
        description="向规划器提供当前性欲档位和释放记录工具",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000, error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner(
        self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        if not cast(DesireConfig, self.config).plugin.enabled:
            return {"action": "continue"}
        if item_schema_version != 1 or not isinstance(items, list):
            raise ValueError("性欲状态需要 Context Item schema 1")
        state = await self._current()
        rule = {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": uuid.uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat()},
            "parts": [{"type": "text", "text": state.planner_text(cast(DesireConfig, self.config).desire.high_threshold)}],
        }
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "items": [*items, rule], "item_schema_version": item_schema_version,
        }}


    @Tool(
        "record_desire_action",
        description="记录自己解决，或记录和主人实际亲密后的释放。成功后性欲降到余韵。",
        parameters={
            "action": {"type": "string", "enum": ["self_relief", "shared"], "required": True},
            "note": {"type": "string", "maxLength": 120, "required": True,
                     "description": "这次决定的简短笔记，不写他人隐私或聊天原文"},
        },
    )
    async def record_desire_action(self, action: str = "", note: str = "", **kwargs: Any) -> dict[str, object]:
        if not cast(DesireConfig, self.config).plugin.enabled:
            return {"success": False, "content": "性欲状态未启用。"}
        if action not in ("self_relief", "shared") or not isinstance(note, str) or not note.strip() or len(note) > 120:
            return {"success": False, "content": "需要 self_relief 或 shared，以及 120 字以内的笔记。"}
        config = cast(DesireConfig, self.config).desire
        if action == "shared":
            target = await self._owner_stream()
            if target is None or kwargs.get("stream_id") != target[1] or kwargs.get("group_id"):
                return {"success": False, "content": "和主人的释放只能在主人私聊里记录。"}
        async with self._lock:
            state = self._state.accrue(time.time(), self._per_second())
            if action == "self_relief" and state.value < config.high_threshold:
                state.save(self._path)
                self._state = state
                return {"success": False, "content": "现在还没有难受到需要自己解决，未记录。"}
            updated = state.release(action, note, time.time(), config.afterglow)
            updated.save(self._path)
            self._state = updated
        self.ctx.logger.info("性欲释放已记录，动作=%s，释放前=%s，释放后=%s", action, round(state.value), round(updated.value))
        return {"success": True, "content": updated.planner_text(config.high_threshold)}

    @Tool(
        "note_desire_rise",
        description="对方这一轮的色情对话，或主人这一轮的身体刺激，让性欲自然升高。不能直接指定数值。",
        parameters={
            "kind": {"type": "string", "enum": ["topic", "physical"], "required": True},
            "note": {"type": "string", "maxLength": 120, "required": True,
                     "description": "这一轮实际发生了什么；不写进后续提示"},
        },
    )
    async def note_desire_rise(self, kind: str = "", note: str = "", **kwargs: Any) -> dict[str, object]:
        if not cast(DesireConfig, self.config).plugin.enabled:
            return {"success": False, "content": "性欲状态未启用。"}
        if kind not in ("topic", "physical") or not isinstance(note, str) or not note.strip() or len(note) > 120:
            return {"success": False, "content": "需要 topic 或 physical，以及 120 字以内的原因。"}
        config = cast(DesireConfig, self.config).desire
        if kind == "physical":
            target = await self._owner_stream()
            if target is None or kwargs.get("stream_id") != target[1] or kwargs.get("group_id"):
                return {"success": False, "content": "身体刺激只统计主人私聊里主人这一轮的动作。"}
        amount = config.topic_gain if kind == "topic" else config.physical_gain
        gap = config.topic_gap_seconds if kind == "topic" else config.physical_gap_seconds
        async with self._lock:
            state = self._state.accrue(time.time(), self._per_second())
            updated = state.stimulate(kind, time.time(), amount, gap)
            if updated is None:
                state.save(self._path)
                self._state = state
                return {"success": False, "content": "同一种刺激刚记过，这次不重复加。"}
            updated.save(self._path)
            self._state = updated
        self.ctx.logger.info(
            "性欲刺激已记录，类型=%s，刺激前=%s，刺激后=%s", kind, round(state.value), round(updated.value),
        )
        return {"success": True, "content": updated.planner_text(config.high_threshold)}




def create_plugin() -> MaiBotPlugin:
    return QQBotDesirePlugin()
