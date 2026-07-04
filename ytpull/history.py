"""Per-chat download history, rendered into a single pinned message.

The history is grouped by channel: a channel hashtag heading with the video-title
hashtags listed beneath it, e.g.::

    📥 История загрузок

    #DeadP47:
     - #GTA_6_Trailer
     - #Another_clip

State is persisted as JSON so it survives restarts; the rendered text is what the
bot keeps in the pinned message.
"""

from __future__ import annotations

import json
import os
import threading

HEADER = "📥 История загрузок"
_MAX_LEN = 4000  # keep under Telegram's 4096-char message limit


class HistoryStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False)
        os.replace(tmp, self._path)

    def _chat(self, chat_id: int) -> dict:
        return self._data.setdefault(str(chat_id), {"pinned": None, "channels": {}})

    def pinned_id(self, chat_id: int) -> int | None:
        return self._chat(chat_id).get("pinned")

    def set_pinned(self, chat_id: int, message_id: int | None) -> None:
        with self._lock:
            self._chat(chat_id)["pinned"] = message_id
            self._save()

    def add(self, chat_id: int, channel_tag: str, title_tag: str) -> str:
        """Record a channel/video and return the freshly rendered pinned text."""
        with self._lock:
            channels: dict = self._chat(chat_id)["channels"]
            vids = channels.setdefault(channel_tag, [])
            if title_tag in vids:
                vids.remove(title_tag)  # move to most-recent position
            vids.append(title_tag)
            self._save()
        return self.render(chat_id)

    def render(self, chat_id: int) -> str:
        channels: dict = self._chat(chat_id)["channels"]
        # Newest channels first; drop the oldest if we run past the length limit.
        items = list(channels.items())
        while True:
            blocks = [HEADER, ""]
            for chan, vids in reversed(items):
                blocks.append(f"#{chan}:")
                blocks.extend(f" - #{v}" for v in vids)
                blocks.append("")
            text = "\n".join(blocks).strip()
            if len(text) <= _MAX_LEN or len(items) <= 1:
                return text
            items = items[1:]  # drop oldest channel and re-render
