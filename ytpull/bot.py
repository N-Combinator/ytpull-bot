"""Telegram handlers: link -> quality menu -> download -> send file."""

from __future__ import annotations

import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import messages
from .cache import ExtractCache
from .config import Config
from .downloader import DownloadError, download, extract_info, ffmpeg_available
from .formats import human_size, parse_options, selector_for

log = logging.getLogger(__name__)

YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?|shorts/|live/|embed/)|youtu\.be/)\S+",
    re.IGNORECASE,
)

# Stored on Application.bot_data under these keys.
CFG = "config"
CACHE = "cache"
FFMPEG = "ffmpeg"

# Uploading a multi-hundred-MB / multi-GB file to the (local) Bot API server takes
# minutes; the python-telegram-bot default read/write timeouts (~5s) fire long
# before the server finishes ingesting the upload, raising TimedOut even though the
# file is delivered. Give media sends generous timeouts to avoid a false failure.
UPLOAD_TIMEOUTS = {
    "read_timeout": 1800,
    "write_timeout": 1800,
    "connect_timeout": 60,
    "pool_timeout": 60,
}


def build_keyboard(token: str, options, upload_limit: int) -> InlineKeyboardMarkup:
    """Two-per-row quality buttons; oversized video tiers are marked ⛔."""
    rows, row = [], []
    for opt in options:
        size_txt = human_size(opt.est_size)
        oversized = opt.est_size is not None and opt.est_size > upload_limit
        prefix = "⛔ " if oversized else ""
        # Video tiers without an H.264 stream (usually >1080p) may not play in the
        # iOS stock player — flag them so the user isn't surprised by a frozen frame.
        suffix = " ⚠️" if opt.kind == "video" and not opt.h264 else ""
        text = f"{prefix}{opt.label}{suffix} · {size_txt}"
        row.append(InlineKeyboardButton(text, callback_data=f"dl:{token}:{opt.key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.START)


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.HELP)


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = YOUTUBE_RE.search(text)
    if not match:
        await update.message.reply_text(messages.NOT_A_LINK)
        return
    url = match.group(0)

    status = await update.message.reply_text(messages.EXTRACTING)
    try:
        info = await extract_info(url)
    except DownloadError as exc:
        await status.edit_text(messages.classify_error(exc))
        return
    except Exception as exc:  # noqa: BLE001 - surface as friendly message
        log.exception("extract failed")
        await status.edit_text(messages.classify_error(exc))
        return

    options = parse_options(info, ctx.application.bot_data[FFMPEG])
    if not options:
        await status.edit_text(messages.NO_FORMATS)
        return

    cache: ExtractCache = ctx.application.bot_data[CACHE]
    token = cache.put(url, info.get("title") or "video", info, options)
    cfg: Config = ctx.application.bot_data[CFG]

    title = info.get("title") or "видео"
    legend = ""
    if any(o.kind == "video" and not o.h264 for o in options):
        legend = "\n\n⚠️ — может не играть на iPhone (нет H.264 в этом качестве)"
    await status.edit_text(
        f"🎬 <b>{title}</b>\nВыбери качество:{legend}",
        parse_mode="HTML",
        reply_markup=build_keyboard(token, options, cfg.upload_limit),
    )


async def on_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, key = query.data.split(":", 2)
    except ValueError:
        return

    cache: ExtractCache = ctx.application.bot_data[CACHE]
    cfg: Config = ctx.application.bot_data[CFG]
    entry = cache.get(token)
    if entry is None:
        await query.edit_message_text(messages.EXPIRED)
        return

    opt = next((o for o in entry.options if o.key == key), None)
    if opt is None:
        await query.edit_message_text(messages.EXPIRED)
        return

    if opt.est_size is not None and opt.est_size > cfg.upload_limit:
        await query.answer(
            messages.TOO_BIG.format(
                size=human_size(opt.est_size), limit=human_size(cfg.upload_limit)
            ),
            show_alert=True,
        )
        return

    await query.edit_message_text(messages.DOWNLOADING.format(label=opt.label))

    path = None
    try:
        path = await download(
            entry.url,
            selector_for(key),
            cfg.download_dir,
            to_audio=(opt.kind == "audio"),
        )

        size = os.path.getsize(path)
        if size > cfg.upload_limit:
            await query.edit_message_text(
                messages.TOO_BIG.format(
                    size=human_size(size), limit=human_size(cfg.upload_limit)
                )
            )
            return

        await query.edit_message_text(messages.UPLOADING)
        with open(path, "rb") as fh:
            if opt.kind == "audio":
                await ctx.bot.send_audio(
                    query.message.chat_id, fh, title=entry.title, **UPLOAD_TIMEOUTS
                )
            else:
                await ctx.bot.send_video(
                    query.message.chat_id,
                    fh,
                    caption=entry.title,
                    supports_streaming=True,
                    **UPLOAD_TIMEOUTS,
                )
        await query.edit_message_text(f"✅ Готово: {entry.title}")
    except DownloadError as exc:
        await query.edit_message_text(messages.classify_error(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("download/send failed")
        await query.edit_message_text(messages.classify_error(exc))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def build_application(cfg: Config) -> Application:
    builder = Application.builder().token(cfg.bot_token)
    if cfg.api_base:
        builder = builder.base_url(cfg.api_base)
    app = builder.build()

    app.bot_data[CFG] = cfg
    app.bot_data[CACHE] = ExtractCache()
    app.bot_data[FFMPEG] = ffmpeg_available()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    return app
