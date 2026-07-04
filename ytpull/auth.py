"""Password gate: a single shared access password derived from a secret seed.

The password is ``sha256("NickOl Family" + AUTH_SEED)`` — the seed lives in the
environment (``.env``), never in the repo. A user is let in once they send the
correct password; their Telegram id is then remembered in ``authorized.json`` so
they never have to type it again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_PREFIX = "NickOl Family"


def password_for(seed: str) -> str:
    """The access password derived from the secret seed."""
    return hashlib.sha256((_PREFIX + seed).encode()).hexdigest()


class AuthStore:
    """Tracks which Telegram user ids have unlocked the bot (persisted to disk)."""

    def __init__(self, seed: str, path: str) -> None:
        self._seed = seed
        self._path = path
        self._lock = threading.Lock()
        self._ids: set[int] = self._load()

    @property
    def enabled(self) -> bool:
        return bool(self._seed)

    @property
    def password(self) -> str:
        return password_for(self._seed)

    def _load(self) -> set[int]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                return {int(x) for x in json.load(fh)}
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return set()

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(self._ids), fh)
        os.replace(tmp, self._path)

    def is_authorized(self, user_id: int) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            return user_id in self._ids

    def check_password(self, text: str) -> bool:
        """True if `text` matches the access password (constant-time compare)."""
        import hmac

        return hmac.compare_digest(text.strip(), self.password)

    def authorize(self, user_id: int) -> None:
        with self._lock:
            if user_id not in self._ids:
                self._ids.add(user_id)
                self._save()
                log.info("authorized new user %s", user_id)
