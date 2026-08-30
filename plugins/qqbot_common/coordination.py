from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Iterable

import hashlib
import sqlite3
import time


class CoordinationStore:
    """保存跨 MaiBot 实例的短期仲裁和群昵称缓存。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._initialize()

    def claim_command(
        self,
        *,
        feature: str,
        text: str,
        user_id: str,
        group_id: str,
        self_id: str,
        timestamp: float,
        at_target_ids: Iterable[str],
        bot_account_ids: Iterable[str],
        ttl_seconds: int,
        bucket_seconds: int,
    ) -> bool:
        """按目标账号或共享唯一键决定当前实例是否执行命令。"""

        bot_ids = tuple(sorted({str(item).strip() for item in bot_account_ids if str(item).strip()}))
        if user_id.strip() in bot_ids:
            return False
        targets = tuple(sorted(set(at_target_ids).intersection(bot_ids)))
        if targets:
            if len(targets) == 1:
                return self_id == targets[0]
            winner_seed = self._canonical_key(feature, text, user_id, group_id, timestamp, bucket_seconds)
            winner_index = int(hashlib.sha256(winner_seed.encode("utf-8")).hexdigest(), 16) % len(targets)
            return self_id == targets[winner_index]

        if not group_id:
            return True

        claim_key = self._canonical_key(feature, text, user_id, group_id, timestamp, bucket_seconds)
        now = time.time()
        expires_at = now + max(1, int(ttl_seconds))
        with closing(self._connect()) as connection, connection:
            connection.execute("begin immediate")
            connection.execute("delete from maibot_command_claims where expires_at <= ?", (now,))
            connection.execute(
                """
                insert or ignore into maibot_command_claims(claim_key, owner_self_id, expires_at)
                values (?, ?, ?)
                """,
                (claim_key, self_id, expires_at),
            )
            row = connection.execute(
                "select owner_self_id from maibot_command_claims where claim_key=?",
                (claim_key,),
            ).fetchone()
        return row is not None and str(row[0]) == self_id

    def remember_name(
        self,
        *,
        group_id: str,
        user_id: str,
        nickname: str,
        cardname: str,
        updated_at: float,
    ) -> None:
        """更新群维度名片和 QQ 昵称。"""

        if not group_id or not user_id:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                insert into maibot_group_names(group_id, user_id, nickname, cardname, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(group_id, user_id) do update set
                    nickname=excluded.nickname,
                    cardname=excluded.cardname,
                    updated_at=excluded.updated_at
                """,
                (group_id, user_id, nickname, cardname, updated_at or time.time()),
            )

    def resolve_display_name(self, group_id: str, user_id: str) -> str:
        """按当前群名片、当前群昵称、其他群最新昵称、QQ 的顺序解析称呼。"""

        if not user_id:
            return ""
        with closing(self._connect()) as connection:
            if group_id:
                row = connection.execute(
                    """
                    select cardname, nickname from maibot_group_names
                    where group_id=? and user_id=?
                    """,
                    (group_id, user_id),
                ).fetchone()
                if row is not None:
                    cardname = str(row[0] or "").strip()
                    nickname = str(row[1] or "").strip()
                    if cardname or nickname:
                        return cardname or nickname
            row = connection.execute(
                """
                select nickname from maibot_group_names
                where user_id=? and nickname<>''
                order by updated_at desc limit 1
                """,
                (user_id,),
            ).fetchone()
        return str(row[0]).strip() if row is not None and str(row[0]).strip() else user_id

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                create table if not exists maibot_command_claims (
                    claim_key text primary key,
                    owner_self_id text not null,
                    expires_at real not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists maibot_group_names (
                    group_id text not null,
                    user_id text not null,
                    nickname text not null default '',
                    cardname text not null default '',
                    updated_at real not null,
                    primary key(group_id, user_id)
                )
                """
            )
            connection.execute(
                "create index if not exists idx_maibot_group_names_user on maibot_group_names(user_id, updated_at desc)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        return connection

    @staticmethod
    def _canonical_key(
        feature: str,
        text: str,
        user_id: str,
        group_id: str,
        timestamp: float,
        bucket_seconds: int,
    ) -> str:
        normalized_text = " ".join(text.split()).casefold()
        effective_timestamp = timestamp if timestamp > 0 else time.time()
        bucket = int(effective_timestamp // max(1, int(bucket_seconds)))
        raw = "\x1f".join((feature.strip().casefold(), group_id, user_id, normalized_text, str(bucket)))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
