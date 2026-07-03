"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from dotenv import load_dotenv

# Telegram upload limits (bytes).
PUBLIC_API_UPLOAD_LIMIT = 50 * 1024 * 1024          # 50 MB on api.telegram.org
LOCAL_API_UPLOAD_LIMIT = 2000 * 1024 * 1024         # 2000 MB — max a local Bot API server accepts


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_base: str | None
    download_dir: str
    log_level: str

    @property
    def upload_limit(self) -> int:
        """Max file size we can send back, depending on the API in use."""
        return LOCAL_API_UPLOAD_LIMIT if self.api_base else PUBLIC_API_UPLOAD_LIMIT


def load_config() -> Config:
    """Read configuration from the environment (and a local .env if present)."""
    load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and add "
            "your token from @BotFather."
        )

    api_base = os.environ.get("TELEGRAM_API_BASE", "").strip() or None

    download_dir = os.environ.get("DOWNLOAD_DIR", "").strip()
    if not download_dir:
        download_dir = os.path.join(tempfile.gettempdir(), "ytpull-bot")
    os.makedirs(download_dir, exist_ok=True)

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return Config(
        bot_token=token,
        api_base=api_base,
        download_dir=download_dir,
        log_level=log_level,
    )
