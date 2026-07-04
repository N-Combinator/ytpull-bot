"""Per-chat download history, persisted in SQLite and mirrored to a pinned message.

Telegram has no way to deep-link a message inside a private (1:1) chat, so instead
each downloaded document is stamped with a sequential number as a hashtag (e.g.
``#0007``) in its caption. The pinned history lists those numbers next to the video
title, grouped by channel — tapping/searching ``#0007`` jumps to the document.

Everything lives in SQLite so history (and the pinned-message id) survives restarts
and redeploys; nothing is kept only in process memory.
"""

from __future__ import annotations

import sqlite3
import threading

HEADER = "📥 История загрузок"
_MAX_LEN = 4000  # keep under Telegram's 4096-char message limit


class HistoryDB:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        with self._connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS chats ("
                " chat_id INTEGER PRIMARY KEY,"
                " pinned_message_id INTEGER,"
                " last_num INTEGER NOT NULL DEFAULT 0)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS downloads ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " chat_id INTEGER NOT NULL,"
                " num INTEGER NOT NULL,"
                " channel TEXT NOT NULL,"
                " title TEXT NOT NULL,"
                " quality TEXT,"
                " url TEXT,"
                " doc_message_id INTEGER)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def next_number(self, chat_id: int) -> int:
        """Reserve and return the next per-chat sequence number (at send time)."""
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO chats (chat_id, last_num) VALUES (?, 1)"
                " ON CONFLICT(chat_id) DO UPDATE SET last_num = last_num + 1",
                (chat_id,),
            )
            return c.execute(
                "SELECT last_num FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]

    def record(self, chat_id: int, num: int, channel: str, title: str,
               quality: str, url: str, doc_message_id: int | None) -> str:
        """Save a download to history; return the rendered pinned text."""
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO downloads"
                " (chat_id, num, channel, title, quality, url, doc_message_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, num, channel, title, quality, url, doc_message_id),
            )
        return self.render(chat_id)

    def pinned_id(self, chat_id: int) -> int | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT pinned_message_id FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return row["pinned_message_id"] if row else None

    def set_pinned(self, chat_id: int, message_id: int | None) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO chats (chat_id, pinned_message_id) VALUES (?, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET pinned_message_id = excluded.pinned_message_id",
                (chat_id, message_id),
            )

    def render(self, chat_id: int) -> str:
        with self._connect() as c:
            rows = c.execute(
                "SELECT num, channel, title FROM downloads WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        # Group by channel, most-recently-active channel first.
        order: list[str] = []
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault(r["channel"], []).append(r)
            if r["channel"] in order:
                order.remove(r["channel"])
            order.append(r["channel"])

        while True:
            blocks = [HEADER, ""]
            for chan in reversed(order):
                blocks.append(f"#{chan}:")
                for r in reversed(groups[chan]):
                    blocks.append(f"  • #{r['num']:04d} — {r['title']}")
                blocks.append("")
            text = "\n".join(blocks).strip()
            if len(text) <= _MAX_LEN or len(order) <= 1:
                return text
            drop = order.pop(0)  # trim oldest channel until it fits
            groups.pop(drop, None)
