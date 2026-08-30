from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import hashlib
import sqlite3
import time

from qqbot_common.api_results import require_api_result


ApiCaller = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ComicFriendRouteDecision:
    selected_worker: str
    has_friend: bool


class ComicFriendRouteCoordinator:
    """Rendezvous friend capability across MaiBot processes through shared SQLite."""

    def __init__(self, database: Path, *, expected_workers: int = 1, wait_seconds: float = 1.5) -> None:
        self.database = Path(database)
        self.expected_workers = max(1, int(expected_workers))
        self.wait_seconds = max(0.1, float(wait_seconds))

    async def choose(
        self,
        event_key: str,
        *,
        self_id: str,
        is_friend: bool,
        preferred_worker: str = "",
    ) -> ComicFriendRouteDecision:
        key = str(event_key).strip()
        worker = str(self_id).strip()
        if not key or not worker:
            return ComicFriendRouteDecision(worker, bool(is_friend))
        deadline = time.monotonic() + self.wait_seconds
        await asyncio.to_thread(self._publish, key, worker, is_friend)
        while True:
            participants = await asyncio.to_thread(self._participants, key)
            if len(participants) >= self.expected_workers or time.monotonic() >= deadline:
                capable = sorted(item for item, available in participants.items() if available)
                candidates = capable or sorted(participants)
                selected = preferred_worker if preferred_worker in candidates else (candidates[0] if candidates else worker)
                return ComicFriendRouteDecision(selected, bool(capable))
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=5)
        connection.execute("pragma busy_timeout=5000")
        connection.execute(
            "create table if not exists comic_friend_routes (event_key text not null, self_id text not null, is_friend integer not null, expires_at real not null, primary key(event_key, self_id))"
        )
        return connection

    def _publish(self, event_key: str, self_id: str, is_friend: bool) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("delete from comic_friend_routes where expires_at<=?", (now,))
            connection.execute(
                "insert into comic_friend_routes values (?, ?, ?, ?) on conflict(event_key, self_id) do update set is_friend=excluded.is_friend, expires_at=excluded.expires_at",
                (event_key, self_id, int(is_friend), now + 15),
            )

    def _participants(self, event_key: str) -> dict[str, bool]:
        with self._connect() as connection:
            rows = connection.execute(
                "select self_id, is_friend from comic_friend_routes where event_key=? and expires_at>?",
                (event_key, time.time()),
            ).fetchall()
        return {str(worker): bool(available) for worker, available in rows}


async def is_onebot_friend(call_api: ApiCaller, user_id: int) -> bool:
    result = await call_api("adapter.napcat.account.get_friend_list", no_cache=False)
    payload = require_api_result(result, "查询好友列表")
    records: object = payload
    if isinstance(payload, Mapping):
        records = payload.get("data", payload.get("friends", []))
    if not isinstance(records, list):
        return False
    target = str(int(user_id))
    return any(isinstance(item, Mapping) and str(item.get("user_id") or "") == target for item in records)


def comic_event_key(text: str, user_id: str, group_id: str, timestamp: float) -> str:
    bucket = int((timestamp if timestamp > 0 else time.time()) // 5)
    value = "\x1f".join((group_id, user_id, " ".join(text.split()).casefold(), str(bucket)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
