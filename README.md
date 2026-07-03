# ytpull-bot

Telegram bot that downloads YouTube videos via [yt-dlp](https://github.com/yt-dlp/yt-dlp), with an
inline quality-selection menu.

## Flow

1. Send the bot a YouTube link.
2. It extracts available qualities with yt-dlp and shows them as inline buttons (with an estimated
   file size per option). Formats above the Telegram upload limit are marked ⛔ and can't be picked.
3. Pick a quality (or "Аудио (mp3)" for audio-only).
4. The bot downloads it in the background and sends the file back.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`ffmpeg` must be installed and on `PATH` — it's used to merge separate video/audio streams (most
non-legacy YouTube resolutions are served video-only) and to extract mp3 audio. Without it, only
formats that already carry audio (usually 360p and below) are offered.

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN=<token from @BotFather>
```

Run it:

```bash
python main.py
```

## Configuration (`.env`)

| Variable             | Required | Default            | Meaning |
|-----------------------|:--------:|---------------------|---------|
| `TELEGRAM_BOT_TOKEN`  | yes      | —                    | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_API_BASE`   | no       | *(empty)*            | Base URL of a self-hosted [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server. Leave empty to use `api.telegram.org`. |
| `DOWNLOAD_DIR`        | no       | system temp dir      | Where downloaded files are staged before upload (removed right after sending) |
| `LOG_LEVEL`           | no       | `INFO`               | Python logging level |

### File size limits

Telegram's Bot API caps file uploads at **50 MB** on the public `api.telegram.org` server. Qualities
that would exceed the limit are marked ⛔ in the menu and rejected if picked (a fresh on-disk size
check runs after download too, since size estimates from yt-dlp aren't always exact).

To send larger files (up to **2 GB**), run your own
[local Bot API server](https://github.com/tdlib/telegram-bot-api) and point `TELEGRAM_API_BASE` at
it, e.g.:

```
TELEGRAM_API_BASE=http://localhost:8081/bot
```

## Error handling

Private, age-restricted, geo-blocked, removed/unavailable videos, and unparsable links all get a
friendly Russian message back instead of a stack trace (see `ytpull/messages.py:classify_error`).

## Project layout

```
main.py              entrypoint: load config, build app, run polling
ytpull/config.py      env/.env loading, upload-limit logic
ytpull/bot.py         Telegram handlers (link -> quality menu -> download -> send)
ytpull/downloader.py  async yt-dlp wrapper (extract_info / download)
ytpull/formats.py     yt-dlp format dict -> user-facing quality tiers + selectors
ytpull/cache.py       short-lived token cache for inline-button callback_data
ytpull/messages.py    user-facing strings + error classification
```
