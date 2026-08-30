from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .service import LoliconImageItem


@dataclass(frozen=True, slots=True)
class LoliconGroupConfig:
    group_r18: bool = False
    show_image: bool = False


class LoliconGroupConfigStore:
    """Read and update the legacy-compatible settings.lolicon JSON namespace."""

    def __init__(self, runtime_root: Path) -> None:
        self.db_path = Path(runtime_root) / "db" / "qqbot_features.sqlite3"

    def get(self, group_id: str | int) -> LoliconGroupConfig:
        payload = self._read_payload()
        raw = payload.get(str(group_id))
        if not isinstance(raw, dict):
            return LoliconGroupConfig()
        return LoliconGroupConfig(
            group_r18=bool(raw.get("group_r18", False)),
            show_image=bool(raw.get("show_image", False)),
        )

    def set(self, group_id: str | int, config: LoliconGroupConfig) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("begin immediate")
            row = connection.execute(
                "select payload from json_state where namespace=?",
                ("settings.lolicon",),
            ).fetchone()
            payload = self._decode_payload(row[0] if row is not None else None)
            payload[str(group_id)] = {
                "group_r18": bool(config.group_r18),
                "show_image": bool(config.show_image),
            }
            connection.execute(
                """
                insert into json_state(namespace, payload, updated_at)
                values (?, ?, datetime('now'))
                on conflict(namespace) do update set
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                ("settings.lolicon", json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )

    def _read_payload(self) -> dict[str, object]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "select payload from json_state where namespace=?",
                ("settings.lolicon",),
            ).fetchone()
        return self._decode_payload(row[0] if row is not None else None)

    @staticmethod
    def _decode_payload(raw: object) -> dict[str, object]:
        if raw is None:
            return {}
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        connection.execute(
            """
            create table if not exists json_state (
                namespace text primary key,
                payload text not null,
                created_at text not null default (datetime('now')),
                updated_at text not null default (datetime('now'))
            )
            """
        )
        connection.commit()
        return connection


class LoliconMetadataStore:
    """Persist Lolicon API metadata without caching remote image bytes."""

    def __init__(self, runtime_root: Path) -> None:
        self.db_path = Path(runtime_root) / "db" / "lolicon.sqlite3"

    def upsert(self, item: LoliconImageItem) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                insert into images (
                    pid, page, uid, title, author, r18, width, height, tags, ext,
                    ai_type, upload_date, url
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(pid, page) do update set
                    uid=excluded.uid,
                    title=excluded.title,
                    author=excluded.author,
                    r18=excluded.r18,
                    width=excluded.width,
                    height=excluded.height,
                    tags=excluded.tags,
                    ext=excluded.ext,
                    ai_type=excluded.ai_type,
                    upload_date=excluded.upload_date,
                    url=excluded.url,
                    updated_at=datetime('now')
                """,
                (
                    item.pid,
                    item.page,
                    item.uid,
                    item.title,
                    item.author,
                    int(item.r18),
                    item.width,
                    item.height,
                    json.dumps(list(item.tags), ensure_ascii=False),
                    item.ext,
                    item.ai_type,
                    item.upload_date,
                    item.url,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        connection.execute(
            """
            create table if not exists images (
                pid integer not null,
                page integer not null,
                uid integer not null,
                title text not null,
                author text not null,
                r18 integer not null,
                width integer not null,
                height integer not null,
                tags text not null,
                ext text not null,
                ai_type integer not null,
                upload_date integer not null,
                url text not null,
                local_path text not null default '',
                created_at text not null default (datetime('now')),
                updated_at text not null default (datetime('now')),
                primary key (pid, page)
            )
            """
        )
        connection.commit()
        return connection
