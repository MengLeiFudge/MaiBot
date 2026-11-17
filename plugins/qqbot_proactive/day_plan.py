from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import json
import re


@dataclass(frozen=True)
class DayPlan:
    """工作区里的日程意图；时间经过不代表活动已经执行。"""

    day: str
    entries: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, filename: str) -> DayPlan | None:
        """接受至多十二项当天安排；缺少文件表示尚未制定计划。"""
        if not filename:
            return None
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError("日程文件必须使用绝对路径")
        if not path.exists():
            return None
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("日程文件超过 16 KiB")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("date"), str):
            raise ValueError("日程需要 date 和 entries")
        day = data["date"]
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("日程日期须为 YYYY-MM-DD")
        entries = data.get("entries")
        if not isinstance(entries, list) or len(entries) > 12:
            raise ValueError("日程最多十二项")
        parsed: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("日程条目必须是对象")
            clock, intent = entry.get("time"), entry.get("intent")
            if not isinstance(clock, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock) is None or clock in seen:
                raise ValueError("日程时间须为不重复的 HH:MM")
            if not isinstance(intent, str) or not intent.strip() or len(intent) > 160:
                raise ValueError("日程意图须为 1–160 字")
            seen.add(clock)
            parsed.append((clock, intent.strip()))
        return cls(day, tuple(sorted(parsed)))

    def hint(self, now: datetime) -> str:
        """仅展示今天最近一项和接下来两项，不携带其他日期的旧安排。"""
        if self.day != now.date().isoformat():
            return ""
        clock = now.strftime("%H:%M")
        past = [entry for entry in self.entries if entry[0] <= clock]
        future = [entry for entry in self.entries if entry[0] > clock]
        selected = past[-1:] + future[:2]
        if not selected:
            return ""
        payload = {"date": self.day, "entries": [{"time": time, "intent": intent} for time, intent in selected]}
        return "日程资料（只是意图，尚未证明执行；不改变共享自身状态）：" + json.dumps(payload, ensure_ascii=False)
