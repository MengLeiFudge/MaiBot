from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import TypeVar


T = TypeVar("T")
RUNTIME_DB_FILE_NAME = "qqbot_features.sqlite3"


def resolve_runtime_db_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "db" / RUNTIME_DB_FILE_NAME


def infer_runtime_root_from_path(path: Path) -> Path:
    candidate = Path(path)
    for parent in (candidate, *candidate.parents):
        if parent.name == "qqbot_features_runtime":
            return parent
    if candidate.parent.parent.name == "db":
        return candidate.parent.parent.parent
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
    """兼容 QQBot 共享 SQLite json_state 表的小状态存储。"""

    def __init__(self, runtime_root: Path) -> None:
        self.db_path = resolve_runtime_db_path(runtime_root)

    def read_with_legacy(
        self,
        namespace: str,
        default: T,
        legacy_loader: Callable[[], T | None],
    ) -> T:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "select payload from json_state where namespace=?",
                (namespace,),
            ).fetchone()
        if row is not None:
            return json.loads(str(row[0]))
        legacy_payload = legacy_loader()
        if legacy_payload is None:
            return deepcopy(default)
        self.write(namespace, legacy_payload)
        return legacy_payload

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

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
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
