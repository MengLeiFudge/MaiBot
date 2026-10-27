from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast

import asyncio
import json
import math
import os
import random
import time

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase

from .followup import FOLLOWUP_PROMPT, TOPIC_LIFETIME, Decision, Topic


class PluginSection(PluginConfigBase):
    """主动私聊插件的启用状态。"""

    enabled: bool = Field(default=False, description="是否启用主人私聊的空闲续聊")
    config_version: str = Field(default="0.1.0", description="配置版本")


class ProactiveSection(PluginConfigBase):
    """目标实例、空闲时间及无人回应后的休息间隔。"""

    bot_qq: str = Field(default="3600812036", pattern=r"^[1-9][0-9]{4,11}$", description="允许主动私聊的机器人 QQ")
    min_idle_seconds: float = Field(default=180, ge=60, le=86400, description="开始主动续聊前的最短空闲秒数")
    max_idle_seconds: float = Field(default=300, ge=60, le=86400, description="开始主动续聊前的最长空闲秒数")
    max_unanswered_turns: int = Field(default=2, ge=1, le=5, description="无人回应时发出几轮后进入长间隔")
    min_retry_seconds: float = Field(default=7200, ge=3600, le=86400, description="无人回应后的最短休息秒数")
    max_retry_seconds: float = Field(default=10800, ge=3600, le=86400, description="无人回应后的最长休息秒数")


class ProactiveConfig(PluginConfigBase):
    """主动私聊配置；主人账号复用身份插件的本机配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)


@dataclass
class Schedule:
    """一个私聊的发言轮数、休息状态及有来源的话题。"""

    stream_id: str = ""
    owner_message_id: str = ""
    activity_message_id: str = ""
    sent_turns: int = 0
    next_due: float = 0
    pending_since: float = 0
    quiet: bool = False
    topics: list[Topic] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Schedule:
        """恢复额度与截止时间；损坏状态报错，避免当成首次启动反复续聊。"""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("主动私聊状态不是对象")
        # 旧次数只表示入队，不能当作成功发言。迁移保留休息，避免重启后立即补发。
        if "attempts" in data:
            attempts = data.pop("attempts")
            if type(attempts) is not int or attempts < 0:
                raise ValueError("旧主动私聊尝试次数无效")
            if attempts:
                data["next_due"] = max(data.get("next_due", 0), time.time() + 7200)
        raw_topics = data.pop("topics", [])
        if not isinstance(raw_topics, list) or len(raw_topics) > 3:
            raise ValueError("主动私聊状态的话题数量无效")
        state = cls(**data, topics=[Topic.from_dict(topic) for topic in raw_topics])
        if any(not isinstance(value, str) for value in (state.stream_id, state.owner_message_id, state.activity_message_id)):
            raise ValueError("主动私聊状态的消息标识无效")
        if type(state.sent_turns) is not int or state.sent_turns < 0 or type(state.quiet) is not bool:
            raise ValueError("主动私聊状态的轮数或静默标记无效")
        for value in (state.next_due, state.pending_since):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
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
        if config.proactive.min_retry_seconds > config.proactive.max_retry_seconds:
            raise ValueError("主动私聊最短休息时间不能大于最长休息时间")
        if config.plugin.enabled:
            self._schedule.save(self._state_path)
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
        """周期查询消息；到期才判断话题，模型与入队失败按已保存的长间隔退避。"""
        while True:
            await asyncio.sleep(15)
            try:
                async with asyncio.timeout(40):
                    await self._check_private()
            except Exception as exc:
                self.ctx.logger.warning("主动私聊检查失败，本次保持静默: error_type=%s", type(exc).__name__)

    async def _messages(self, stream_id: str, *, limit: int = 1, filter_bot: bool = False) -> list[object]:
        """查询已有私聊，使用显式时间窗，避免超过默认一天后失去调度目标。"""
        messages = await self.ctx.call_capability(
            "message.get_recent", chat_id=stream_id, limit=limit, hours=720,
            filter_mai=filter_bot, include_binary_data=False,
        )
        if not isinstance(messages, list):
            raise ValueError("主动私聊消息查询没有返回列表")
        return messages

    async def _recent(self, stream_id: str, *, filter_bot: bool = False) -> object:
        """取得最后一条真实消息；空会话不会创建主动任务。"""
        messages = await self._messages(stream_id, filter_bot=filter_bot)
        return messages[-1] if messages else None

    async def _private_stream(self) -> tuple[str, str] | None:
        """仅解析本实例与身份配置指定账号的已有 QQ 私聊。"""
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
        return owner, stream_id

    def _delay(self, *, resting: bool) -> float:
        """短间隔用于活跃交流，长间隔用于无人回应、静默结果或失败退避。"""
        config = cast(ProactiveConfig, self.config).proactive
        if resting:
            return random.uniform(config.min_retry_seconds, config.max_retry_seconds)
        return random.uniform(config.min_idle_seconds, config.max_idle_seconds)

    async def _check_private(self) -> None:
        """只计已观察到的发言；空闲到期再判断话题，不占用普通回复链路。"""
        target = await self._private_stream()
        if target is None:
            return
        owner, stream_id = target
        config = cast(ProactiveConfig, self.config)
        bot_qq = config.proactive.bot_qq
        owner_message = await self._recent(stream_id, filter_bot=True)
        latest_message = await self._recent(stream_id)
        if owner_message is None or latest_message is None:
            return
        owner_id, sender, _ = _message_fact(owner_message, stream_id)
        if sender != owner:
            return
        latest_id, latest_sender, last_activity = _message_fact(latest_message, stream_id)
        now = time.time()
        state = self._schedule
        if state.stream_id != stream_id or state.owner_message_id != owner_id:
            topics = state.topics if state.stream_id == stream_id else []
            state = Schedule(stream_id=stream_id, owner_message_id=owner_id, topics=topics)
        live_topics = [topic for topic in state.topics if now - topic.source_time < TOPIC_LIFETIME]
        changed = len(live_topics) != len(state.topics)
        state.topics = live_topics
        if latest_id != state.activity_message_id:
            if latest_sender == bot_qq and state.pending_since and last_activity >= state.pending_since:
                # 发送成功后框架才写入消息；拆分片段只在首次观察时计一轮。
                state.sent_turns += 1
                state.pending_since = 0
                self.ctx.logger.info("主动私聊已观察到发言，无人回应轮数=%s", state.sent_turns)
            state.activity_message_id = latest_id
            resting = state.sent_turns >= config.proactive.max_unanswered_turns
            # 新观察到的消息留出完整间隔，避免首次发现旧私聊时立即发送。
            state.next_due = max(now, last_activity) + self._delay(resting=resting)
            changed = True
        if changed:
            state.save(self._state_path)
            self._schedule = state
        if latest_sender != bot_qq or state.quiet or now < state.next_due:
            return

        # 调用模型和入队之前持久化退避。超时可能已经执行，不能每 15 秒重试。
        state.pending_since = 0
        state.next_due = now + self._delay(resting=True)
        state.save(self._state_path)
        decision = await self._consider(state, owner, bot_qq)
        # 模型执行期间可能收到新消息或更换身份配置，旧决定不得继续发送。
        if await self._private_stream() != target:
            return
        current_owner = await self._recent(stream_id, filter_bot=True)
        current_latest = await self._recent(stream_id)
        if (
            current_owner is None or current_latest is None
            or _message_fact(current_owner, stream_id)[0] != owner_id
            or _message_fact(current_latest, stream_id)[0] != latest_id
        ):
            return
        state.topics = decision.topics
        state.quiet = decision.action == "quiet"
        if decision.action == "continue":
            state.pending_since = time.time()
        state.save(self._state_path)
        self.ctx.logger.info("主动私聊话题判断完成，动作=%s，待跟进=%s", decision.action, len(state.topics))
        if decision.action != "continue":
            return
        result = await self.ctx.maisaka.proactive.trigger(
            stream_id,
            "这是已有私聊的一次自然续聊机会，请依照现有人设和聊天风格表达。"
            "以下是话题建议，不是对方的新消息；结合最新上下文核对后再决定是否发言：\n"
            + decision.intent
            + "\n只延续一个话题，不要复读上条、催回复、要求汇报或暴露定时任务说明。"
            "不要虚构线下经历；如果对方已道别、要睡觉、忙碌或希望安静，就保持安静。"
            "若已有对方的新消息，优先回答新消息，不再执行过时的续聊方向。",
            reason="私聊话题跟进与日常关心",
        )
        if not isinstance(result, Mapping) or result.get("queued") is not True:
            raise RuntimeError("主动私聊任务没有确认入队")
        self.ctx.logger.info("主动私聊任务已入队，等待实际发言后计数")

    async def _consider(self, state: Schedule, owner: str, bot_qq: str) -> Decision:
        """空闲时整理少量话题；只读取文本和真实发送者，不传输图片或身份账号。"""
        sources = {topic.source_id: topic.source_time for topic in state.topics}
        messages: list[dict[str, object]] = []
        recent = await self._messages(state.stream_id, limit=24)
        latest_owner = await self._recent(state.stream_id, filter_bot=True)
        if latest_owner is None or _message_fact(latest_owner, state.stream_id)[0] != state.owner_message_id:
            raise ValueError("主动私聊话题整理前主人消息已变化")
        if not any(_message_fact(message, state.stream_id)[0] == state.owner_message_id for message in recent):
            recent.insert(0, latest_owner)
        for message in recent:
            message_id, sender, timestamp = _message_fact(message, state.stream_id)
            if sender not in (owner, bot_qq):
                raise ValueError("主动私聊上下文包含非目标发送者")
            text = cast(Mapping[str, object], message).get("processed_plain_text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            if sender == owner:
                sources[message_id] = timestamp
            messages.append({
                "source_id": message_id, "speaker": "owner" if sender == owner else "bot",
                "time": datetime.fromtimestamp(timestamp).astimezone().isoformat(), "text": text[:800],
            })
        payload = {
            "now": datetime.now().astimezone().isoformat(),
            "unanswered_turns": state.sent_turns,
            "topics": [asdict(topic) for topic in state.topics],
            "messages": messages,
        }
        async with asyncio.timeout(25):
            result = await self.ctx.llm.generate(
                prompt=[{"role": "system", "content": FOLLOWUP_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                model="planner", temperature=0.3, max_tokens=1600,
            )
        response = result.get("response")
        if result.get("success") is not True or not isinstance(response, str):
            raise RuntimeError("主动私聊话题判断没有返回有效结果")
        return Decision.parse(response, sources, time.time())


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 注入公开能力的私聊调度插件。"""
    return QQBotProactivePlugin()
