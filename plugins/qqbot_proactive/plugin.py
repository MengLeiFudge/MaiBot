from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import asyncio
import json
import math
import os
import random
import time
import uuid

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

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
    appointment_due: float = 0
    appointment_intent: str = ""

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
        if not isinstance(state.appointment_intent, str) or len(state.appointment_intent) > 400:
            raise ValueError("主动私聊联系计划的内容无效")
        if bool(state.appointment_due) != bool(state.appointment_intent.strip()):
            raise ValueError("主动私聊联系计划的时间与内容不完整")
        for value in (state.next_due, state.pending_since, state.appointment_due):
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
                async with asyncio.timeout(90):
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
            state = (replace(state, owner_message_id=owner_id, activity_message_id="",
                             sent_turns=0, pending_since=0, quiet=False)
                     if state.stream_id == stream_id else Schedule(stream_id=stream_id, owner_message_id=owner_id))
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
            due = max(now, last_activity) + self._delay(resting=resting)
            state.next_due = max(state.next_due, due) if state.sent_turns else due
            if state.appointment_due:
                state.next_due = max(state.appointment_due, due) if resting else state.appointment_due
            changed = True
        if changed:
            state.save(self._state_path)
            self._schedule = state
        if state.quiet or now < state.next_due:
            return
        # 先给普通回复链路十分钟；它选择不回或漏回时，不永久封死主动联系。
        if latest_sender != bot_qq and now - last_activity < 600:
            return

        if state.pending_since:
            # 上次入队尚未观察到发言，不把一次短检查当成重新发送的理由。
            state.pending_since = 0
            state.next_due = now + self._delay(resting=True)
            state.save(self._state_path)
            return
        # 调用模型和入队之前持久化退避。超时可能已经执行，不能每 15 秒重试。
        state.pending_since = 0
        state.next_due = now + self._delay(resting=True)
        state.save(self._state_path)
        decision = await self._consider(state, owner, bot_qq)
        # 模型执行期间可能收到新消息或更换身份配置，旧决定不得继续发送。
        if await self._private_stream() != target or self._schedule is not state:
            return
        current_owner = await self._recent(stream_id, filter_bot=True)
        current_latest = await self._recent(stream_id)
        if (
            current_owner is None or current_latest is None
            or _message_fact(current_owner, stream_id)[0] != owner_id
            or _message_fact(current_latest, stream_id)[0] != latest_id
        ):
            return
        if self._schedule is not state:
            return
        state.topics = decision.topics
        state.appointment_due = 0
        state.appointment_intent = ""
        delay: float = decision.check_after_seconds
        if state.sent_turns >= config.proactive.max_unanswered_turns:
            delay = max(delay, self._delay(resting=True))
        state.next_due = time.time() + delay
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
            "不要虚构线下经历；对方明确要求不要打扰时保持安静。临时忙碌或睡觉要结合时间判断是否结束。"
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
            "latest_owner_unanswered": bool(recent) and _message_fact(recent[-1], state.stream_id)[1] == owner,
            "appointment": ({"intent": state.appointment_intent,
                             "due": datetime.fromtimestamp(state.appointment_due).astimezone().isoformat()}
                            if state.appointment_due else None),
            "topics": [asdict(topic) for topic in state.topics],
            "messages": messages,
        }
        async with asyncio.timeout(60):
            result = await self.ctx.llm.generate(
                prompt=[{"role": "system", "content": FOLLOWUP_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                model="planner", temperature=0.3, max_tokens=8192,
            )
        response = result.get("response")
        if result.get("success") is not True or not isinstance(response, str):
            raise RuntimeError("主动私聊话题判断没有返回有效结果")
        return Decision.parse(response, sources, time.time())

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_proactive_schedule_hint",
        description="向主人私聊规划器说明可自行安排一次后续联系",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=4000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_schedule(
        self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """在原有规划请求内提供工具说明，不为普通回复额外请求模型。"""
        target = await self._private_stream()
        if target is None or target[1] != session_id or item_schema_version != 1 or not isinstance(items, list):
            return {"action": "continue"}
        rule = {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": uuid.uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat()},
            "parts": [{"type": "text", "text": (
                "[私聊联系计划]有适合稍后关心的事情时，可以用 schedule_private_followup 给自己安排下次联系，"
                "通过 tool_search 查找该本地工具。依据对方真实消息的 source_id，指定 300–86400 秒后的时间和具体意图。"
                "同一私聊只保留一个计划，新计划替换旧计划；不再适合可用 cancel 取消。"
                "到期会重新判断是否适合发言，不是必须发送的提醒，不要承诺准确时间。"
                "安排不代表已经回答对方；本轮需要的普通回复照常执行，完成后结束规划，不用 wait 循环等时间。"
                "工具返回成功才算安排完成。临时忙碌可安排稍后关心，明确不要打扰时不要安排。[/私聊联系计划]"
            )}],
        }
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "items": [*items, rule], "item_schema_version": item_schema_version,
        }}

    @Tool(
        "schedule_private_followup",
        description="为主人已有私聊安排或取消一次后续联系，适用于稍后自然关心或延续话题。",
        parameters={
            "action": {"type": "string", "enum": ["schedule", "cancel"], "required": True},
            "delay_seconds": {"type": "integer", "minimum": 300, "maximum": 86400, "description": "多少秒后重新考虑联系；取消时省略"},
            "intent": {"type": "string", "maxLength": 400, "description": "届时关心的具体方向，不是最终消息正文；取消时省略"},
            "source_id": {"type": "string", "description": "支持此安排的真实主人消息 ID；取消时省略"},
        },
    )
    async def schedule_followup(
        self, action: str = "", delay_seconds: int = 0, intent: str = "", source_id: str = "", **kwargs: Any,
    ) -> dict[str, object]:
        """核验调用会话和来源，原子替换一个计划；不向聊天直接发消息。"""
        async with asyncio.timeout(5):
            target = await self._private_stream()
            if target is None or kwargs.get("stream_id") != target[1] or kwargs.get("group_id"):
                return {"success": False, "content": "只能在已配置的主人私聊中安排联系。"}
            owner, stream_id = target
            latest_owner = await self._recent(stream_id, filter_bot=True)
            if latest_owner is None:
                return {"success": False, "content": "没有可用的主人消息，未安排。"}
            owner_id, sender, _ = _message_fact(latest_owner, stream_id)
            if sender != owner:
                return {"success": False, "content": "主人身份不匹配，未安排。"}
            if action not in ("schedule", "cancel"):
                return {"success": False, "content": "动作只能是 schedule 或 cancel。"}
            if action == "schedule":
                if (type(delay_seconds) is not int or not 300 <= delay_seconds <= 86400
                        or not isinstance(intent, str) or not intent.strip() or len(intent) > 400
                        or not isinstance(source_id, str) or not source_id):
                    return {"success": False, "content": "需要 300–86400 秒的间隔、具体联系方向和来源消息。"}
                source = await self.ctx.message.get_by_id(source_id, chat_id=stream_id, include_binary_data=False)
                if source is None:
                    return {"success": False, "content": "未找到同会话的来源消息，未安排。"}
                _, source_sender, source_time = _message_fact(source, stream_id)
                if source_sender != owner or not 0 <= time.time() - source_time <= TOPIC_LIFETIME:
                    return {"success": False, "content": "来源必须是七天内的真实主人消息，未安排。"}
            if await self._private_stream() != target:
                return {"success": False, "content": "私聊配置已变化，未安排。"}
            current_owner = await self._recent(stream_id, filter_bot=True)
            if current_owner is None or _message_fact(current_owner, stream_id)[0] != owner_id:
                return {"success": False, "content": "对方已有新消息，请先阅读后重新决定。"}
        state = self._schedule
        if state.stream_id != stream_id:
            state = Schedule(stream_id=stream_id, owner_message_id=owner_id)
        elif state.owner_message_id != owner_id:
            state = replace(state, owner_message_id=owner_id, activity_message_id="", sent_turns=0, pending_since=0, quiet=False)
        if action == "schedule" and state.quiet:
            return {"success": False, "content": "当前已暂停主动联系，等待对方重新开始聊天。"}
        delay = delay_seconds if action == "schedule" else self._delay(resting=False)
        if state.sent_turns >= cast(ProactiveConfig, self.config).proactive.max_unanswered_turns:
            delay = max(delay, self._delay(resting=True))
        due = time.time() + delay
        state = replace(state, next_due=due, appointment_due=due if action == "schedule" else 0,
                        appointment_intent=intent.strip() if action == "schedule" else "")
        state.save(self._state_path)
        self._schedule = state
        self.ctx.logger.info("主动私聊联系计划已更新，动作=%s，等待秒数=%s", action, round(delay))
        return {"success": True, "content": (
            "已取消当前联系计划，恢复普通空闲判断。" if action == "cancel" else
            "已安排一次联系检查：" + datetime.fromtimestamp(due).astimezone().isoformat() + "。到期结合新消息判断，不保证发言。"
        )}


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 注入公开能力的私聊调度插件。"""
    return QQBotProactivePlugin()
