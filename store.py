"""SQLite 持久化：会话映射、去重、消息日志。"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    wxid        TEXT PRIMARY KEY,
    alias       TEXT,
    session_id  TEXT,
    is_group    INTEGER DEFAULT 0,
    first_seen  REAL,
    last_seen   REAL,
    msg_count   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (
    msgid TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid     TEXT,
    sender    TEXT,
    room      TEXT,
    direction TEXT,          -- in / out
    content   TEXT,
    ts        REAL,
    elapsed   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msgid ON messages(msgid, direction);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- 会话 ----
    def get_session(self, wxid: str) -> Optional[str]:
        with self._lock:
            row = self.db.execute(
                "SELECT session_id FROM contacts WHERE wxid=?", (wxid,)
            ).fetchone()
        return row[0] if row and row[0] else None

    def save_session(self, wxid: str, session_id: str) -> None:
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO contacts (wxid, session_id, first_seen, last_seen)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(wxid) DO UPDATE SET session_id=excluded.session_id,
                                                   last_seen=excluded.last_seen""",
                (wxid, session_id, now, now),
            )
            self.db.commit()

    def touch(self, wxid: str, alias: str = "", is_group: bool = False) -> None:
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO contacts (wxid, alias, is_group, first_seen, last_seen, msg_count)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(wxid) DO UPDATE SET last_seen=excluded.last_seen,
                                                   msg_count=contacts.msg_count+1,
                                                   alias=COALESCE(NULLIF(excluded.alias,''), contacts.alias)""",
                (wxid, alias, 1 if is_group else 0, now, now),
            )
            self.db.commit()

    def reset_session(self, wxid: str) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE contacts SET session_id=NULL WHERE wxid=?", (wxid,)
            )
            self.db.commit()
        return cur.rowcount > 0

    # ---- 消息 ----
    def log(self, msgid: str, sender: str, room: Optional[str], direction: str,
            content: str, elapsed: float = 0.0) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO messages (msgid, sender, room, direction, content, ts, elapsed)"
                " VALUES (?,?,?,?,?,?,?)",
                (msgid, sender, room or "", direction, content[:4000], time.time(), elapsed),
            )
            self.db.commit()

    def is_duplicate(self, msgid: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM messages WHERE msgid=? AND direction='in' LIMIT 1", (msgid,)
            ).fetchone()
        return row is not None

    def max_ts(self, direction: str = "in") -> float:
        """某方向消息里最大的时间戳（用来做"启动补收"的起点）。"""
        with self._lock:
            row = self.db.execute(
                "SELECT MAX(ts) FROM messages WHERE direction=?", (direction,)
            ).fetchone()
        try:
            return float(row[0]) if row and row[0] is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def unanswered(self, minutes: float = 20, limit: int = 5) -> list[tuple]:
        """最近一段时间里"收到了但没回"的消息（进程半路挂掉就会留下这种债）。"""
        cutoff = time.time() - minutes * 60
        with self._lock:
            return self.db.execute(
                "SELECT m.msgid, m.sender, m.room, m.content, m.ts FROM messages m"
                " WHERE m.direction='in' AND m.ts > ?"
                "   AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.direction='out'"
                "        AND (o.sender=m.sender OR (m.room<>'' AND o.room=m.room))"
                "        AND o.ts > m.ts)"
                " ORDER BY m.ts ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()

    def recent(self, limit: int = 20) -> list[tuple]:
        with self._lock:
            return self.db.execute(
                "SELECT ts, direction, sender, room, content FROM messages"
                " ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def stats(self) -> dict:
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            people = self.db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            today = self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE ts > ?", (time.time() - 86400,)
            ).fetchone()[0]
        return {"messages": total, "contacts": people, "last_24h": today}

    def mark_seen(self, msgid: str) -> None:
        """记住"这条看过了、不用回"，避免补收每轮把它再拖出来重下一次图。"""
        try:
            self.db.execute("INSERT OR IGNORE INTO seen(msgid, ts) VALUES (?, ?)",
                            (msgid, time.time()))
            self.db.commit()
        except Exception:
            pass

    def is_seen(self, msgid: str) -> bool:
        row = self.db.execute("SELECT 1 FROM seen WHERE msgid=?", (msgid,)).fetchone()
        return row is not None
