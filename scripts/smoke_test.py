#!/usr/bin/env python3
"""Live end-to-end smoke test for ytpull-bot — run BEFORE declaring a deploy done.

Exercises the real flow so regressions are caught before users hit them:
  extract -> quality menu (sizes) -> download 144p -> ffprobe height
  -> Telegram send/edit/document sequence -> history record/render/delete.

The Telegram part needs a chat the bot can message:
    SMOKE_CHAT_ID=<id> .venv/bin/python scripts/smoke_test.py [youtube_url]
If SMOKE_CHAT_ID is unset it falls back to the newest chat in history.db. All test
messages and files are deleted afterwards. Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ytpull.config import load_config
from ytpull.downloader import download, extract_info, ffmpeg_available, probe_height
from ytpull.formats import human_size, parse_options, selector_for
from ytpull.history import HistoryDB
from ytpull.bot import KEYBOARD

DEFAULT_URL = "https://youtu.be/aqz-KE-bpKQ"  # Big Buck Bunny, stable, many tiers
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + label)
    if not cond:
        failures.append(label)


def _fallback_chat_id() -> int | None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        con = sqlite3.connect(os.path.join(root, "history.db"))
        con.row_factory = sqlite3.Row
        row = (con.execute("SELECT chat_id FROM downloads ORDER BY id DESC LIMIT 1").fetchone()
               or con.execute("SELECT chat_id FROM chats LIMIT 1").fetchone())
        return row["chat_id"] if row else None
    except Exception:
        return None


async def main(url: str) -> None:
    cfg = load_config()

    print("1) extract + quality menu")
    info = await extract_info(url, cookiefile=cfg.cookies_file)
    check(bool(info.get("title")), f"extract ok: {info.get('title')!r}")
    opts = parse_options(info, ffmpeg_available())
    check(bool(opts), "quality menu non-empty")
    for o in opts:
        print(f"     {o.label:8} {human_size(o.est_size)}")
    check(all(o.est_size for o in opts), "no '?' sizes (every tier has a size)")

    print("2) download smallest tier + verify real resolution")
    vid = [o for o in opts if o.kind == "video"]
    pick = min(vid, key=lambda o: int(o.key[1:])) if vid else opts[0]
    outdir = tempfile.mkdtemp(prefix="smoke-")
    path = await download(url, selector_for(pick.key), outdir,
                          to_audio=(pick.kind == "audio"), cookiefile=cfg.cookies_file)
    size = os.path.getsize(path)
    h = probe_height(path)
    print(f"     picked {pick.label} -> {h}px, {human_size(size)}")
    check(size > 0, "file downloaded (non-empty)")
    check(pick.kind == "audio" or h == int(pick.key[1:]),
          f"real height matches picked tier ({h} == {pick.key[1:]})")

    print("3) Telegram send/edit/document sequence")
    chat_id = int(os.environ.get("SMOKE_CHAT_ID") or _fallback_chat_id() or 0)
    if not chat_id:
        print("  ⚠️  no chat id — skipping Telegram checks (set SMOKE_CHAT_ID)")
    else:
        async with Bot(cfg.bot_token, base_url=cfg.api_base) as bot:
            status = await bot.send_message(chat_id, "🔎 smoke: reading…")
            menu = InlineKeyboardMarkup([[InlineKeyboardButton("144p", callback_data="x")]])
            try:  # the regression that hung the bot lived exactly here
                await bot.edit_message_text("🎬 smoke menu", chat_id, status.message_id,
                                            reply_markup=menu)
                check(True, "status message edits into inline menu")
            except Exception as exc:  # noqa: BLE001
                check(False, f"status->menu edit FAILED: {exc}")
            await bot.delete_message(chat_id, status.message_id)

            with open(path, "rb") as fh:
                doc = await bot.send_document(chat_id, fh, filename="smoke.mp4",
                                              caption="smoke 🎥 #N9999",
                                              reply_markup=KEYBOARD,
                                              read_timeout=120, write_timeout=120)
            check(bool(doc.message_id), "send_document with reply keyboard ok")
            await bot.delete_message(chat_id, doc.message_id)

    print("4) history record / render / delete")
    hpath = os.path.join(outdir, "smoke_history.db")
    db = HistoryDB(hpath)
    n1 = db.next_number(999)
    n2 = db.next_number(999)
    db.record(999, n1, "SmokeChan", "Vid one", "144p", "https://y/1", 11)
    db.record(999, n2, "SmokeChan", "Vid two", "144p", "https://y/2", 12)
    rendered = db.render(999)
    check("#N%04d" % n1 in rendered, "history renders #N-prefixed numbers")
    check(len(db.records(999)) == 2, "history has 2 records")
    db.delete(999, db.records(999)[0]["id"])
    check(len(db.records(999)) == 1, "delete removes a record")

    # cleanup
    for p in (path, hpath):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(outdir)
    except OSError:
        pass

    print()
    if failures:
        print(f"SMOKE FAIL — {len(failures)} check(s) failed:")
        for f in failures:
            print("   -", f)
        sys.exit(1)
    print("SMOKE PASS — full end-to-end flow verified.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
