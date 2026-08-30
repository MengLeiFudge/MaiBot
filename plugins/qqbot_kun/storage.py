from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import TypeVar


T = TypeVar("T")


RUNTIME_DB_DIR = "db"
RUNTIME_DB_FILE_NAME = "qqbot_features.sqlite3"


def resolve_runtime_db_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / RUNTIME_DB_DIR / RUNTIME_DB_FILE_NAME


def infer_runtime_root_from_path(path: Path) -> Path:
    candidate = Path(path)
    for parent in (candidate, *candidate.parents):
        if parent.name == "qqbot_features_runtime":
            return parent
    if candidate.parent.parent.name == "data":
        return candidate.parent.parent.parent
    if candidate.parent.name in {"ai", "settings", "cache", "assets", "db"}:
        return candidate.parent.parent
    return candidate.parent


def read_json_file(path: Path, default: T) -> T:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


class RuntimeJsonStore:
    """Small SQLite-backed JSON state store for migrated qqbot runtime data.

    The migrated services mostly need to load and replace compact structured
    payloads. A JSON-state table keeps the migration low-risk while moving the
    filesystem fact source into a single plugin-owned database.
    """

    def __init__(self, runtime_root: Path | None = None, *, db_path: Path | None = None) -> None:
        if db_path is None:
            if runtime_root is None:
                raise ValueError("runtime_root or db_path is required")
            db_path = resolve_runtime_db_path(runtime_root)
        self.db_path = Path(db_path)

    def read(self, namespace: str, default: T) -> T:
        raw = self._read_raw(namespace)
        if raw is None:
            return deepcopy(default)
        return json.loads(raw)

    def read_with_legacy(
        self,
        namespace: str,
        default: T,
        legacy_loader: Callable[[], T | None],
    ) -> T:
        raw = self._read_raw(namespace)
        if raw is not None:
            return json.loads(raw)
        legacy_payload = legacy_loader()
        if legacy_payload is not None:
            self.write(namespace, legacy_payload)
            return legacy_payload
        return deepcopy(default)

    def write(self, namespace: str, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                insert into json_state(namespace, payload, updated_at)
                values (?, ?, datetime('now'))
                on conflict(namespace) do update set
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (namespace, encoded),
            )

    def delete(self, namespace: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("delete from json_state where namespace=?", (namespace,))

    def _read_raw(self, namespace: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "select payload from json_state where namespace=?",
                (namespace,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        # Keep the runtime footprint to one SQLite file instead of persistent
        # -wal/-shm sidecar files; these states are small and do not need WAL.
        conn.execute("pragma journal_mode=delete")
        conn.execute(
            """
            create table if not exists json_state (
                namespace text primary key,
                payload text not null,
                created_at text not null default (datetime('now')),
                updated_at text not null default (datetime('now'))
            )
            """
        )
        conn.commit()
        return conn
