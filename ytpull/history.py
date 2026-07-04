"""Per-chat download history, persisted in SQLite.

Each downloaded document is stamped with a sequential number as a hashtag (e.g.
``#0007``) in its caption. The history is shown on demand (via a reply-keyboard
button) as a message listing those numbers next to the video title, grouped by
channel — tapping/searching ``#0007`` jumps to the document. Entries can be deleted
from an edit view. Everything lives in SQLite so history survives restarts.
"""

from __future__ import annotations

import sqlite3
import threading

HEADER = "📥 История загрузок"
PAGE_SIZE = 10  # history entries shown per page


class HistoryDB:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        with self._connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS chats ("
                " chat_id INTEGER PRIMARY KEY,"
                " pinned_message_id INTEGER,"
                " last_num INTEGER NOT NULL DEFAULT 0,"
                " hist_message_id INTEGER)"
            )
            # Migrate older DBs that predate the hist_message_id column.
            cols = [r[1] for r in c.execute("PRAGMA table_info(chats)").fetchall()]
            if "hist_message_id" not in cols:
                c.execute("ALTER TABLE chats ADD COLUMN hist_message_id INTEGER")
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

    def hist_message_id(self, chat_id: int) -> int | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT hist_message_id FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return row["hist_message_id"] if row else None

    def set_hist_message_id(self, chat_id: int, message_id: int | None) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO chats (chat_id, hist_message_id) VALUES (?, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET hist_message_id = excluded.hist_message_id",
                (chat_id, message_id),
            )

    def record(self, chat_id: int, num: int, channel: str, title: str,
               quality: str, url: str, doc_message_id: int | None) -> None:
        """Save a download to history."""
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO downloads"
                " (chat_id, num, channel, title, quality, url, doc_message_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, num, channel, title, quality, url, doc_message_id),
            )

    def records(self, chat_id: int) -> list[dict]:
        """All history rows for a chat, newest first (for the edit list)."""
        with self._connect() as c:
            rows = c.execute(
                "SELECT id, num, channel, title FROM downloads"
                " WHERE chat_id = ? ORDER BY id DESC",
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, chat_id: int, record_id: int) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "DELETE FROM downloads WHERE id = ? AND chat_id = ?",
                (record_id, chat_id),
            )

    def page_count(self, chat_id: int) -> int:
        n = len(self.records(chat_id))
        return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)

    def page_records(self, chat_id: int, page: int) -> list[dict]:
        """The (clamped) page's records, newest first."""
        recs = self.records(chat_id)
        page = max(0, min(page, self._pages(len(recs)) - 1))
        return recs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    @staticmethod
    def _pages(n: int) -> int:
        return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)

    def render_page(self, chat_id: int, page: int) -> tuple[str, int, int]:
        """Render one page of history. Returns (text, total_pages, clamped_page)."""
        recs = self.records(chat_id)  # newest first
        pages = self._pages(len(recs))
        page = max(0, min(page, pages - 1))
        chunk = recs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        # Group the page's rows by channel, in order of first appearance.
        order: list[str] = []
        groups: dict[str, list[dict]] = {}
        for r in chunk:
            groups.setdefault(r["channel"], []).append(r)
            if r["channel"] not in order:
                order.append(r["channel"])
        blocks = [f"{HEADER}  (стр. {page + 1}/{pages})", ""]
        for chan in order:
            blocks.append(f"#{chan}:")
            for r in groups[chan]:
                blocks.append(f"  • #N{r['num']:04d} — {r['title']}")
            blocks.append("")
        return "\n".join(blocks).strip(), pages, page
