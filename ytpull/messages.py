"""User-facing (Russian) strings and yt-dlp error classification."""

from __future__ import annotations

START = (
    "👋 Привет! Пришли мне ссылку на видео с YouTube — я покажу доступные "
    "качества, а ты выберешь, что скачать.\n\n"
    "Команды:\n"
    "/help — как пользоваться"
)

HELP = (
    "📖 Как пользоваться:\n"
    "1. Пришли ссылку на YouTube-видео.\n"
    "2. Я покажу кнопки с доступными качествами и примерным размером.\n"
    "3. Нажми на нужное — я скачаю и пришлю файл.\n\n"
    "⚠️ Ограничение Telegram: обычные боты могут отправлять файлы до 50 МБ. "
    "Слишком тяжёлые качества я помечаю и не даю выбрать — бери полегче или "
    "аудио. (Если бот подключён к локальному Bot API серверу — лимит 2 ГБ.)"
)

NOT_A_LINK = "Это не похоже на ссылку YouTube. Пришли ссылку вида https://youtu.be/…"
EXTRACTING = "🔎 Читаю информацию о видео…"
NO_FORMATS = "Не удалось найти пригодных для скачивания форматов у этого видео."
DOWNLOADING = "⬇️ Скачиваю: {label}…"
UPLOADING = "📤 Отправляю файл…"
EXPIRED = "Кнопка устарела — пришли ссылку заново."
TOO_BIG = (
    "⚠️ Это качество (~{size}) больше лимита Telegram ({limit}). "
    "Выбери качество полегче или аудио."
)


def classify_error(exc: Exception) -> str:
    """Map a yt-dlp / download exception to a friendly Russian message."""
    text = str(exc).lower()

    if "private" in text:
        return "🔒 Это приватное видео — скачать нельзя."
    if "sign in to confirm your age" in text or "age" in text and "restrict" in text:
        return "🔞 Видео с возрастным ограничением — YouTube не отдаёт его боту."
    if "geo" in text or "not available in your country" in text or "country" in text:
        return "🌍 Видео заблокировано в регионе сервера."
    if "removed" in text or "unavailable" in text or "no longer available" in text:
        return "🚫 Видео недоступно или удалено."
    if "not a valid url" in text or "unsupported url" in text:
        return "🔗 Не удалось распознать ссылку. Проверь, что это ссылка на YouTube."
    return "❌ Не получилось обработать видео. Попробуй другую ссылку или качество позже."
