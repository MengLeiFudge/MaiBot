from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import json
import re


def _read_object(path: Path, limit: int) -> dict[str, Any]:
    """限量读取本机 JSON；格式错误不能当作空档案覆盖已有文件。"""
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("日记文件超过读取上限")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("日记文件必须是 JSON 对象")
    return value


class DiaryExporter:
    """只读 better-diary 成品，按内容摘要保护工作区中的人工修改。"""

    def __init__(self, source: Path, destination: Path, state_path: Path) -> None:
        self.source = source
        self.destination = destination
        self.state_path = state_path

    def sync(self) -> tuple[int, list[str]]:
        """导出新增或重写的成品；不复制选材原话、META、cookies 或发布状态。"""
        if not self.source.exists():
            return 0, []
        data = _read_object(self.source, 32 * 1024 * 1024)
        previous = _read_object(self.state_path, 1024 * 1024) if self.state_path.exists() else {}
        if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in previous.values()):
            raise ValueError("日记导出摘要记录无效")
        documents: dict[str, bytes] = {}
        for day, entry in data.items():
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("日记存档日期无效")
            if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
                raise ValueError("日记存档缺少正文")
            content = entry["content"].strip()
            if content:
                documents[day] = (content + "\n").encode("utf-8")
        self.destination.mkdir(parents=True, exist_ok=True)
        updated = dict(previous)
        count = 0
        conflicts: list[str] = []
        for day, content in documents.items():
            target = self.destination / f"{day}.md"
            wanted = sha256(content).hexdigest()
            if target.exists():
                # 仅替换上次由导出器写入且未被编辑的版本。未知同名文件也保留。
                if target.stat().st_size > 1024 * 1024:
                    conflicts.append(day)
                    continue
                current = sha256(target.read_bytes()).hexdigest()
                if current == wanted:
                    updated[day] = wanted
                    continue
                if current != previous.get(day):
                    conflicts.append(day)
                    continue
            temporary = target.with_suffix(".md.tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
            updated[day] = wanted
            count += 1
        if updated != previous:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(updated, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            temporary.replace(self.state_path)
        return count, conflicts
