"""MaiBot 插件独立持有的批次队列，不依赖 AstrBot 的目录或进程。"""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import time
import uuid


class Queue:
    """SQLite 同线程事务保存原文、汇总、调用预算与传输回执。"""

    def __init__(self, path: Path, binding: dict, capacity: int):
        """首次固定绑定；更换世代必须显式归档旧队列后重新配置。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.capacity = capacity
        try:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, binding TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (group_id TEXT,message_id TEXT,sender_id TEXT,received_at INTEGER,body TEXT,batch_id TEXT,PRIMARY KEY(group_id,message_id));
                CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY,group_id TEXT,summary TEXT,attempted INTEGER DEFAULT 0,state TEXT DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY,attempts INTEGER);
                CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY,payload TEXT);
            """)
            encoded = json.dumps(binding, sort_keys=True)
            with self.db:
                old = self.db.execute("SELECT binding FROM settings WHERE id=1").fetchone()
                if old and old[0] != encoded:
                    raise ValueError("绑定已变化，请归档旧队列后重新绑定")
                self.db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (encoded,))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        """释放插件自身的连接。"""
        self.db.close()

    def expire(self) -> int:
        """七天从原始收件时间起算；丢弃原文但保留摘要。"""
        cutoff = int(time.time() * 1000) - 604800000
        with self.db:
            self.db.execute("UPDATE batches SET state='expired' WHERE state='pending' AND id IN (SELECT batch_id FROM items WHERE received_at<=?)", (cutoff,))
            count = self.db.execute("UPDATE items SET body=NULL WHERE received_at<=? AND body IS NOT NULL", (cutoff,)).rowcount
            self.db.execute("DELETE FROM items WHERE body IS NULL AND received_at<?", (cutoff - 604800000,))
        return count

    def collect(self, group: str, message: str, sender: str, body: str):
        """收到明确需求后先持久化，容量不足明确拒收。"""
        if not group.isdecimal() or not sender.isdecimal() or not message or len(message) > 128 or not body.strip() or len(body.encode()) > 8192:
            raise ValueError("需求来源或长度无效")
        with self.db:
            if self.db.execute("SELECT 1 FROM items WHERE group_id=? AND message_id=?", (group, message)).fetchone():
                return
            size, count = self.db.execute("SELECT coalesce(sum(length(CAST(body AS BLOB))),0),count(*) FROM items WHERE body IS NOT NULL").fetchone()
            if size + len(body.encode()) > self.capacity or count >= 10000:
                raise ValueError("原文队列已满")
            self.db.execute("INSERT INTO items VALUES(?,?,?,?,?,NULL)", (group, message, sender, int(time.time() * 1000), body))

    def next_batch(self, count: int, delay: int) -> dict | None:
        """优先重发已冻结批次；新批次始终只包含一个群。"""
        now = int(time.time() * 1000)
        with self.db:
            row = self.db.execute("SELECT * FROM batches WHERE state='pending' AND (summary IS NOT NULL OR attempted<=?) ORDER BY attempted,id LIMIT 1", (now - 1800000,)).fetchone()
            if not row:
                group = self.db.execute("SELECT group_id FROM items WHERE batch_id IS NULL AND body IS NOT NULL GROUP BY group_id HAVING count(*)>=? OR min(received_at)<=? ORDER BY min(received_at) LIMIT 1", (count, now - delay)).fetchone()
                if not group:
                    return None
                identifier = str(uuid.uuid4())
                self.db.execute("INSERT INTO batches(id,group_id) VALUES(?,?)", (identifier, group[0]))
                self.db.execute("UPDATE items SET batch_id=? WHERE group_id=? AND message_id IN (SELECT message_id FROM items WHERE group_id=? AND batch_id IS NULL AND body IS NOT NULL ORDER BY received_at,message_id LIMIT 50)", (identifier, group[0], group[0]))
                row = self.db.execute("SELECT * FROM batches WHERE id=?", (identifier,)).fetchone()
            batch = dict(row)
            batch["items"] = [dict(value) for value in self.db.execute("SELECT message_id,sender_id,received_at,body FROM items WHERE batch_id=? ORDER BY received_at,message_id", (batch["id"],))]
            return batch

    def charge(self, identifier: str, limit: int) -> bool:
        """网络调用前记录UTC日尝试预算，失败和中断也计数。"""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.db:
            used = self.db.execute("SELECT attempts FROM budget WHERE day=?", (day,)).fetchone()
            if used and used[0] >= limit:
                return False
            self.db.execute("INSERT INTO budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET attempts=attempts+1", (day,))
            self.db.execute("UPDATE batches SET attempted=? WHERE id=?", (int(time.time() * 1000), identifier))
        return True

    def summarize(self, identifier: str, summary: str):
        """先落摘要再发送，使HTTP重试不产生第二次模型调用。"""
        if not summary.strip() or len(summary.encode()) > 8192:
            raise ValueError("摘要为空或超限")
        with self.db:
            self.db.execute("UPDATE batches SET summary=? WHERE id=? AND state='pending'", (summary, identifier))

    def delivered(self, identifier: str):
        """桥接提交成功后释放本地原文。"""
        with self.db:
            self.db.execute("UPDATE batches SET state='delivered' WHERE id=?", (identifier,))
            self.db.execute("UPDATE items SET body=NULL WHERE batch_id=?", (identifier,))

    def sent(self, identifier: str, mark: bool = False) -> bool:
        """记录QQ已发送状态，避免ack丢失后再次发送。"""
        with self.db:
            if mark:
                self.db.execute("INSERT OR IGNORE INTO sent VALUES(?)", (identifier,))
            return self.db.execute("SELECT 1 FROM sent WHERE id=?", (identifier,)).fetchone() is not None

    def reply(self, identifier: str, payload: dict):
        """离线保存主人命令；不让LLM提供来源信息。"""
        with self.db:
            if self.db.execute("SELECT count(*) FROM replies").fetchone()[0] >= 1000:
                raise ValueError("决定队列已满")
            self.db.execute("INSERT OR IGNORE INTO replies VALUES(?,?)", (identifier, json.dumps(payload, ensure_ascii=False)))

    def replies(self) -> list[dict]:
        """读取有界的待发送决定回复。"""
        return [{"id": row[0], "payload": json.loads(row[1])} for row in self.db.execute("SELECT id,payload FROM replies ORDER BY rowid LIMIT 20")]

    def ack_reply(self, identifier: str):
        """明确成功或拒绝后删除本地传输副本。"""
        with self.db:
            self.db.execute("DELETE FROM replies WHERE id=?", (identifier,))
