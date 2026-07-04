"""Telegram handlers: link -> quality menu -> download -> send file."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

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
from .auth import AuthStore
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
AUTH = "auth"


def _render_bar(pct: int, width: int = 12) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


async def _safe_edit(message, text: str) -> None:
    """Edit a message, swallowing 'not modified' / flood / transient errors."""
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001 - progress edits are best-effort
        pass


def _channel_tag(info: dict) -> str:
    """A Telegram-hashtag-safe slug of the channel name (spaces -> underscore)."""
    name = (info.get("uploader") or info.get("channel") or "").strip()
    return re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_")


def _caption(entry, quality_label: str, bot_username: str) -> str:
    """Document caption: title, channel hashtag, and @bot: quality."""
    lines = [f"🎥 {entry.title}"]
    tag = _channel_tag(entry.info)
    if tag:
        lines.append(f"👤 #{tag}")
    lines.append(f"@{bot_username}: 🎥 {quality_label}")
    return "\n".join(lines)

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


async def _ensure_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Password gate. Returns True only if the user may proceed with this message.

    An unauthorized user's message is treated as a password attempt: on success we
    remember them (and delete the message so the password doesn't linger); either
    way the current message is consumed, so callers stop when this returns False.
    """
    store: AuthStore = ctx.application.bot_data[AUTH]
    uid = update.effective_user.id
    if store.is_authorized(uid):
        return True
    text = (update.message.text or "") if update.message else ""
    if store.check_password(text):
        store.authorize(uid)
        await update.message.reply_text(messages.ACCESS_GRANTED)
        try:
            await ctx.bot.delete_message(update.message.chat_id, update.message.message_id)
        except Exception:  # noqa: BLE001
            pass
    else:
        await update.message.reply_text(messages.NEED_PASSWORD)
    return False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: AuthStore = ctx.application.bot_data[AUTH]
    if store.is_authorized(update.effective_user.id):
        await update.message.reply_text(messages.START)
    else:
        await update.message.reply_text(messages.NEED_PASSWORD)


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.HELP)


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_access(update, ctx):
        return
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
    token = cache.put(
        url, info.get("title") or "video", info, options, user_msg_id=update.message.message_id
    )
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

    store: AuthStore = ctx.application.bot_data[AUTH]
    if not store.is_authorized(update.effective_user.id):
        await query.answer(messages.NEED_PASSWORD, show_alert=True)
        return

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

    # Live progress: yt-dlp calls `hook` from its worker thread, so we bounce the
    # message edit back onto this event loop, throttled to avoid Telegram flood
    # limits (every ~3s and only when the percentage advances).
    loop = asyncio.get_running_loop()
    prog = {"pct": -1, "t": 0.0}

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not total:
            return
        pct = int((d.get("downloaded_bytes") or 0) * 100 / total)
        now = time.monotonic()
        if pct <= prog["pct"] or now - prog["t"] < 3:
            return
        prog["pct"], prog["t"] = pct, now
        text = f"⬇️ {opt.label}\n{_render_bar(pct)} {pct}%"
        asyncio.run_coroutine_threadsafe(_safe_edit(query.message, text), loop)

    path = None
    try:
        path = await download(
            entry.url,
            selector_for(key),
            cfg.download_dir,
            to_audio=(opt.kind == "audio"),
            progress=hook,
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
        chat_id = query.message.chat_id
        caption = _caption(entry, opt.label, ctx.bot.username)
        with open(path, "rb") as fh:
            if opt.kind == "audio":
                await ctx.bot.send_audio(
                    chat_id, fh, title=entry.title, caption=caption, **UPLOAD_TIMEOUTS
                )
            else:
                # Sent as a document (not send_video): Telegram hands the raw file to
                # the phone's native player instead of inline-decoding it — which is
                # what makes AV1/high-res clips play on iOS.
                ext = os.path.splitext(path)[1] or ".mp4"
                safe = re.sub(r"[^\w\-]+", "_", entry.title).strip("_")[:60] or "video"
                await ctx.bot.send_document(
                    chat_id, fh, filename=f"{safe}{ext}", caption=caption, **UPLOAD_TIMEOUTS
                )
        # No textual confirmation: drop our own progress/menu message and the user's
        # original link, so only the delivered file is left in the chat.
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        if entry.user_msg_id:
            try:
                await ctx.bot.delete_message(chat_id, entry.user_msg_id)
            except Exception:  # noqa: BLE001
                pass
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
    auth_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "authorized.json")
    app.bot_data[AUTH] = AuthStore(cfg.auth_seed, auth_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    return app
