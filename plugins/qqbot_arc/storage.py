from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import json
import sqlite3
import time


SessionMutator = Callable[[dict[str, Any]], tuple[dict[str, Any] | None, Any]]


class ArcSessionStore:
    """Cross-process Arc session state backed by one SQLite row per group."""

    def __init__(self, db_path: Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        last_error: sqlite3.OperationalError | None = None
        for _attempt in range(20):
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS arc_guess_sessions (
                            group_id TEXT PRIMARY KEY,
                            revision INTEGER NOT NULL,
                            updated_at REAL NOT NULL,
                            payload TEXT NOT NULL
                        )
                        """
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.05)
        raise sqlite3.OperationalError("初始化 ARC 会话数据库时持续被锁定") from last_error

    def load(self, group_id: str) -> dict[str, Any] | None:
        if not self.db_path.is_file():
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM arc_guess_sessions WHERE group_id = ?",
                    (str(group_id),),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        return json.loads(row[0]) if row else None

    def start(
        self,
        group_id: str,
        factory: Callable[[], dict[str, Any]],
        *,
        now: float | None = None,
        timeout_seconds: float = 300.0,
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, updated_at FROM arc_guess_sessions WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
            previous = json.loads(row[0]) if row else None
            if row and current - float(row[1]) <= timeout_seconds:
                connection.commit()
                return False, None, previous
            session = factory()
            connection.execute(
                """
                INSERT INTO arc_guess_sessions(group_id, revision, updated_at, payload)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    revision = arc_guess_sessions.revision + 1,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (str(group_id), current, json.dumps(session, ensure_ascii=False)),
            )
            connection.commit()
            return True, previous, session

    def mutate(
        self,
        group_id: str,
        mutator: SessionMutator,
        *,
        now: float | None = None,
        touch: bool = True,
    ) -> Any:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision, updated_at FROM arc_guess_sessions WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            session = json.loads(row[0])
            session["_updated_at"] = float(row[2])
            replacement, result = mutator(session)
            if replacement is None:
                connection.execute(
                    "DELETE FROM arc_guess_sessions WHERE group_id = ?",
                    (str(group_id),),
                )
            else:
                replacement.pop("_updated_at", None)
                connection.execute(
                    """
                    UPDATE arc_guess_sessions
                    SET revision = ?, updated_at = ?, payload = ?
                    WHERE group_id = ?
                    """,
                    (
                        int(row[1]) + 1,
                        current if touch else float(row[2]),
                        json.dumps(replacement, ensure_ascii=False),
                        str(group_id),
                    ),
                )
            connection.commit()
            return result

    def collect_expired(self, *, now: float, timeout_seconds: float) -> list[tuple[str, dict[str, Any]]]:
        if not self.db_path.is_file():
            return []
        cutoff = float(now) - float(timeout_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT group_id, payload FROM arc_guess_sessions WHERE updated_at < ? ORDER BY group_id",
                    (cutoff,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    connection.commit()
                    return []
                raise
            if rows:
                connection.executemany(
                    "DELETE FROM arc_guess_sessions WHERE group_id = ?",
                    ((str(row[0]),) for row in rows),
                )
            connection.commit()
        return [(str(group_id), json.loads(payload)) for group_id, payload in rows]

    def delete(self, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM arc_guess_sessions WHERE group_id = ?", (str(group_id),))
            connection.commit()
