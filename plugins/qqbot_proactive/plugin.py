from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar, cast

import asyncio
import json
import math
import os
import random
import time

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase


class PluginSection(PluginConfigBase):
    """主动私聊插件的启用状态。"""

    enabled: bool = Field(default=False, description="是否启用主人私聊的空闲续聊")
    config_version: str = Field(default="0.1.0", description="配置版本")


class ProactiveSection(PluginConfigBase):
    """目标实例、空闲时间和无人回应时的主动尝试上限。"""

    bot_qq: str = Field(default="3600812036", pattern=r"^[1-9][0-9]{4,11}$", description="允许主动私聊的机器人 QQ")
    min_idle_seconds: float = Field(default=180, ge=60, le=86400, description="开始主动续聊前的最短空闲秒数")
    max_idle_seconds: float = Field(default=300, ge=60, le=86400, description="开始主动续聊前的最长空闲秒数")
    max_unanswered_turns: int = Field(default=2, ge=1, le=5, description="主人未回复时最多尝试主动续聊几轮")


class ProactiveConfig(PluginConfigBase):
    """主动私聊配置；主人账号复用身份插件的本机配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)


@dataclass
class Schedule:
    """一个私聊的持久调度状态；一次主人新消息重置主动尝试额度。"""

    stream_id: str = ""
    owner_message_id: str = ""
    activity_message_id: str = ""
    attempts: int = 0
    next_due: float = 0

    @classmethod
    def load(cls, path: Path) -> Schedule:
        """恢复额度与截止时间；损坏状态报错，避免当成首次启动反复续聊。"""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        state = cls(**data)
        if any(not isinstance(value, str) for value in (state.stream_id, state.owner_message_id, state.activity_message_id)):
            raise ValueError("主动私聊状态的消息标识无效")
        if type(state.attempts) is not int or state.attempts < 0:
            raise ValueError("主动私聊状态的尝试次数无效")
        if type(state.next_due) not in (int, float) or not math.isfinite(state.next_due) or state.next_due < 0:
            raise ValueError("主动私聊状态的调度时间无效")
        return state

    def save(self, path: Path) -> None:
        """原子替换调度文件；写入失败时调用方不得继续发起主动任务。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)


def _message_fact(message: object, stream_id: str) -> tuple[str, str, float]:
    """从同会话的结构化消息提取 ID、发送者和时间，不读取正文推断身份。"""
    if not isinstance(message, Mapping) or message.get("session_id") != stream_id or message.get("platform") != "qq":
        raise ValueError("主动私聊消息不属于当前 QQ 会话")
    info = message.get("message_info")
    sender = info.get("user_info") if isinstance(info, Mapping) else None
    if not isinstance(info, Mapping) or info.get("group_info") or not isinstance(sender, Mapping):
        raise ValueError("主动私聊消息缺少私聊发送者事实")
    message_id, user_id = message.get("message_id"), sender.get("user_id")
    if not isinstance(message_id, str) or not message_id or not isinstance(user_id, str) or not user_id:
        raise ValueError("主动私聊消息标识无效")
    raw_timestamp = message.get("timestamp")
    if not isinstance(raw_timestamp, (str, int, float)) or isinstance(raw_timestamp, bool):
        raise ValueError("主动私聊消息缺少有效时间")
    timestamp = float(raw_timestamp)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("主动私聊消息时间无效")
    return message_id, user_id, timestamp


class QQBotProactivePlugin(MaiBotPlugin):
    """定时唤醒已有主人私聊，由原生 Planner/Replyer 决定是否自然续聊。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = ProactiveConfig

    async def on_load(self) -> None:
        """恢复本机额度并启动单个后台任务，不在加载回调中调用模型。"""
        self._task: asyncio.Task[None] | None = None
        self._state_path = Path(self.ctx.paths.data_dir) / "schedule.json"
        self._schedule = Schedule.load(self._state_path)
        self._start()

    def _start(self) -> None:
        """只为已启用且时间范围有效的配置创建调度循环。"""
        config = cast(ProactiveConfig, self.config)
        if config.proactive.min_idle_seconds > config.proactive.max_idle_seconds:
            raise ValueError("主动私聊最短空闲时间不能大于最长空闲时间")
        if config.plugin.enabled:
            self._task = asyncio.create_task(self._run(), name="qqbot-proactive-private")
        self.ctx.logger.info("主动私聊调度已加载，启用=%s", config.plugin.enabled)

    async def on_unload(self) -> None:
        """卸载前取消并回收调度任务，保留磁盘额度。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """重载只替换任务和时间范围，不重置无人回应额度。"""
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self.on_unload()
            self._schedule = Schedule.load(self._state_path)
            self._start()

    async def _run(self) -> None:
        """周期只查询本机状态，到期才提交主动任务；RPC 失败留待下个周期。"""
        while True:
            await asyncio.sleep(15)
            try:
                async with asyncio.timeout(12):
                    await self._check_private()
            except Exception as exc:
                self.ctx.logger.warning("主动私聊检查失败，本次保持静默: error_type=%s", type(exc).__name__)

    async def _recent(self, stream_id: str, *, filter_bot: bool = False) -> object:
        """只查询最近一条消息的结构化事实，避免传输图片二进制。"""
        messages = await self.ctx.call_capability(
            "message.get_recent", chat_id=stream_id, limit=1, filter_mai=filter_bot, include_binary_data=False
        )
        if not isinstance(messages, list):
            raise ValueError("主动私聊消息查询没有返回列表")
        return messages[-1] if messages else None

    async def _check_private(self) -> None:
        """仅在主人已有私聊空闲且上一条为本机器人回复时消费一次额度。"""
        config = cast(ProactiveConfig, self.config)
        if not config.plugin.enabled:
            return
        bot_qq = await self.ctx.config.get("bot.qq_account", "")
        if bot_qq != config.proactive.bot_qq:
            return
        identity = await self.ctx.config.get_plugin("mlj.qqbot-identity")
        section = identity.get("identity")
        owner = section.get("owner_qq") if isinstance(section, Mapping) else None
        if not isinstance(owner, str) or not owner.isdecimal() or owner == bot_qq:
            return
        stream = await self.ctx.chat.get_stream_by_user_id(owner, platform="qq")
        if stream is None:
            return
        if (
            not isinstance(stream, Mapping)
            or stream.get("platform") != "qq"
            or stream.get("group_id")
            or stream.get("user_id") != owner
        ):
            raise ValueError("主动私聊查询没有返回主人私聊")
        stream_id = stream.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("主动私聊缺少真实会话 ID")
        owner_message = await self._recent(stream_id, filter_bot=True)
        latest_message = await self._recent(stream_id)
        if owner_message is None or latest_message is None:
            return
        owner_id, sender, _ = _message_fact(owner_message, stream_id)
        if sender != owner:
            return
        latest_id, latest_sender, last_activity = _message_fact(latest_message, stream_id)
        state = self._schedule
        if state.stream_id != stream_id or state.owner_message_id != owner_id:
            state = Schedule(stream_id=stream_id, owner_message_id=owner_id)
        if latest_id != state.activity_message_id:
            state.activity_message_id = latest_id
            delay = random.uniform(config.proactive.min_idle_seconds, config.proactive.max_idle_seconds)
            # 新观察到的消息留出完整空闲间隔，避免首次发现旧私聊时立即发送。
            state.next_due = max(time.time(), last_activity) + delay
            state.save(self._state_path)
            self._schedule = state
        if (
            latest_sender != bot_qq
            or state.attempts >= config.proactive.max_unanswered_turns
            or time.time() < state.next_due
        ):
            return
        # 保存额度先于 RPC；请求超时可能已在 Host 入队，不能退还额度并重发。
        state.attempts += 1
        state.next_due = time.time() + random.uniform(config.proactive.min_idle_seconds, config.proactive.max_idle_seconds)
        state.save(self._state_path)
        result = await self.ctx.maisaka.proactive.trigger(
            stream_id,
            "这是与对方的已有私聊。结合最近话题和当前上下文，判断是否自然地主动聊一句。"
            "可以接续上个话题或分享一个相关想法，不要重复上一条、连续催回复或输出定时任务说明。"
            "如果对方已道别、要睡觉、忙碌或希望安静，就保持安静。若已有新消息，先回应新消息。",
            reason="私聊空闲续聊",
        )
        if not isinstance(result, Mapping) or result.get("queued") is not True:
            raise RuntimeError("主动私聊任务没有确认入队")
        self.ctx.logger.info("主动私聊任务已入队，本次无人回应尝试=%s", state.attempts)


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 注入公开能力的私聊调度插件。"""
    return QQBotProactivePlugin()
