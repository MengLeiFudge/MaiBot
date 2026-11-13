from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import json
import math
import os


@dataclass
class DesireEvent:
    """一次已经发生的释放记录，笔记只留在本机。"""

    at: float
    action: str
    before: float
    after: float
    note: str

    @classmethod
    def from_dict(cls, data: object) -> DesireEvent:
        if not isinstance(data, dict):
            raise ValueError("性欲记录不是对象")
        event = cls(**data)
        if event.action not in ("self_relief", "shared"):
            raise ValueError("性欲记录动作无效")
        if not isinstance(event.note, str) or not event.note.strip() or len(event.note) > 120:
            raise ValueError("性欲记录笔记无效")
        for value in (event.at, event.before, event.after):
            if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("性欲记录数值无效")
        if event.before > 100 or event.after > 100:
            raise ValueError("性欲记录超出范围")
        return event


@dataclass
class DesireState:
    """全身一份的性欲值。时间会涨，色情对话和主人的身体刺激也会涨。"""

    value: float = 0
    updated_at: float = 0
    next_decision: float = 0
    last_topic_at: float = 0
    last_physical_at: float = 0
    events: list[DesireEvent] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, now: float) -> DesireState:
        """缺失时从零开始；损坏时失败，避免把积攒进度悄悄清掉。"""
        if not path.exists():
            return cls(updated_at=now)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("性欲状态不是对象")
        raw_events = data.pop("events", [])
        if not isinstance(raw_events, list) or len(raw_events) > 40:
            raise ValueError("性欲记录数量无效")
        state = cls(**data, events=[DesireEvent.from_dict(item) for item in raw_events])
        for value in (state.value, state.updated_at, state.next_decision, state.last_topic_at, state.last_physical_at):
            if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("性欲状态数值无效")
        if state.value > 100:
            raise ValueError("性欲状态超出范围")
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def accrue(self, now: float, per_second: float) -> DesireState:
        """按真实流逝时间补记。没有起始时间或时钟回拨时，不把整段历史一次加满。"""
        if self.updated_at <= 0 or now < self.updated_at:
            return replace(self, updated_at=now)
        gained = max(0.0, now - self.updated_at) * per_second
        value = min(100.0, round(self.value + gained, 1))
        return replace(self, value=value, updated_at=now)

    def stimulate(self, kind: str, now: float, amount: float, gap: float) -> DesireState | None:
        """同一种刺激在间隔内只加一次。返回空表示这次已经记过。"""
        previous = self.last_topic_at if kind == "topic" else self.last_physical_at
        if previous and now - previous < gap:
            return None
        value = min(100.0, round(self.value + amount, 1))
        if kind == "topic":
            return replace(self, value=value, updated_at=now, last_topic_at=now)
        return replace(self, value=value, updated_at=now, last_physical_at=now)

    def release(self, action: str, note: str, now: float, afterglow: float) -> DesireState:
        after = min(self.value, afterglow)
        event = DesireEvent(at=now, action=action, before=self.value, after=after, note=note.strip())
        return replace(self, value=after, updated_at=now, events=[*self.events, event][-40:])

    def planner_text(self, high: float) -> str:
        """只记账，不覆盖人设，也不规定怎么说话。"""
        return (
            "[性欲状态]这是身体有多想要的记账，不替换人设。不要把数值说出来。"
            f"当前大约是{round(self.value)}。越高越想要，但只能决定想不想露出来，不能把吃饭、睡觉、照片这些话题改写成发情。普通内容先按原话题回答。自己解决需要不低于{round(high)}。"
            "对方这一轮确实在聊色情或挑逗时，通过 tool_search 调用 note_desire_rise，kind 用 topic。"
            "主人这一轮确实在摸、亲、进入或用别的方式刺激身体时，kind 用 physical，而且只能在主人私聊。"
            "普通聊天、自己先说的感觉、回忆和假设不要记。同一种刺激短时间重复调用会被拒绝。"
            "通过 tool_search 查找 record_desire_action。自己解决用 self_relief。"
            "和主人实际亲密之后用 shared，而且只能在主人私聊里记。笔记最多 120 字，写这次决定，不写他人隐私。"
            "做不做由你决定。工具成功后才会降到余韵。[/性欲状态]"
        )
