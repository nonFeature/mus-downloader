"""
bot/i18n.py
Bilingual localisation engine for mus-downloader (Russian / English).

Auto-detection: language_code starts with "ru" → Russian; anything else → English.
Persistent override: stored in UserSettings under the "language" key.

Every emoji in the catalogs is a placeholder (``{emoji_wave}``, ``{emoji_cross}``,
…) resolved through ``bot.emoji``: custom ``<tg-emoji>`` when enabled, standard
Unicode fallback otherwise.

Usage:
    from bot.i18n import t, detect_lang

    lang = detect_lang(event_from_user)
    await message.answer(t("cmd.start", lang))
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .emoji import emoji_kwargs

logger = logging.getLogger("mus_bot.i18n")

__all__ = ["t", "detect_lang", "SUPPORTED_LANGS", "FALLBACK_LANG"]

SUPPORTED_LANGS = ("ru", "en")
FALLBACK_LANG = "ru"

# ---------------------------------------------------------------------------
# String catalogs
# ---------------------------------------------------------------------------

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        # ── Commands ─────────────────────────────────────────────────────────
        "cmd.start": """{emoji_wave} <b>Music Downloader Bot на связи.</b>

Кинь <b>ссылку на трек</b> или напиши <b>название</b> (Артист - Название). Скачаю в MP3 320k или FLAC и пришлю файлом прямо в чат.

<b>Что принимаю?</b>
<i>Практически</i> все стриминговые платформы, и просто поиск, по типу <code>On the level Mac DeMarco</code>

<b>Команды:</b>
• /quality: качество по умолчанию""",
        "settings.quality_title": """{emoji_settings} <b>Настройка качества по умолчанию</b>

Сейчас: <b>{current_pref}</b>

Как качать новые треки?""",
        "settings.quality_saved": """{emoji_settings} <b>Настройка качества по умолчанию</b>

Сейчас: <b>{current_pref}</b>

{emoji_check} Сохранил.""",
        "settings.quality_saved_alert": "{emoji_check} Запомнил: {pref}",

        # ── Preferences ───────────────────────────────────────────────────────
        "pref.ask": "{emoji_ask} Спрашивать каждый раз",
        "pref.mp3": "{emoji_audio} MP3 320k (автоматически)",
        "pref.flac": "{emoji_vinyl} FLAC Lossless (автоматически)",

        # ── Keyboard labels (без эмодзи: его добавляет bot.emoji) ─────────────
        "btn.mp3": "MP3 320k",
        "btn.flac": "FLAC Lossless",
        "btn.ask": "Спросить каждый раз",
        "quality.mp3": "MP3 320k",
        "quality.flac": "FLAC Lossless",
        "quality.ask": "Спрашивать каждый раз",

        # ── Metadata card ─────────────────────────────────────────────────────
        "card.unknown_artist": "Неизвестный исполнитель",
        "card.unknown_track": "Неизвестный трек",
        "card.album": "Альбом:",
        "card.year": "Год:",
        "card.duration": "Длительность:",
        "duration.unknown": "Неизвестно",

        # ── Status updates ────────────────────────────────────────────────────
        "status.searching_meta": "{emoji_search} <i>Ищу трек...</i>",
        "status.track_not_found": """{emoji_cross} <b>Трек не найден</b>

Ничего не нашлось. Уточни название или кинь прямую ссылку со Spotify, Apple Music, Deezer или YouTube.""",
        "status.auto_download": """{emoji_bolt} <b>Авто-скачивание [{pref}]</b>

{card_text}

<i>Начинаю скачивать...</i>""",
        "status.starting_dl": "Запуск скачивания ({quality})...",
        "status.queued": """{emoji_queue} <b>{artist} — {title}</b> [{quality}]

<i>Стою в очереди, жду свободный слот...</i>""",
        "status.searching_sources": """{emoji_queue} <b>{artist} — {title}</b> [{quality}]

<i>Ищу, откуда скачать...</i>""",
        "status.uploading": "{emoji_upload} Отправляю в Telegram...",
        "status.unknown_cmd": "{emoji_ask} Неизвестная команда. Что умею — в /start.",
        "status.unsupported_content": "{emoji_bulb} Пришли ссылку на трек или его название текстом.",

        # ── Errors ────────────────────────────────────────────────────────────
        "error.invalid_request": "Неверный формат запроса.",
        "error.invalid_quality": "Неверное значение качества.",
        "error.invalid_download_quality": "Неверное качество скачивания.",
        "error.expired_button": "{emoji_warning} Срок действия этой кнопки истёк. Пожалуйста, отправьте ссылку на трек заново.",
        "error.download_failed": """{emoji_cross} <b>{artist} — {title}</b>

Не удалось скачать трек: ни один источник не отдал файл. Попробуй другую ссылку или уточни запрос.""",
        "error.file_too_large_cloud": """{emoji_warning} <b>Файл слишком большой ({size_mb:.1f} МБ)</b>

Облачный Telegram пропускает файлы до 50 МБ.

{emoji_bulb} <b>Что делать:</b>
• взять <b>[{emoji_audio} MP3 320k]</b>: весит в 3-4 раза меньше
• или поднять локальный Bot API Server (<code>TELEGRAM_API_ID</code> и <code>TELEGRAM_API_HASH</code> в <code>.env</code>), через него уходят файлы до 2 ГБ""",
        "error.file_too_large_local": "{emoji_cross} <b>Файл превышает лимит сервера в 2 ГБ ({size_mb:.1f} МБ)</b>",
        "error.generic": "{emoji_cross} <b>Ошибка:</b> {error}",
    },
    "en": {
        # ── Commands ─────────────────────────────────────────────────────────
        "cmd.start": """{emoji_wave} <b>Music Downloader Bot here.</b>

Send a <b>track link</b> or type a <b>track name</b> (Artist - Title). I'll grab it in MP3 320k or FLAC and drop the file in this chat.

<b>What do I accept?</b>
<i>Pretty much</i> every streaming platform, plus plain search like <code>On the level Mac DeMarco</code>

<b>Commands:</b>
• /quality: default format""",
        "settings.quality_title": """{emoji_settings} <b>Default quality</b>

Now: <b>{current_pref}</b>

How should I handle new tracks?""",
        "settings.quality_saved": """{emoji_settings} <b>Default quality</b>

Now: <b>{current_pref}</b>

{emoji_check} Saved.""",
        "settings.quality_saved_alert": "{emoji_check} Saved: {pref}",

        # ── Preferences ───────────────────────────────────────────────────────
        "pref.ask": "{emoji_ask} Always ask",
        "pref.mp3": "{emoji_audio} MP3 320k (automatic)",
        "pref.flac": "{emoji_vinyl} FLAC Lossless (automatic)",

        # ── Keyboard labels (без эмодзи: его добавляет bot.emoji) ─────────────
        "btn.mp3": "MP3 320k",
        "btn.flac": "FLAC Lossless",
        "btn.ask": "Ask every time",
        "quality.mp3": "MP3 320k",
        "quality.flac": "FLAC Lossless",
        "quality.ask": "Ask every time",

        # ── Metadata card ─────────────────────────────────────────────────────
        "card.unknown_artist": "Unknown Artist",
        "card.unknown_track": "Unknown Track",
        "card.album": "Album:",
        "card.year": "Year:",
        "card.duration": "Duration:",
        "duration.unknown": "Unknown",

        # ── Status updates ────────────────────────────────────────────────────
        "status.searching_meta": "{emoji_search} <i>Looking up the track...</i>",
        "status.track_not_found": """{emoji_cross} <b>Track not found</b>

No matches. Try another spelling or send a direct link from Spotify, Apple Music, Deezer or YouTube.""",
        "status.auto_download": """{emoji_bolt} <b>Auto-download [{pref}]</b>

{card_text}

<i>Starting now...</i>""",
        "status.starting_dl": "Starting download ({quality})...",
        "status.queued": """{emoji_queue} <b>{artist} — {title}</b> [{quality}]

<i>Waiting for a free slot...</i>""",
        "status.searching_sources": """{emoji_queue} <b>{artist} — {title}</b> [{quality}]

<i>Checking sources...</i>""",
        "status.uploading": "{emoji_upload} Sending to Telegram...",
        "status.unknown_cmd": "{emoji_ask} Unknown command. See /start for what I do.",
        "status.unsupported_content": "{emoji_bulb} Send a track link or its title as text.",

        # ── Errors ────────────────────────────────────────────────────────────
        "error.invalid_request": "Invalid request.",
        "error.invalid_quality": "Invalid quality value.",
        "error.invalid_download_quality": "Invalid download quality.",
        "error.expired_button": "{emoji_warning} This button expired. Send the track link again.",
        "error.download_failed": """{emoji_cross} <b>{artist} — {title}</b>

No source delivered the file. Try another link or refine the query.""",
        "error.file_too_large_cloud": """{emoji_warning} <b>File too large ({size_mb:.1f} MB)</b>

Cloud Telegram caps uploads at 50 MB.

{emoji_bulb} <b>What to do:</b>
• grab <b>[{emoji_audio} MP3 320k]</b>: 3-4x smaller
• or run a local Bot API Server (<code>TELEGRAM_API_ID</code> and <code>TELEGRAM_API_HASH</code> in <code>.env</code>) for files up to 2 GB""",
        "error.file_too_large_local": "{emoji_cross} <b>File exceeds the server limit of 2 GB ({size_mb:.1f} MB)</b>",
        "error.generic": "{emoji_cross} <b>Error:</b> {error}",
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_lang(user: Any) -> str:
    """
    Determine the UI language from a Telegram User object.

    Rules:
    - If user has a ``language_code`` that starts with "ru" → "ru"
    - Otherwise → "en"
    - If user is None or language_code is absent → FALLBACK_LANG ("ru")
    """
    lc: Optional[str] = None
    try:
        lc = getattr(user, "language_code", None)
    except Exception:
        pass
    if not lc:
        return FALLBACK_LANG
    return "ru" if lc.lower().startswith("ru") else "en"


def t(key: str, lang: str, **kwargs: Any) -> str:
    """
    Look up a localisation string by key for the given language.

    Falls back to FALLBACK_LANG if the key is missing in ``lang``.
    Emoji placeholders (``{emoji_audio}``, ``{emoji_check}``, …) are resolved
    through ``bot.emoji``: custom ``<tg-emoji>`` tags when enabled, plain
    Unicode fallbacks otherwise. Extra kwargs are passed to ``str.format``.

    Example:
        t("card.album", "en")
        → "Album:"
    """
    lang = lang if lang in SUPPORTED_LANGS else FALLBACK_LANG
    catalog = _STRINGS.get(lang, _STRINGS[FALLBACK_LANG])
    raw = catalog.get(key)
    if raw is None:
        # Try fallback language before giving up
        raw = _STRINGS[FALLBACK_LANG].get(key)
    if raw is None:
        logger.warning("Missing i18n key: %r (lang=%r)", key, lang)
        return key
    if "{" in raw or kwargs:
        try:
            return raw.format(**{**emoji_kwargs(), **kwargs})
        except (KeyError, ValueError, IndexError) as exc:
            logger.debug("i18n format error for key %r: %s", key, exc)
    return raw
