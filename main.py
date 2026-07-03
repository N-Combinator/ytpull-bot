"""Entrypoint: load config, build the bot, run polling."""

from __future__ import annotations

import logging

from ytpull.bot import build_application
from ytpull.config import load_config


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_application(cfg)
    logging.getLogger(__name__).info(
        "ytpull-bot starting (upload limit: %d MB, ffmpeg: %s)",
        cfg.upload_limit // (1024 * 1024),
        app.bot_data["ffmpeg"],
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
