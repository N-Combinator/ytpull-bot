"""Telegram handlers: link -> quality menu -> download -> send file."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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
from .downloader import (
    DownloadError,
    download,
    extract_info,
    ffmpeg_available,
    prepare_thumbnail,
    probe_height,
)
from .formats import human_size, parse_options, selector_for
from .history import HistoryDB

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
HISTORY = "history"

# Persistent reply-keyboard button that shows the download history on demand.
HISTORY_BTN = "📥 История скачивания"
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(HISTORY_BTN)]], resize_keyboard=True, is_persistent=True
)


def _render_bar(pct: int, width: int = 12) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


async def _safe_edit(message, text: str) -> None:
    """Edit a message, swallowing 'not modified' / flood / transient errors."""
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001 - progress edits are best-effort
        pass


async def _safe_edit_markup(message, text: str, markup) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        pass


def _slug(name: str, maxlen: int = 64) -> str:
    """Telegram-hashtag-safe slug: non-word runs -> underscore (Unicode-aware)."""
    return re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_")[:maxlen].strip("_")


def _channel_tag(info: dict) -> str:
    return _slug((info.get("uploader") or info.get("channel") or "").strip())


def _title_tag(title: str) -> str:
    return _slug(title) or "video"


def _caption(entry, quality_label: str, bot_username: str, num: int, url: str) -> str:
    """Document caption: title, channel + #number, @bot: quality, source link."""
    tag = _channel_tag(entry.info)
    ids = "  ·  ".join(([f"#{tag}"] if tag else []) + [f"#N{num:04d}"])
    lines = [f"🎥 {entry.title}", f"👤 {ids}", f"@{bot_username}: 🎥 {quality_label}"]
    if url:
        lines.append(f"🔗 {url}")
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
        await update.message.reply_text(messages.ACCESS_GRANTED, reply_markup=KEYBOARD)
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
        await update.message.reply_text(messages.START, reply_markup=KEYBOARD)
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
    cfg: Config = ctx.application.bot_data[CFG]

    status = await update.message.reply_text(messages.EXTRACTING, reply_markup=KEYBOARD)
    try:
        info = await extract_info(url, cookiefile=cfg.cookies_file)
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
            cookiefile=cfg.cookies_file,
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
        history: HistoryDB = ctx.application.bot_data[HISTORY]

        # Real quality from the file (avc1 fallback may differ from the picked tier).
        if opt.kind == "audio":
            quality = opt.label
        else:
            real_h = probe_height(path)
            quality = f"{real_h}p" if real_h else opt.label

        num = history.next_number(chat_id)
        caption = _caption(entry, quality, ctx.bot.username, num, entry.url)
        thumb = await prepare_thumbnail(entry.info, cfg.download_dir)
        thumb_fh = open(thumb, "rb") if thumb else None
        try:
            with open(path, "rb") as fh:
                if opt.kind == "audio":
                    sent = await ctx.bot.send_audio(
                        chat_id, fh, title=entry.title, caption=caption,
                        thumbnail=thumb_fh, **UPLOAD_TIMEOUTS,
                    )
                else:
                    # Sent as a document (not send_video): Telegram hands the raw file
                    # to the phone's native player instead of inline-decoding it —
                    # which is what makes AV1/high-res clips play on iOS. The YouTube
                    # thumbnail rides along as the document's small preview icon.
                    ext = os.path.splitext(path)[1] or ".mp4"
                    safe = re.sub(r"[^\w\-]+", "_", entry.title).strip("_")[:60] or "video"
                    sent = await ctx.bot.send_document(
                        chat_id, fh, filename=f"{safe}{ext}", caption=caption,
                        thumbnail=thumb_fh, **UPLOAD_TIMEOUTS,
                    )
        finally:
            if thumb_fh:
                thumb_fh.close()
            if thumb and os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    pass
        entry.num, entry.quality = num, quality
        entry.doc_message_id = sent.message_id
        # No "done" text. Drop the user's original link, and turn our own message
        # into a compact "add to history?" offer (buttons vanish with it on choice).
        if entry.user_msg_id:
            try:
                await ctx.bot.delete_message(chat_id, entry.user_msg_id)
            except Exception:  # noqa: BLE001
                pass
        offer = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да", callback_data=f"save:{token}:{key}"),
            InlineKeyboardButton("Нет", callback_data="save:no"),
        ]])
        await _safe_edit_markup(query.message, "🗂 Сохранить в историю?", offer)
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


async def on_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer the post-download 'save to history?' offer (Да/Нет)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "save:no":
        await query.message.delete()
        return

    cache: ExtractCache = ctx.application.bot_data[CACHE]
    hist: HistoryDB = ctx.application.bot_data[HISTORY]
    try:
        _, token, _key = query.data.split(":", 2)
        entry = cache.get(token)
    except ValueError:
        entry = None
    if entry is not None and entry.num is not None:
        channel = _channel_tag(entry.info) or "unknown"
        hist.record(
            chat_id, entry.num, channel, entry.title,
            entry.quality or "", entry.url, entry.doc_message_id,
        )
    await query.message.delete()


async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'История скачивания' reply-keyboard button."""
    store: AuthStore = ctx.application.bot_data[AUTH]
    if not store.is_authorized(update.effective_user.id):
        await update.message.reply_text(messages.NEED_PASSWORD)
        return
    hist: HistoryDB = ctx.application.bot_data[HISTORY]
    text = hist.render(update.message.chat_id)
    if not hist.records(update.message.chat_id):
        await update.message.reply_text("История пуста.", reply_markup=KEYBOARD)
        return
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Редактировать", callback_data="hedit")]]
    )
    await update.message.reply_text(text, reply_markup=markup)


def _edit_markup(records: list[dict]) -> InlineKeyboardMarkup:
    """One delete button per history row, plus a Готово button."""
    rows = [
        [InlineKeyboardButton(f"✖️ #N{r['num']:04d} {r['title']}"[:60],
                              callback_data=f"hdel:{r['id']}")]
        for r in records
    ]
    rows.append([InlineKeyboardButton("Готово", callback_data="hdone")])
    return InlineKeyboardMarkup(rows)


async def on_hist_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit view for the history: delete individual entries."""
    query = update.callback_query
    await query.answer()
    hist: HistoryDB = ctx.application.bot_data[HISTORY]
    chat_id = query.message.chat_id

    if query.data == "hdone":
        text = hist.render(chat_id)
        if hist.records(chat_id):
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✏️ Редактировать", callback_data="hedit")]]
            )
            await _safe_edit_markup(query.message, text, markup)
        else:
            await _safe_edit(query.message, "История пуста.")
        return

    if query.data.startswith("hdel:"):
        try:
            hist.delete(chat_id, int(query.data.split(":", 1)[1]))
        except (ValueError, IndexError):
            pass

    records = hist.records(chat_id)
    if not records:
        await _safe_edit(query.message, "История пуста.")
        return
    await _safe_edit_markup(query.message, "Удалить записи:", _edit_markup(records))


def build_application(cfg: Config) -> Application:
    builder = Application.builder().token(cfg.bot_token)
    if cfg.api_base:
        builder = builder.base_url(cfg.api_base)
    app = builder.build()

    app.bot_data[CFG] = cfg
    app.bot_data[CACHE] = ExtractCache()
    app.bot_data[FFMPEG] = ffmpeg_available()
    root = os.path.dirname(os.path.dirname(__file__))
    app.bot_data[AUTH] = AuthStore(cfg.auth_seed, os.path.join(root, "authorized.json"))
    app.bot_data[HISTORY] = HistoryDB(os.path.join(root, "history.db"))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(on_save, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_hist_edit, pattern=r"^h(edit|del|done)"))
    app.add_handler(MessageHandler(filters.Text([HISTORY_BTN]), show_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    return app
