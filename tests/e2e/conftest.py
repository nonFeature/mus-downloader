"""E2E Test Configuration and Fixtures for mus-downloader.

Provides the test harness, mock factories, and graceful adapter layer for
testing against both modular services and progressive milestone implementations.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

# ---------------------------------------------------------------------------
# Dynamic Service & Module Resolvers with Graceful Fallbacks
# ---------------------------------------------------------------------------

try:
    from bot.storage import QueryStore as _SvcQueryStore, UserSettings as _SvcUserSettings
except ImportError:
    _SvcQueryStore, _SvcUserSettings = None, None

try:
    from bot.process_lock import SingleInstanceLock as _SvcSingleInstanceLock
except ImportError:
    _SvcSingleInstanceLock = None

try:
    from bot.local_bot_api import LocalBotAPIManager as _SvcLocalBotAPIManager
except ImportError:
    _SvcLocalBotAPIManager = None

try:
    from bot.notifier import StatusNotifier as _SvcStatusNotifier
except ImportError:
    _SvcStatusNotifier = None

try:
    from bot import emoji as _svc_emoji
except ImportError:
    _svc_emoji = None

try:
    from bot import i18n as _svc_i18n
except ImportError:
    _svc_i18n = None


# ---------------------------------------------------------------------------
# Domain Constants: Custom Telegram Emojis
# ---------------------------------------------------------------------------
ID_AUDIO = "5318991475760897711"
ID_VINYL = "5318991475760897712"
ID_TRACK = "5318991475760897713"
ID_DOWNLOAD = "5318991475760897714"
ID_CHECK = "5318991475760897715"
ID_CROSS = "5318991475760897716"
ID_WARNING = "5318991475760897717"
ID_SETTINGS = "5318991475760897718"
ID_QUALITY = "5318991475760897719"
ID_CLOCK = "5318991475760897720"
ID_WAVE = "5318991475760897721"


# ---------------------------------------------------------------------------
# E2E Emoji Adapter
# ---------------------------------------------------------------------------
class E2EEmoji:
    _custom_emoji_enabled: bool = False

    @classmethod
    def set_custom_emoji_enabled(cls, enabled: bool) -> None:
        if _svc_emoji and hasattr(_svc_emoji, "set_custom_emoji_enabled"):
            _svc_emoji.set_custom_emoji_enabled(enabled)
        cls._custom_emoji_enabled = enabled

    @classmethod
    def is_custom_emoji_enabled(cls) -> bool:
        if _svc_emoji and hasattr(_svc_emoji, "is_custom_emoji_enabled"):
            return _svc_emoji.is_custom_emoji_enabled()
        return cls._custom_emoji_enabled

    @classmethod
    def e(cls, emoji_id: Optional[str], fallback: str) -> str:
        if _svc_emoji and hasattr(_svc_emoji, "e"):
            return _svc_emoji.e(emoji_id, fallback)
        if cls.is_custom_emoji_enabled() and emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        return fallback

    @classmethod
    def make_inline_button(
        cls,
        text: str,
        callback_data: Optional[str] = None,
        url: Optional[str] = None,
        emoji_id: Optional[str] = None,
        fallback_emoji: str = "",
        style: Optional[str] = None,
        **kwargs: Any,
    ) -> InlineKeyboardButton:
        if _svc_emoji and hasattr(_svc_emoji, "make_inline_button"):
            return _svc_emoji.make_inline_button(
                text=text,
                callback_data=callback_data,
                url=url,
                emoji_id=emoji_id,
                fallback_emoji=fallback_emoji,
                style=style,
                **kwargs,
            )
        kwargs_btn: Dict[str, Any] = {}
        if callback_data is not None:
            kwargs_btn["callback_data"] = callback_data
        if url is not None:
            kwargs_btn["url"] = url

        if cls.is_custom_emoji_enabled() and emoji_id:
            return InlineKeyboardButton(
                text=text,
                icon_custom_emoji_id=emoji_id,
                **kwargs_btn,
                **kwargs,
            )
        label = f"{fallback_emoji} {text}".strip() if fallback_emoji else text
        return InlineKeyboardButton(text=label, **kwargs_btn, **kwargs)


# ---------------------------------------------------------------------------
# E2E i18n Localization Engine & Middleware
# ---------------------------------------------------------------------------
current_lang: ContextVar[str] = ContextVar("current_lang", default="ru")

I18N_DICTIONARY: Dict[str, Dict[str, str]] = {
    "ru": {
        "duration.unknown": "Неизвестно",
        "card.unknown_artist": "Неизвестный исполнитель",
        "card.unknown_track": "Неизвестный трек",
        "card.template": "{emoji_audio} <b>{artist} — {title}</b>{explicit}\n\n{emoji_vinyl} <b>Альбом:</b> {album}\n{emoji_calendar} <b>Год:</b> {year}\n{emoji_clock} <b>Длительность:</b> {duration}",
        "btn.mp3_320k": "{emoji_audio} MP3 320k",
        "btn.flac_lossless": "{emoji_vinyl} FLAC Lossless",
        "btn.ask_always": "❓ Спрашивать каждый раз{mark}",
        "btn.mp3_pref": "{emoji_audio} MP3 320k{mark}",
        "btn.flac_pref": "{emoji_vinyl} FLAC Lossless{mark}",
        "btn.lang_ru": "🇷🇺 Русский{mark}",
        "btn.lang_en": "🇬🇧 English{mark}",
        "btn.lang_auto": "🔄 Авто ({detected}){mark}",
        "btn.back": "{emoji_back} Назад",
        "btn.settings_quality": "{emoji_quality} Качество звука",
        "btn.settings_language": "🌐 Язык интерфейса",
        "cmd.start": "👋 <b>Добро пожаловать в Music Downloader Bot!</b>\n\nОтправьте мне <b>ссылку на трек</b> или <b>название трека</b> (Артист — Название), и я скачаю его в лучшем качестве и пришлю аудиофайлом прямо в чат!\n\n<b>Поддерживаемые ссылки:</b>\n• Spotify, Apple Music, Deezer\n• YouTube Music, SoundCloud, Яндекс.Музыка\n• Текстовые поисковые запросы\n\n<b>Команды:</b>\n• /quality — настроить качество по умолчанию (MP3 320k / FLAC)\n• /settings — настройки бота (качество, язык)\n• /help — подробная справка",
        "cmd.help": "📖 <b>Справка по использованию:</b>\n\n1. <b>Как скачать трек?</b>\nПросто отправьте боту ссылку на трек из любого поддерживаемого стриминга или напишите название (например: <code>Queen Bohemian Rhapsody</code>).\n\n2. <b>Выбор качества:</b>\n• <b>MP3 320k</b>: честный 320 kbps CBR без пережатия (отлично подходит для телефонов, размер ~8-15 МБ).\n• <b>FLAC Lossless</b>: несжатый студийный Lossless из Soulseek P2P или Deezer HiFi.\n\n3. <b>Настройка качества по умолчанию:</b>\nКоманда /quality позволяет выбрать, спрашивать ли качество каждый раз или скачивать автоматически.\n\n4. <b>Ограничения по размеру файлов:</b>\nСтандартный лимит отправки через Telegram — 50 МБ. Если FLAC трек превышает этот лимит, бот предложит скачать его в MP3 или подключить локальный Bot API Server.",
        "settings.quality_title": "{emoji_settings} <b>Настройка качества по умолчанию</b>\n\nТекущий режим: <b>{current_pref}</b>\n\nВыберите желаемый режим для новых запросов:",
        "settings.quality_saved": "{emoji_settings} <b>Настройка качества по умолчанию</b>\n\nТекущий режим: <b>{current_pref}</b>\n\n{emoji_check} Настройки успешно сохранены!",
        "settings.lang_title": "🌐 <b>Настройка языка интерфейса / Language settings</b>\n\nТекущий язык / Current language: <b>{current_lang_name}</b>\n\nВыберите предпочтительный язык интерфейса:",
        "settings.lang_saved_alert": "Язык интерфейса изменён на Русский 🇷🇺",
        "pref.ask": "❓ Спрашивать каждый раз",
        "pref.mp3": "{emoji_audio} MP3 320k (автоматически)",
        "pref.flac": "{emoji_vinyl} FLAC Lossless (автоматически)",
        "status.searching_meta": "🔍 <i>Поиск метаданных трека...</i>",
        "status.track_not_found": "{emoji_cross} <b>Трек не найден</b>\n\nНе удалось извлечь информацию о треке по вашему запросу. Попробуйте уточнить название (например: <code>Queen — Bohemian Rhapsody</code>) или отправить прямую ссылку со Spotify / Apple Music / Deezer / YouTube.",
        "status.auto_download": "⚡ <b>Авто-скачивание [{pref}]</b>\n\n{card_text}\n\n<i>Запуск процесса скачивания...</i>",
        "status.starting_dl": "Запуск скачивания ({quality})...",
        "status.queued": "{emoji_clock} <b>{artist} — {title}</b> [{quality}]\n\n<i>Очередь скачивания: ожидание свободного слота...</i>",
        "status.searching_sources": "{emoji_clock} <b>{artist} — {title}</b> [{quality}]\n\n<i>Поиск источников...</i>",
        "status.uploading": "🚀 Отправка аудио в Telegram...",
        "status.unknown_cmd": "❓ Неизвестная команда. Введите /help для просмотра списка команд.",
        "status.unsupported_content": "💡 Пожалуйста, отправьте ссылку на трек или его название текстом.",
        "error.invalid_request": "Неверный формат запроса.",
        "error.invalid_quality": "Неверное качество скачивания.",
        "error.expired_button": "{emoji_warning} Срок действия этой кнопки истёк. Пожалуйста, отправьте ссылку на трек заново.",
        "error.download_failed": "{emoji_cross} <b>{artist} — {title}</b>\n\nНе удалось скачать трек ни с одного источника. Попробуйте другой источник или поиск.",
        "error.file_too_large_cloud": "{emoji_warning} <b>Файл слишком большой ({size_mb:.1f} МБ)</b>\n\nСтандартный Telegram Cloud Bot API ограничивает размер отправляемых файлов до <b>50 МБ</b>.\n\n💡 <b>Рекомендации:</b>\n• Скачайте трек в формате <b>[🎵 MP3 320k]</b> (размер будет в 3-4 раза меньше)\n• Или настройте локальный Telegram Bot API Server (параметры <code>TELEGRAM_API_ID</code> и <code>TELEGRAM_API_HASH</code> в <code>.env</code>) для отправки файлов до 2 ГБ.",
        "error.file_too_large_local": "{emoji_cross} <b>Файл превышает лимит сервера в 2 ГБ ({size_mb:.1f} МБ)</b>",
        "error.generic": "{emoji_cross} <b>Ошибка:</b> {error}",
    },
    "en": {
        "duration.unknown": "Unknown",
        "card.unknown_artist": "Unknown Artist",
        "card.unknown_track": "Unknown Track",
        "card.template": "{emoji_audio} <b>{artist} — {title}</b>{explicit}\n\n{emoji_vinyl} <b>Album:</b> {album}\n{emoji_calendar} <b>Year:</b> {year}\n{emoji_clock} <b>Duration:</b> {duration}",
        "btn.mp3_320k": "{emoji_audio} MP3 320k",
        "btn.flac_lossless": "{emoji_vinyl} FLAC Lossless",
        "btn.ask_always": "❓ Always ask{mark}",
        "btn.mp3_pref": "{emoji_audio} MP3 320k{mark}",
        "btn.flac_pref": "{emoji_vinyl} FLAC Lossless{mark}",
        "btn.lang_ru": "🇷🇺 Russian{mark}",
        "btn.lang_en": "🇬🇧 English{mark}",
        "btn.lang_auto": "🔄 Auto ({detected}){mark}",
        "btn.back": "{emoji_back} Back",
        "btn.settings_quality": "{emoji_quality} Audio Quality",
        "btn.settings_language": "🌐 Interface Language",
        "cmd.start": "👋 <b>Welcome to Music Downloader Bot!</b>\n\nSend me a <b>track link</b> or <b>track name</b> (Artist — Title), and I will download it in the best quality and send it right into the chat!\n\n<b>Supported links:</b>\n• Spotify, Apple Music, Deezer\n• YouTube Music, SoundCloud, Yandex Music\n• Text search queries\n\n<b>Commands:</b>\n• /quality — set default quality (MP3 320k / FLAC)\n• /settings — bot settings (quality, language)\n• /help — detailed help",
        "cmd.help": "📖 <b>User Guide:</b>\n\n1. <b>How to download a track?</b>\nSimply send a track link from any supported streaming service or enter the title (e.g.: <code>Queen Bohemian Rhapsody</code>).\n\n2. <b>Quality selection:</b>\n• <b>MP3 320k</b>: genuine 320 kbps CBR without recompression (ideal for mobile, ~8-15 MB).\n• <b>FLAC Lossless</b>: uncompressed studio Lossless from Soulseek P2P or Deezer HiFi.\n\n3. <b>Default quality configuration:</b>\nUse /quality or /settings to choose whether to ask every time or download automatically.\n\n4. <b>File size limits:</b>\nStandard Telegram Cloud limit is 50 MB. If a FLAC file exceeds this limit, the bot will suggest MP3 or setting up a local Bot API Server.",
        "settings.quality_title": "{emoji_settings} <b>Default Quality Settings</b>\n\nCurrent mode: <b>{current_pref}</b>\n\nSelect the desired mode for new requests:",
        "settings.quality_saved": "{emoji_settings} <b>Default Quality Settings</b>\n\nCurrent mode: <b>{current_pref}</b>\n\n{emoji_check} Settings saved successfully!",
        "settings.lang_title": "🌐 <b>Interface Language Settings</b>\n\nCurrent language: <b>{current_lang_name}</b>\n\nSelect your preferred interface language:",
        "settings.lang_saved_alert": "Interface language changed to English 🇬🇧",
        "pref.ask": "❓ Always ask",
        "pref.mp3": "{emoji_audio} MP3 320k (automatic)",
        "pref.flac": "{emoji_vinyl} FLAC Lossless (automatic)",
        "status.searching_meta": "🔍 <i>Searching track metadata...</i>",
        "status.track_not_found": "{emoji_cross} <b>Track not found</b>\n\nCould not retrieve track information for your query. Try refining the title (e.g.: <code>Queen — Bohemian Rhapsody</code>) or send a direct link from Spotify / Apple Music / Deezer / YouTube.",
        "status.auto_download": "⚡ <b>Auto-download [{pref}]</b>\n\n{card_text}\n\n<i>Starting download process...</i>",
        "status.starting_dl": "Starting download ({quality})...",
        "status.queued": "{emoji_clock} <b>{artist} — {title}</b> [{quality}]\n\n<i>Download queue: waiting for available slot...</i>",
        "status.searching_sources": "{emoji_clock} <b>{artist} — {title}</b> [{quality}]\n\n<i>Searching sources...</i>",
        "status.uploading": "🚀 Uploading audio to Telegram...",
        "status.unknown_cmd": "❓ Unknown command. Type /help to see the list of commands.",
        "status.unsupported_content": "💡 Please send a track link or title as text.",
        "error.invalid_request": "Invalid request format.",
        "error.invalid_quality": "Invalid download quality.",
        "error.expired_button": "{emoji_warning} This button has expired. Please send the track link again.",
        "error.download_failed": "{emoji_cross} <b>{artist} — {title}</b>\n\nFailed to download track from all available sources. Try another source or query.",
        "error.file_too_large_cloud": "{emoji_warning} <b>File is too large ({size_mb:.1f} MB)</b>\n\nStandard Telegram Cloud Bot API limits uploaded files to <b>50 MB</b>.\n\n💡 <b>Recommendations:</b>\n• Download in <b>[🎵 MP3 320k]</b> format (~3-4x smaller)\n• Or configure a local Telegram Bot API Server (<code>TELEGRAM_API_ID</code> and <code>TELEGRAM_API_HASH</code> in <code>.env</code>) to send files up to 2 GB.",
        "error.file_too_large_local": "{emoji_cross} <b>File exceeds server limit of 2 GB ({size_mb:.1f} MB)</b>",
        "error.generic": "{emoji_cross} <b>Error:</b> {error}",
    },
}


class E2EI18nEngine:
    def __init__(self) -> None:
        self._current_lang = "ru"

    def set_locale(self, lang: str) -> None:
        if lang in ("ru", "en"):
            self._current_lang = lang
            current_lang.set(lang)

    def get_locale(self) -> str:
        try:
            return current_lang.get()
        except LookupError:
            return self._current_lang

    def get(self, key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
        if _svc_i18n and hasattr(_svc_i18n, "I18nEngine"):
            engine = _svc_i18n.I18nEngine()
            if hasattr(engine, "get"):
                return engine.get(key, lang=lang, **kwargs)

        target_lang = lang or self.get_locale()
        if target_lang not in I18N_DICTIONARY:
            target_lang = "ru"

        translations = I18N_DICTIONARY.get(target_lang, I18N_DICTIONARY["ru"])
        raw_text = translations.get(key) or I18N_DICTIONARY["ru"].get(key, key)

        emoji_kwargs = {
            "emoji_audio": E2EEmoji.e(ID_AUDIO, "🎵"),
            "emoji_vinyl": E2EEmoji.e(ID_VINYL, "💿"),
            "emoji_track": E2EEmoji.e(ID_TRACK, "🎶"),
            "emoji_download": E2EEmoji.e(ID_DOWNLOAD, "⬇️"),
            "emoji_check": E2EEmoji.e(ID_CHECK, "✅"),
            "emoji_cross": E2EEmoji.e(ID_CROSS, "❌"),
            "emoji_warning": E2EEmoji.e(ID_WARNING, "⚠️"),
            "emoji_settings": E2EEmoji.e(ID_SETTINGS, "⚙️"),
            "emoji_quality": E2EEmoji.e(ID_QUALITY, "🎚"),
            "emoji_clock": E2EEmoji.e(ID_CLOCK, "⏳"),
            "emoji_wave": E2EEmoji.e(ID_WAVE, "👋"),
            "emoji_calendar": "📅",
            "emoji_back": "⬅️",
        }
        format_args = {**emoji_kwargs, **kwargs}
        try:
            return raw_text.format(**format_args)
        except (KeyError, ValueError, IndexError):
            return raw_text


def autodetect_language(language_code: Optional[str]) -> str:
    """Requirement R4: starts with ru -> ru; else -> en; None/empty -> ru."""
    if not language_code:
        return "ru"
    clean = str(language_code).strip().lower()
    if not clean:
        return "ru"
    if clean.startswith("ru"):
        return "ru"
    return "en"


def resolve_effective_language(
    user_id: int | str,
    telegram_language_code: Optional[str],
    user_settings: Any,
) -> str:
    """Resolves language according to strict R4 hierarchy."""
    if user_settings:
        override = user_settings.get_language(user_id)
        if override in ("ru", "en"):
            return override
    return autodetect_language(telegram_language_code)


# ---------------------------------------------------------------------------
# E2E Storage Adapter (Dual Schema: quality & language)
# ---------------------------------------------------------------------------
class E2EUserSettings:
    """Storage adapter supporting both quality and language preference."""

    def __init__(self, file_path: Optional[Path | str] = None) -> None:
        self.file_path = Path(file_path) if file_path else Path("user_settings_test.json")
        self._data: Dict[str, Dict[str, Any]] = {}
        if _SvcUserSettings is not None:
            self._impl = _SvcUserSettings(file_path=self.file_path)
        else:
            self._impl = None

        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    for k, v in content.items():
                        if isinstance(v, str):
                            self._data[str(k)] = {"quality": v, "language": None}
                        elif isinstance(v, dict):
                            self._data[str(k)] = v
            except Exception:
                pass

    def _save_to_disk(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_quality(self, user_id: int | str) -> str:
        uid = str(user_id)
        if self._impl is not None and hasattr(self._impl, "get_quality"):
            return self._impl.get_quality(user_id)
        return self._data.get(uid, {}).get("quality", "ASK")

    def set_quality(self, user_id: int | str, quality: str) -> None:
        uid = str(user_id)
        if self._impl is not None and hasattr(self._impl, "set_quality"):
            self._impl.set_quality(user_id, quality)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["quality"] = quality
        self._save_to_disk()

    def get_language(self, user_id: int | str) -> Optional[str]:
        uid = str(user_id)
        if self._impl is not None and hasattr(self._impl, "get_language"):
            return self._impl.get_language(user_id)
        return self._data.get(uid, {}).get("language")

    def set_language(self, user_id: int | str, lang: Optional[str]) -> None:
        uid = str(user_id)
        if self._impl is not None and hasattr(self._impl, "set_language"):
            self._impl.set_language(user_id, lang)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["language"] = lang
        self._save_to_disk()


class E2EQueryStore:
    """In-memory query metadata cache with TTL eviction."""

    def __init__(self, ttl: int = 3600) -> None:
        self.ttl = ttl
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        if _SvcQueryStore is not None:
            self._impl = _SvcQueryStore(ttl=ttl)
        else:
            self._impl = None

    def save(self, query_id: str, data: Dict[str, Any]) -> None:
        if self._impl is not None and hasattr(self._impl, "save"):
            self._impl.save(query_id, data)
            return
        self._cache[query_id] = (time.time(), data)

    def get(self, query_id: str) -> Optional[Dict[str, Any]]:
        if self._impl is not None and hasattr(self._impl, "get"):
            return self._impl.get(query_id)
        if query_id in self._cache:
            ts, data = self._cache[query_id]
            if time.time() - ts < self.ttl:
                return data
            del self._cache[query_id]
        return None


# ---------------------------------------------------------------------------
# E2E Process Lock Adapter
# ---------------------------------------------------------------------------
class E2ESingleInstanceLock:
    """SingleInstanceLock adapter verifying Windows & Linux mutual exclusion."""

    def __init__(self, lock_file: Optional[Path | str] = None) -> None:
        self.lock_file = Path(lock_file) if lock_file else Path("bot.lock")
        if _SvcSingleInstanceLock is not None:
            self._impl = _SvcSingleInstanceLock(lock_file=self.lock_file)
        else:
            self._impl = None
        self._fd: Optional[Any] = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._impl is not None and hasattr(self._impl, "acquire"):
            return self._impl.acquire()
        if self._acquired:
            return False
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(self.lock_file, "a+b")
            self._fd.seek(0)
            if sys.platform == "win32":
                import msvcrt
                if not self.lock_file.exists() or self.lock_file.stat().st_size == 0:
                    self._fd.write(b"0")
                    self._fd.flush()
                    self._fd.seek(0)
                try:
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                except (OSError, IOError, PermissionError):
                    self._fd.close()
                    self._fd = None
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, IOError):
                    self._fd.close()
                    self._fd = None
                    return False
            self._acquired = True
            return True
        except Exception:
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
            return False

    def release(self) -> None:
        if self._impl is not None and hasattr(self._impl, "release"):
            self._impl.release()
            self._acquired = False
            return
        if self._acquired and self._fd:
            try:
                self._fd.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                self._fd.close()
            except Exception:
                pass
            finally:
                self._fd = None
                self._acquired = False

    def __enter__(self) -> E2ESingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# E2E Local Bot API Adapter
# ---------------------------------------------------------------------------
class E2ELocalBotAPIManager:
    """LocalBotAPIManager adapter for lifecycle and health checks."""

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8081",
        local_mode: bool = False,
        server_path: Optional[Path | str] = None,
    ) -> None:
        self.api_url = api_url
        self.local_mode = local_mode
        self.server_path = Path(server_path) if server_path else None
        if _SvcLocalBotAPIManager is not None:
            self._impl = _SvcLocalBotAPIManager(
                api_url=api_url, local_mode=local_mode, server_path=server_path
            )
        else:
            self._impl = None
        self._is_running = False

    async def start(self) -> Optional[str]:
        if self._impl is not None and hasattr(self._impl, "start"):
            return await self._impl.start()
        self._is_running = True
        return self.api_url

    async def stop(self) -> None:
        if self._impl is not None and hasattr(self._impl, "stop"):
            await self._impl.stop()
        self._is_running = False

    def resolve_local_filepath(self, telegram_path: str) -> Path:
        if self._impl is not None and hasattr(self._impl, "resolve_local_filepath"):
            return self._impl.resolve_local_filepath(telegram_path)
        return Path(telegram_path)

    async def check_health(self) -> bool:
        if self._impl is not None and hasattr(self._impl, "check_health"):
            return await self._impl.check_health()
        return self._is_running


# ---------------------------------------------------------------------------
# E2E Status Notifier Adapter
# ---------------------------------------------------------------------------
class E2EStatusNotifier:
    """StatusNotifier adapter handling TelegramRetryAfter and TelegramBadRequest."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        lang: str = "ru",
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.lang = lang
        self.last_text: Optional[str] = None
        self.closed: bool = False
        if _SvcStatusNotifier is not None:
            self._impl = _SvcStatusNotifier(bot, chat_id, message_id, lang=lang)
        else:
            self._impl = None

    async def notify(self, status_key: str, **kwargs: Any) -> None:
        if self.closed:
            return
        engine = E2EI18nEngine()
        text = engine.get(status_key, lang=self.lang, **kwargs)
        self.last_text = text

        if self._impl is not None and hasattr(self._impl, "notify"):
            await self._impl.notify(status_key, **kwargs)
            return

        retries = 2
        while retries >= 0:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                break
            except TelegramRetryAfter as e:
                retries -= 1
                await asyncio.sleep(min(float(e.retry_after), 0.05))
            except TelegramBadRequest as e:
                err_msg = str(e).lower()
                if "message to edit not found" in err_msg or "message is not modified" in err_msg:
                    break
                raise
            except Exception:
                break

    async def close(self) -> None:
        self.closed = True
        if self._impl is not None and hasattr(self._impl, "close"):
            await self._impl.close()


# ---------------------------------------------------------------------------
# E2E Keyboard Builders
# ---------------------------------------------------------------------------
class E2EKeyboards:
    @staticmethod
    def build_quality_keyboard(query_id: str, lang: str = "ru") -> InlineKeyboardMarkup:
        engine = E2EI18nEngine()
        # Clean label without embedded emoji tag for make_inline_button
        mp3_label = engine.get("btn.mp3_320k", lang=lang, emoji_audio="").strip()
        flac_label = engine.get("btn.flac_lossless", lang=lang, emoji_vinyl="").strip()

        btn_mp3 = E2EEmoji.make_inline_button(
            text=mp3_label,
            callback_data=f"quality:mp3:{query_id}",
            emoji_id=ID_AUDIO,
            fallback_emoji="🎵",
        )
        btn_flac = E2EEmoji.make_inline_button(
            text=flac_label,
            callback_data=f"quality:flac:{query_id}",
            emoji_id=ID_VINYL,
            fallback_emoji="💿",
        )
        return InlineKeyboardMarkup(inline_keyboard=[[btn_mp3, btn_flac]])

    @staticmethod
    def build_settings_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
        engine = E2EI18nEngine()
        btn_quality = E2EEmoji.make_inline_button(
            text=engine.get("btn.settings_quality", lang=lang, emoji_quality="").strip(),
            callback_data="settings:quality",
            emoji_id=ID_QUALITY,
            fallback_emoji="🎚",
        )
        btn_language = E2EEmoji.make_inline_button(
            text=engine.get("btn.settings_language", lang=lang).strip(),
            callback_data="settings:language",
            fallback_emoji="🌐",
        )
        return InlineKeyboardMarkup(inline_keyboard=[[btn_quality], [btn_language]])

    @staticmethod
    def build_language_keyboard(current_pref: Optional[str] = None, lang: str = "ru") -> InlineKeyboardMarkup:
        engine = E2EI18nEngine()
        mark_ru = " " + E2EEmoji.e(ID_CHECK, "✅") if current_pref == "ru" else ""
        mark_en = " " + E2EEmoji.e(ID_CHECK, "✅") if current_pref == "en" else ""
        mark_auto = " " + E2EEmoji.e(ID_CHECK, "✅") if not current_pref or current_pref == "auto" else ""

        btn_ru = InlineKeyboardButton(
            text=engine.get("btn.lang_ru", lang=lang, mark=mark_ru),
            callback_data="set_lang:ru",
        )
        btn_en = InlineKeyboardButton(
            text=engine.get("btn.lang_en", lang=lang, mark=mark_en),
            callback_data="set_lang:en",
        )
        btn_auto = InlineKeyboardButton(
            text=engine.get("btn.lang_auto", lang=lang, detected=lang.upper(), mark=mark_auto),
            callback_data="set_lang:auto",
        )
        btn_back = InlineKeyboardButton(
            text=engine.get("btn.back", lang=lang),
            callback_data="settings:main",
        )
        return InlineKeyboardMarkup(inline_keyboard=[[btn_ru, btn_en], [btn_auto], [btn_back]])

    @staticmethod
    def build_quality_settings_keyboard(current_pref: str = "ASK", lang: str = "ru") -> InlineKeyboardMarkup:
        engine = E2EI18nEngine()
        mark_ask = " " + E2EEmoji.e(ID_CHECK, "✅") if current_pref == "ASK" else ""
        mark_mp3 = " " + E2EEmoji.e(ID_CHECK, "✅") if current_pref == "MP3" else ""
        mark_flac = " " + E2EEmoji.e(ID_CHECK, "✅") if current_pref == "FLAC" else ""

        btn_ask = InlineKeyboardButton(
            text=engine.get("btn.ask_always", lang=lang, mark=mark_ask),
            callback_data="set_quality:ASK",
        )
        btn_mp3 = InlineKeyboardButton(
            text=engine.get("btn.mp3_pref", lang=lang, mark=mark_mp3),
            callback_data="set_quality:MP3",
        )
        btn_flac = InlineKeyboardButton(
            text=engine.get("btn.flac_pref", lang=lang, mark=mark_flac),
            callback_data="set_quality:FLAC",
        )
        btn_back = InlineKeyboardButton(
            text=engine.get("btn.back", lang=lang),
            callback_data="settings:main",
        )
        return InlineKeyboardMarkup(inline_keyboard=[[btn_ask], [btn_mp3], [btn_flac], [btn_back]])


# ---------------------------------------------------------------------------
# E2E Metadata Card Builder
# ---------------------------------------------------------------------------
def build_metadata_card(data: Dict[str, Any], lang: str = "ru") -> str:
    """Build localized HTML metadata card."""
    engine = E2EI18nEngine()
    artist = data.get("artist") or engine.get("card.unknown_artist", lang=lang)
    title = data.get("title") or engine.get("card.unknown_track", lang=lang)
    album = data.get("album") or "—"
    year = str(data.get("year")) if data.get("year") else "—"
    duration = data.get("duration_formatted") or engine.get("duration.unknown", lang=lang)
    explicit = " [18+]" if data.get("explicit") else ""

    return engine.get(
        "card.template",
        lang=lang,
        artist=artist,
        title=title,
        album=album,
        year=year,
        duration=duration,
        explicit=explicit,
    )


# ---------------------------------------------------------------------------
# Concurrency Semaphore Manager Adapter
# ---------------------------------------------------------------------------
class E2EDownloadConcurrencyManager:
    """Download concurrency manager preventing thread exhaustion."""

    def __init__(self, max_concurrent: int = 3) -> None:
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.active_count = 0
        self.queued_count = 0

    async def acquire(self):
        self.queued_count += 1
        await self._semaphore.acquire()
        self.queued_count -= 1
        self.active_count += 1
        return self

    def release(self):
        self.active_count -= 1
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()

    @property
    def is_queued(self) -> bool:
        return self.active_count >= self.max_concurrent


# ---------------------------------------------------------------------------
# Pytest Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_custom_emoji_between_tests():
    """Не даём тестам с кастомными эмодзи влиять на следующие тесты."""
    yield
    E2EEmoji.set_custom_emoji_enabled(False)


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.get_me = AsyncMock()
    bot.get_me.return_value = MagicMock(id=100001, is_bot=True, username="mus_downloader_bot")
    return bot


@pytest.fixture
def user_factory() -> Callable[..., User]:
    def _make_user(
        user_id: int = 123456,
        language_code: Optional[str] = "ru",
        first_name: str = "TestUser",
        is_bot: bool = False,
    ) -> User:
        return User(
            id=user_id,
            is_bot=is_bot,
            first_name=first_name,
            language_code=language_code,
        )
    return _make_user


@pytest.fixture
def message_factory(user_factory: Callable[..., User]) -> Callable[..., Message]:
    def _make_message(
        text: str = "/start",
        user_id: int = 123456,
        language_code: Optional[str] = "ru",
        chat_id: int = 123456,
    ) -> Message:
        msg = MagicMock(spec=Message)
        msg.message_id = 999
        msg.text = text
        msg.from_user = user_factory(user_id=user_id, language_code=language_code)
        msg.chat = MagicMock(spec=Chat)
        msg.chat.id = chat_id
        msg.answer = AsyncMock()
        msg.reply = AsyncMock()
        msg.edit_text = AsyncMock()
        return msg
    return _make_message


@pytest.fixture
def callback_factory(user_factory: Callable[..., User]) -> Callable[..., CallbackQuery]:
    def _make_callback(
        data: str,
        user_id: int = 123456,
        language_code: Optional[str] = "ru",
        chat_id: int = 123456,
    ) -> CallbackQuery:
        cb = MagicMock(spec=CallbackQuery)
        cb.id = "cb_12345"
        cb.data = data
        cb.from_user = user_factory(user_id=user_id, language_code=language_code)
        cb.message = MagicMock(spec=Message)
        cb.message.message_id = 999
        cb.message.chat = MagicMock(spec=Chat)
        cb.message.chat.id = chat_id
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        return cb
    return _make_callback


@pytest.fixture
def temp_settings_path(tmp_path: Path) -> Path:
    return tmp_path / "user_settings_e2e.json"


@pytest.fixture
def user_settings(temp_settings_path: Path) -> E2EUserSettings:
    return E2EUserSettings(file_path=temp_settings_path)


@pytest.fixture
def query_store() -> E2EQueryStore:
    return E2EQueryStore(ttl=3600)


@pytest.fixture
def i18n_engine() -> E2EI18nEngine:
    engine = E2EI18nEngine()
    engine.set_locale("ru")
    return engine
