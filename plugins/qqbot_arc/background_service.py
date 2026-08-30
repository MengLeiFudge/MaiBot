from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncio
import json
import logging
import re
import sqlite3


WIKI_API_URL = "https://arcaea.fandom.com/api.php"
DIFFICULTY_CC_FIELDS = {"Past": 0, "Present": 1, "Future": 2, "Beyond": 3, "Eternal": 4}


@dataclass(slots=True)
class ArcBackgroundState:
    alias_last_synced_at: str = ""
    constants_last_synced_at: str = ""
    version_last_checked_at: str = ""
    version_last_seen: str = ""
    version_last_downloaded: str = ""
    activity_last_checked_at: str = ""
    group_last_reminded_on: dict[str, str] = field(default_factory=dict)


class ArcBackgroundStore:
    """Persist ARC background facts and cross-instance reminder claims in SQLite."""

    NAMESPACE = "arc.background_state"

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS arc_background_state (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arc_activity_reminder_claims (
                    group_id TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    PRIMARY KEY(group_id, reminder_date)
                );
                CREATE TABLE IF NOT EXISTS arc_alias_cache (
                    song_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arc_constant_cache (
                    song_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    constants TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def load(self) -> ArcBackgroundState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM arc_background_state WHERE namespace = ?",
                (self.NAMESPACE,),
            ).fetchone()
        raw = json.loads(row[0]) if row else {}
        return ArcBackgroundState(
            alias_last_synced_at=str(raw.get("alias_last_synced_at", "")),
            constants_last_synced_at=str(raw.get("constants_last_synced_at", "")),
            version_last_checked_at=str(raw.get("version_last_checked_at", "")),
            version_last_seen=str(raw.get("version_last_seen", "")),
            version_last_downloaded=str(raw.get("version_last_downloaded", "")),
            activity_last_checked_at=str(raw.get("activity_last_checked_at", "")),
            group_last_reminded_on={
                str(key): str(value) for key, value in dict(raw.get("group_last_reminded_on", {})).items()
            },
        )

    def update(self, **changes: Any) -> ArcBackgroundState:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM arc_background_state WHERE namespace = ?",
                (self.NAMESPACE,),
            ).fetchone()
            raw = json.loads(row[0]) if row else {}
            raw.update(changes)
            payload = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO arc_background_state(namespace, payload, updated_at)
                VALUES (?, ?, unixepoch('subsec'))
                ON CONFLICT(namespace) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (self.NAMESPACE, payload),
            )
            connection.commit()
        return self.load()

    def claim_reminder(self, group_id: str, reminder_date: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO arc_activity_reminder_claims(group_id, reminder_date) VALUES (?, ?)",
                (str(group_id), reminder_date),
            )
            return cursor.rowcount == 1

    def release_reminder(self, group_id: str, reminder_date: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM arc_activity_reminder_claims WHERE group_id = ? AND reminder_date = ?",
                (str(group_id), reminder_date),
            )

    def record_reminded(self, group_id: str, reminder_date: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM arc_background_state WHERE namespace = ?",
                (self.NAMESPACE,),
            ).fetchone()
            raw = json.loads(row[0]) if row else {}
            reminders = dict(raw.get("group_last_reminded_on", {}))
            reminders[str(group_id)] = reminder_date
            raw["group_last_reminded_on"] = reminders
            connection.execute(
                """
                INSERT INTO arc_background_state(namespace, payload, updated_at)
                VALUES (?, ?, unixepoch('subsec'))
                ON CONFLICT(namespace) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (self.NAMESPACE, json.dumps(raw, ensure_ascii=False, separators=(",", ":"))),
            )
            connection.commit()

    def replace_aliases(self, songs: dict[str, tuple[str, list[str]]], updated_at: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM arc_alias_cache")
            connection.executemany(
                "INSERT INTO arc_alias_cache(song_id, title, aliases, updated_at) VALUES (?, ?, ?, ?)",
                (
                    (song_id, title, json.dumps(aliases, ensure_ascii=False), updated_at)
                    for song_id, (title, aliases) in songs.items()
                ),
            )
            connection.commit()

    def upsert_constants(self, songs: dict[str, tuple[str, dict[str, float]]], updated_at: str) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO arc_constant_cache(song_id, title, constants, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(song_id) DO UPDATE SET
                    title = excluded.title, constants = excluded.constants, updated_at = excluded.updated_at
                """,
                (
                    (song_id, title, json.dumps(constants), updated_at)
                    for song_id, (title, constants) in songs.items()
                ),
            )


class ArcKnowledgeSyncService:
    def __init__(self, assets_root: Path, store: ArcBackgroundStore, *, request_timeout: float = 20) -> None:
        self.assets_root = Path(assets_root)
        self.store = store
        self.request_timeout = request_timeout

    def sync_aliases(self, now: datetime) -> None:
        songs: dict[str, tuple[str, list[str]]] = {}
        for song_id, title, localized_titles in self._songs():
            aliases = list(dict.fromkeys(localized_titles + self._base_aliases(title)))
            for localized_title in localized_titles:
                try:
                    aliases.extend(alias for alias in self._fetch_redirects(localized_title) if alias not in aliases)
                except Exception:
                    continue
            songs[song_id] = (title, aliases)
        self.store.replace_aliases(songs, now.isoformat())

    def sync_missing_constants(self, now: datetime, *, limit: int = 20) -> None:
        with self.store._connect() as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT song_id FROM arc_constant_cache WHERE constants != '{}'"
                ).fetchall()
            }
        updates: dict[str, tuple[str, dict[str, float]]] = {}
        for song_id, title, _localized_titles in self._songs():
            if song_id in existing:
                continue
            try:
                constants = self._parse_constants(self._fetch_wikitext(title))
            except Exception:
                constants = {}
            updates[song_id] = (title, constants)
            if len(updates) >= limit:
                break
        if updates:
            self.store.upsert_constants(updates, now.isoformat())

    def _songs(self) -> list[tuple[str, str, list[str]]]:
        for path in (self.assets_root / "官谱" / "songlist", self.assets_root / "官谱" / "songlist.json"):
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                break
        else:
            raise FileNotFoundError(f"未找到 Arcaea songlist：{self.assets_root / '官谱'}")
        result: list[tuple[str, str, list[str]]] = []
        for song in payload.get("songs", []):
            if song.get("deleted"):
                continue
            localized = song.get("title_localized")
            localized = localized if isinstance(localized, dict) else {}
            titles = list(dict.fromkeys(str(value).strip() for value in localized.values() if str(value).strip()))
            song_id = str(song.get("id") or "").strip()
            title = str(localized.get("en") or (titles[0] if titles else song_id)).strip()
            if song_id and title:
                result.append((song_id, title, titles or [title]))
        return result

    @staticmethod
    def _base_aliases(title: str) -> list[str]:
        words = re.findall(r"[A-Za-z0-9]+", title)
        return ["".join(word[0] for word in words).lower()] if len(words) > 1 else []

    def _fetch_redirects(self, title: str) -> list[str]:
        payload = self._wiki_json(
            {"action": "query", "titles": title, "prop": "redirects", "rdlimit": "max", "format": "json"}
        )
        pages = payload.get("query", {}).get("pages", {})
        return [
            str(item.get("title") or "").strip()
            for page in pages.values()
            for item in page.get("redirects", [])
            if str(item.get("title") or "").strip()
        ]

    def _fetch_wikitext(self, title: str) -> str:
        payload = self._wiki_json(
            {
                "action": "query",
                "titles": title,
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "format": "json",
            }
        )
        for page in payload.get("query", {}).get("pages", {}).values():
            revisions = page.get("revisions") or []
            if revisions:
                revision = revisions[0]
                return str(revision.get("slots", {}).get("main", {}).get("*") or revision.get("*") or "")
        return ""

    def _wiki_json(self, query: dict[str, str]) -> dict[str, Any]:
        request = Request(
            f"{WIKI_API_URL}?{urlencode(query)}",
            headers={"User-Agent": "qqbot-arc/0.3"},
        )
        with urlopen(request, timeout=self.request_timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse_constants(wikitext: str) -> dict[str, float]:
        constants: dict[str, float] = {}
        for field_name, difficulty_index in DIFFICULTY_CC_FIELDS.items():
            match = re.search(
                rf"\|\s*{field_name}\s+CC\s*=\s*([0-9]+(?:\.[0-9]+)?)",
                wikitext,
                flags=re.IGNORECASE,
            )
            if match:
                constants[str(difficulty_index)] = float(match.group(1))
        return constants


class ArcBackgroundService:
    def __init__(
        self,
        store: ArcBackgroundStore,
        *,
        version_fetcher: Callable[[], str],
        alias_sync: Callable[[datetime], None],
        constants_sync: Callable[[datetime], None],
        expire_sessions: Callable[[float], list[tuple[str, Any]]],
        event_messages: Callable[[datetime], list[str]],
        list_group_ids: Callable[[], Awaitable[list[str]]],
        send_group: Callable[[str, Any], Awaitable[None]],
        timezone_name: str = "Asia/Shanghai",
        alias_interval: timedelta = timedelta(hours=24),
        constants_interval: timedelta = timedelta(hours=24),
        version_interval: timedelta = timedelta(hours=12),
        activity_check_interval: timedelta = timedelta(hours=1),
        reminder_group_ids: tuple[str, ...] = (),
        aliases_enabled: bool = True,
        constants_enabled: bool = True,
        version_check_enabled: bool = True,
        guess_expiration_enabled: bool = True,
        activity_reminders_enabled: bool = True,
        logger: logging.Logger | Any | None = None,
    ) -> None:
        self.store = store
        self.version_fetcher = version_fetcher
        self.alias_sync = alias_sync
        self.constants_sync = constants_sync
        self.expire_sessions = expire_sessions
        self.event_messages = event_messages
        self.list_group_ids = list_group_ids
        self.send_group = send_group
        self.zone = self._zone(timezone_name)
        self.alias_interval = alias_interval
        self.constants_interval = constants_interval
        self.version_interval = version_interval
        self.activity_check_interval = activity_check_interval
        self.reminder_group_ids = reminder_group_ids
        self.aliases_enabled = aliases_enabled
        self.constants_enabled = constants_enabled
        self.version_check_enabled = version_check_enabled
        self.guess_expiration_enabled = guess_expiration_enabled
        self.activity_reminders_enabled = activity_reminders_enabled
        self.logger = logger or logging.getLogger(__name__)

    async def run_once(self, now: datetime | None = None) -> None:
        current = self._now(now)
        state = self.store.load()
        if self.aliases_enabled and self._due(state.alias_last_synced_at, current, self.alias_interval):
            await self._run_sync_stage("alias", self.alias_sync, current, "alias_last_synced_at")
        state = self.store.load()
        if self.constants_enabled and self._due(state.constants_last_synced_at, current, self.constants_interval):
            await self._run_sync_stage("constants", self.constants_sync, current, "constants_last_synced_at")
        state = self.store.load()
        if self.version_check_enabled and self._due(state.version_last_checked_at, current, self.version_interval):
            try:
                version = await asyncio.to_thread(self.version_fetcher)
                self.store.update(version_last_checked_at=current.isoformat(), version_last_seen=version)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("QQBot ARC 后台 version 阶段失败: type=%s", type(exc).__name__)
        if self.guess_expiration_enabled:
            try:
                expired = await asyncio.to_thread(self.expire_sessions, current.timestamp())
                for group_id, message in expired:
                    await self.send_group(group_id, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("QQBot ARC 后台 guess-expiration 阶段失败: type=%s", type(exc).__name__)
        if self.activity_reminders_enabled and self._due(
            self.store.load().activity_last_checked_at,
            current,
            self.activity_check_interval,
        ):
            await self._send_activity_reminders(current)

    async def _run_sync_stage(
        self,
        name: str,
        callback: Callable[[datetime], None],
        current: datetime,
        state_field: str,
    ) -> None:
        try:
            await asyncio.to_thread(callback, current)
            self.store.update(**{state_field: current.isoformat()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("QQBot ARC 后台 %s 阶段失败: type=%s", name, type(exc).__name__)

    async def _send_activity_reminders(self, current: datetime) -> None:
        reminder_date = current.date().isoformat()
        try:
            groups = list(self.reminder_group_ids) or await self.list_group_ids()
            messages = await asyncio.to_thread(self.event_messages, current)
            self.store.update(activity_last_checked_at=current.isoformat())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("QQBot ARC 后台 activity-fetch 阶段失败: type=%s", type(exc).__name__)
            return
        if not messages or messages == ["当前没有活动梯子。"]:
            return
        for group_id in dict.fromkeys(str(item) for item in groups if str(item)):
            if not self.store.claim_reminder(group_id, reminder_date):
                continue
            try:
                for message in messages:
                    await self.send_group(group_id, message)
            except asyncio.CancelledError:
                self.store.release_reminder(group_id, reminder_date)
                raise
            except Exception as exc:
                self.store.release_reminder(group_id, reminder_date)
                self.logger.warning(
                    "QQBot ARC 后台 activity-send 阶段失败: group=%s type=%s",
                    group_id,
                    type(exc).__name__,
                )
                continue
            self.store.record_reminded(group_id, reminder_date)

    @staticmethod
    def _due(raw: str, current: datetime, interval: timedelta) -> bool:
        if not raw:
            return True
        try:
            previous = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=current.tzinfo)
        return current - previous.astimezone(current.tzinfo) >= interval

    def _now(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(self.zone)
        return value.replace(tzinfo=self.zone) if value.tzinfo is None else value.astimezone(self.zone)

    @staticmethod
    def _zone(name: str):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=8)) if name == "Asia/Shanghai" else timezone.utc
