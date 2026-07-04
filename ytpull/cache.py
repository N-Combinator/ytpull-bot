"""Short-lived cache mapping a compact token to an extracted-info entry.

Telegram ``callback_data`` is capped at 64 bytes, so we cannot stuff a URL plus
format details into a button. Instead we cache the extraction result under a
short token and only put ``dl:<token>:<key>`` into the callback.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entry:
    url: str
    title: str
    info: dict[str, Any]
    options: list[Any]
    user_msg_id: int | None = None  # the user's original link message, to delete after send
    created: float = field(default_factory=time.monotonic)


class ExtractCache:
    def __init__(self, ttl: float = 1800.0) -> None:
        self._store: dict[str, Entry] = {}
        self._ttl = ttl

    def _evict(self) -> None:
        now = time.monotonic()
        stale = [t for t, e in self._store.items() if now - e.created > self._ttl]
        for t in stale:
            self._store.pop(t, None)

    def put(
        self,
        url: str,
        title: str,
        info: dict[str, Any],
        options: list[Any],
        user_msg_id: int | None = None,
    ) -> str:
        self._evict()
        token = uuid.uuid4().hex[:10]
        self._store[token] = Entry(
            url=url, title=title, info=info, options=options, user_msg_id=user_msg_id
        )
        return token

    def get(self, token: str) -> Entry | None:
        self._evict()
        return self._store.get(token)
