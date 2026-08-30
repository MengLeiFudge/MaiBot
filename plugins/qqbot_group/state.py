from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Iterable

import json
import sqlite3
import time


class GroupStateStore:
    """Persist social-request and group-file state in plugin-owned SQLite tables."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def remember_inviter(self, self_id: str, group_id: str, inviter_id: str) -> None:
        if not self_id or not group_id or not inviter_id:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                insert into maibot_group_invites(self_id, group_id, inviter_id, updated_at)
                values (?, ?, ?, ?)
                on conflict(self_id, group_id) do update set
                    inviter_id=excluded.inviter_id,
                    updated_at=excluded.updated_at
                """,
                (self_id, group_id, inviter_id, time.time()),
            )

    def pop_inviter(self, self_id: str, group_id: str) -> str:
        with closing(self._connect()) as connection, connection:
            connection.execute("begin immediate")
            row = connection.execute(
                "select inviter_id from maibot_group_invites where self_id=? and group_id=?",
                (self_id, group_id),
            ).fetchone()
            connection.execute(
                "delete from maibot_group_invites where self_id=? and group_id=?",
                (self_id, group_id),
            )
        return str(row[0]).strip() if row is not None else ""

    def record_protocol_audit(
        self,
        *,
        event_kind: str,
        action: str,
        sub_type: str,
        outcome: str,
        failure_reason: str,
        flag: str,
        self_id: str,
        group_id: str,
        user_id: str,
    ) -> None:
        if outcome not in {"success", "failure", "skipped"}:
            raise ValueError("outcome must be success, failure, or skipped")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                insert into maibot_group_protocol_audit(
                    event_kind, action, sub_type, outcome, success,
                    failure_reason, flag, self_id, group_id, user_id, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_kind,
                    action,
                    sub_type,
                    outcome,
                    int(outcome == "success"),
                    failure_reason,
                    flag,
                    self_id,
                    group_id,
                    user_id,
                    time.time(),
                ),
            )

    def protocol_audits(self) -> list[dict[str, object]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                select event_kind, action, sub_type, outcome, success,
                       failure_reason, flag, self_id, group_id, user_id
                from maibot_group_protocol_audit
                order by id
                """
            ).fetchall()
        return [
            {
                "event_kind": str(row[0]),
                "action": str(row[1]),
                "sub_type": str(row[2]),
                "outcome": str(row[3]),
                "success": bool(row[4]),
                "failure_reason": str(row[5]),
                "flag": str(row[6]),
                "self_id": str(row[7]),
                "group_id": str(row[8]),
                "user_id": str(row[9]),
            }
            for row in rows
        ]

    def save_pending_cleanup(
        self,
        *,
        group_id: str,
        user_id: str,
        file_ids: Iterable[str],
        file_names: Iterable[str],
        muted_until: int,
        checked_at: int,
    ) -> None:
        ids = [str(item) for item in file_ids]
        names = [str(item) for item in file_names]
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                insert into maibot_group_file_cleanup_pending(
                    group_id, user_id, file_ids_json, file_names_json,
                    last_checked_at, muted_until, status
                ) values (?, ?, ?, ?, ?, ?, 'pending')
                on conflict(group_id, user_id) do update set
                    file_ids_json=excluded.file_ids_json,
                    file_names_json=excluded.file_names_json,
                    last_checked_at=excluded.last_checked_at,
                    muted_until=excluded.muted_until,
                    status='pending'
                """,
                (
                    group_id,
                    user_id,
                    json.dumps(ids, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(names, ensure_ascii=False, separators=(",", ":")),
                    int(checked_at),
                    int(muted_until),
                ),
            )

    def pending_cleanup(self, group_id: str, user_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                select file_ids_json, file_names_json, last_checked_at, muted_until, status
                from maibot_group_file_cleanup_pending
                where group_id=? and user_id=?
                """,
                (group_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "file_ids": tuple(str(item) for item in json.loads(str(row[0]))),
            "file_names": tuple(str(item) for item in json.loads(str(row[1]))),
            "last_checked_at": int(row[2]),
            "muted_until": int(row[3]),
            "status": str(row[4]),
        }

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                create table if not exists maibot_group_invites (
                    self_id text not null,
                    group_id text not null,
                    inviter_id text not null,
                    updated_at real not null,
                    primary key(self_id, group_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists maibot_group_protocol_audit (
                    id integer primary key autoincrement,
                    event_kind text not null,
                    action text not null,
                    sub_type text not null,
                    outcome text not null,
                    success integer not null,
                    failure_reason text not null,
                    flag text not null,
                    self_id text not null,
                    group_id text not null,
                    user_id text not null,
                    created_at real not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists maibot_group_file_cleanup_pending (
                    group_id text not null,
                    user_id text not null,
                    file_ids_json text not null,
                    file_names_json text not null,
                    last_checked_at integer not null,
                    muted_until integer not null,
                    status text not null,
                    primary key(group_id, user_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        return connection
