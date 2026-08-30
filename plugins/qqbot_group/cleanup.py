from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncio
import math
import time

from .state import GroupStateStore


_BYTES_PER_MUTE_MINUTE = 1_000_000
_MAX_MUTE_SECONDS = 30 * 24 * 60 * 60


class OneBotActionCaller(Protocol):
    async def call_action(self, action_name: str, params: dict[str, object]) -> object:
        """Call one OneBot action and return its data payload."""


@dataclass(frozen=True, slots=True)
class GroupFileInfo:
    file_id: str
    name: str
    size: int
    uploaded_at: int
    uploader_id: str


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    user_id: str
    files: tuple[GroupFileInfo, ...]
    total_size: int
    mute_duration_seconds: int


@dataclass(frozen=True, slots=True)
class CleanupResult:
    root_file_count: int
    folder_count: int
    inner_file_count: int
    violating_user_count: int
    violating_file_count: int
    muted_user_count: int
    failed_mute_count: int


class GroupFileCleanupService:
    """Scan stale root-level group files, notify uploaders, and apply size-based mutes."""

    def __init__(
        self,
        *,
        caller: OneBotActionCaller,
        store: GroupStateStore,
        group_id: str,
        old_file_grace_days: int = 7,
        fetch_count: int = 10_000,
        message_interval_seconds: float = 1.0,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.caller = caller
        self.store = store
        self.group_id = str(group_id)
        self.old_file_grace_days = max(0, int(old_file_grace_days))
        self.fetch_count = max(1, min(50_000, int(fetch_count)))
        self.message_interval_seconds = max(0.0, float(message_interval_seconds))
        self.zone = _resolve_zone(timezone_name)

    async def run(self, *, now: datetime | None = None) -> CleanupResult:
        current = _coerce_now(now, self.zone)
        root_payload = await self.caller.call_action(
            "get_group_root_files",
            {"group_id": int(self.group_id), "file_count": self.fetch_count},
        )
        root_files = _parse_files(_extract_list(root_payload, "files", "file", "items"))
        folders = _parse_folders(_extract_list(root_payload, "folders", "folder"))
        inner_count = 0
        for folder_id, total_count in folders:
            folder_payload = await self.caller.call_action(
                "get_group_files_by_folder",
                {
                    "group_id": int(self.group_id),
                    "folder_id": folder_id,
                    "file_count": max(self.fetch_count, total_count),
                },
            )
            inner_count += len(_parse_files(_extract_list(folder_payload, "files", "file", "items")))

        summaries = _build_summaries(root_files, current, self.old_file_grace_days)
        if not summaries:
            await self._send_text(f"当前没有超过 {self.old_file_grace_days} 天的外层群文件需要清理。")
            return CleanupResult(len(root_files), len(folders), inner_count, 0, 0, 0, 0)

        await self._send_text(
            "该清理文件了！\n"
            f"以下只统计超过 {self.old_file_grace_days} 天、未归类到文件夹内的文件。\n"
            "请将自己的文件删除或移动到合适的文件夹。"
        )
        muted = 0
        failed_mutes = 0
        for start in range(0, len(summaries), 10):
            chunk = summaries[start : start + 10]
            if self.message_interval_seconds:
                await asyncio.sleep(self.message_interval_seconds)
            await self._send_summary(chunk)
            for summary in chunk:
                if summary.mute_duration_seconds < 60:
                    continue
                try:
                    await self.caller.call_action(
                        "set_group_ban",
                        {
                            "group_id": int(self.group_id),
                            "user_id": int(summary.user_id),
                            "duration": summary.mute_duration_seconds,
                        },
                    )
                except Exception:
                    failed_mutes += 1
                    continue
                muted += 1
                checked_at = int(current.timestamp())
                self.store.save_pending_cleanup(
                    group_id=self.group_id,
                    user_id=summary.user_id,
                    file_ids=(item.file_id for item in summary.files),
                    file_names=(item.name for item in summary.files),
                    checked_at=checked_at,
                    muted_until=checked_at + summary.mute_duration_seconds,
                )

        return CleanupResult(
            root_file_count=len(root_files),
            folder_count=len(folders),
            inner_file_count=inner_count,
            violating_user_count=len(summaries),
            violating_file_count=sum(len(summary.files) for summary in summaries),
            muted_user_count=muted,
            failed_mute_count=failed_mutes,
        )

    async def _send_text(self, text: str) -> None:
        await self.caller.call_action(
            "send_group_msg",
            {
                "group_id": int(self.group_id),
                "message": [{"type": "text", "data": {"text": text}}],
            },
        )

    async def _send_summary(self, summaries: tuple[CleanupSummary, ...]) -> None:
        segments: list[dict[str, object]] = []
        for index, summary in enumerate(summaries):
            if index:
                segments.append({"type": "text", "data": {"text": "\n"}})
            segments.append({"type": "at", "data": {"qq": summary.user_id}})
            size_mb = summary.total_size / _BYTES_PER_MUTE_MINUTE
            segments.append(
                {
                    "type": "text",
                    "data": {"text": f" {len(summary.files)} 个，{size_mb:.1f} MB"},
                }
            )
        await self.caller.call_action(
            "send_group_msg",
            {"group_id": int(self.group_id), "message": segments},
        )


def _build_summaries(
    files: tuple[GroupFileInfo, ...],
    now: datetime,
    grace_days: int,
) -> tuple[CleanupSummary, ...]:
    cutoff = int((now - timedelta(days=grace_days)).timestamp())
    grouped: dict[str, list[GroupFileInfo]] = {}
    for item in files:
        if not item.uploader_id or item.uploaded_at <= 0 or item.uploaded_at > cutoff:
            continue
        grouped.setdefault(item.uploader_id, []).append(item)
    summaries: list[CleanupSummary] = []
    for user_id, user_files in grouped.items():
        ordered = tuple(sorted(user_files, key=lambda item: (-item.size, item.name)))
        total_size = sum(max(0, item.size) for item in ordered)
        raw_seconds = math.ceil(total_size * 60 / _BYTES_PER_MUTE_MINUTE)
        mute_seconds = 0 if raw_seconds < 60 else min(raw_seconds, _MAX_MUTE_SECONDS)
        summaries.append(
            CleanupSummary(
                user_id=user_id,
                files=ordered,
                total_size=total_size,
                mute_duration_seconds=mute_seconds,
            )
        )
    return tuple(sorted(summaries, key=lambda item: (-item.total_size, item.user_id)))


def _extract_list(payload: object, *keys: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Mapping):
        return ()
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, Mapping))
        if isinstance(value, Mapping):
            nested = _extract_list(value, *keys)
            if nested:
                return nested
    return ()


def _parse_files(raw_files: tuple[Mapping[str, object], ...]) -> tuple[GroupFileInfo, ...]:
    files: list[GroupFileInfo] = []
    for item in raw_files:
        file_id = _first_text(item, "file_id", "id")
        name = _first_text(item, "file_name", "name")
        if not file_id or not name:
            continue
        files.append(
            GroupFileInfo(
                file_id=file_id,
                name=name,
                size=_first_int(item, "file_size", "size"),
                uploaded_at=_normalize_unix_seconds(
                    _first_positive_int(item, "upload_time", "uploaded_at", "create_time", "modify_time")
                ),
                uploader_id=_first_text(item, "uploader", "uploader_id", "user_id", "sender_id"),
            )
        )
    return tuple(files)


def _parse_folders(raw_folders: tuple[Mapping[str, object], ...]) -> tuple[tuple[str, int], ...]:
    folders: list[tuple[str, int]] = []
    for item in raw_folders:
        folder_id = _first_text(item, "folder_id", "id")
        if folder_id:
            folders.append((folder_id, _first_int(item, "total_file_count", "file_count")))
    return tuple(folders)


def _first_text(item: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _first_int(item: Mapping[str, object], *keys: str) -> int:
    value = _first_text(item, *keys)
    try:
        return int(float(value)) if value else 0
    except ValueError:
        return 0


def _first_positive_int(item: Mapping[str, object], *keys: str) -> int:
    for key in keys:
        value = _first_int(item, key)
        if value > 0:
            return value
    return 0


def _normalize_unix_seconds(value: int) -> int:
    normalized = int(value)
    while normalized > 10_000_000_000:
        normalized //= 1000
    return normalized


def _resolve_zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name=name)
        return timezone.utc


def _coerce_now(now: datetime | None, zone) -> datetime:
    if now is None:
        return datetime.fromtimestamp(time.time(), zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)
