#!/usr/bin/env python3
"""
Telegram-бот оболочка для mus-downloader.
Запуск: uv run python bot.py

Возможности:
- Управление доступом через белый список ALLOWED_USERS в .env
- Карточка метаданных с выбором качества (MP3 320k / FLAC Lossless)
- Настройка качества по умолчанию через команду /quality
- Нативная отправка аудио (send_audio) с обложкой, артистом, названием и длительностью
- Поддержка локального Telegram Bot API Server (до 2 ГБ) через BOT_API_SERVER_URL
- Умная проверка лимита 50 МБ для стандартного Cloud Bot API
- Параллельное скачивание нескольких треков без взаимных блокировок
- Изолированное временное хранилище с очисткой при старте и завершении скачиваний
"""

import asyncio
import functools
import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Union

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
    TelegramObject,
)


import config
import core
from core import metadata, tagger
from .emoji import emoji_kwargs, set_custom_emoji_enabled, strip_html
from .fileid_cache import get_file_id_cache, is_invalid_file_id_error
from .i18n import detect_lang, t, t_plain
from .local_bot_api import LocalBotAPIManager
from .notifier import StatusNotifier, escape_html, format_status_text
from .process_lock import SingleInstanceLock
from .storage import QueryStore, UserSettings
from .keyboards import (
    build_quality_keyboard,
    build_quality_settings_keyboard,
    build_settings_keyboard,
    build_search_results_keyboard,
)


__all__ = [
    "LocalBotAPIManager",
    "SingleInstanceLock",
    "QueryStore",
    "UserSettings",
    "StatusNotifier",
    "user_settings",
    "query_store",
    "WhitelistMiddleware",
    "create_bot",
    "cleanup_temp_dir",
    "format_duration",
    "escape_html",
    "build_metadata_card",
    "build_quality_keyboard",
    "build_settings_keyboard",
    "build_search_results_keyboard",
    "handle_start",
    "handle_settings_command",
    "handle_quality_command",
    "handle_search_command",
    "handle_preference_callback",
    "handle_set_search_callback",
    "handle_search_callback",
    "handle_download_callback",
    "handle_track_query",
    "handle_unsupported_content",
    "run_download_and_send",
    "prepare_thumbnail",
    "show_search_results",
    "main",
    "core",
    "tagger",
    "metadata",
    "Bot",
    "Dispatcher",
    "strip_html",
    "t_plain",
]

# Wrap CallbackQuery.answer to guarantee that no raw HTML tags (e.g. <tg-emoji>)
# are ever displayed in Telegram callback query notification toasts or alerts.
_orig_callback_answer = CallbackQuery.answer


@functools.wraps(_orig_callback_answer)
async def _safe_callback_answer(self: CallbackQuery, text: Optional[str] = None, **kwargs: Any) -> bool:
    if text is not None:
        text = strip_html(text)
    return await _orig_callback_answer(self, text=text, **kwargs)


CallbackQuery.answer = _safe_callback_answer  # type: ignore[assignment]

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mus_bot")

MAX_CLOUD_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ лимит для Telegram Cloud Bot API
MAX_LOCAL_FILE_SIZE = 2000 * 1024 * 1024  # 2 ГБ лимит для локального Bot API сервера


def format_duration(seconds: Optional[float | int], lang: str = "ru") -> str:
    """Форматирует длительность в секундах в строку MM:SS."""
    if seconds is None or seconds <= 0:
        return t("duration.unknown", lang)
    total_sec = int(round(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    return f"{mins}:{secs:02d}"


def cleanup_temp_dir(temp_dir: Path) -> int:
    """
    Очищает временную директорию от остаточных файлов и папок.
    Возвращает количество удаленных элементов.
    """
    removed = 0
    if not temp_dir.exists():
        temp_dir.mkdir(parents=True, exist_ok=True)
        return removed

    for item in list(temp_dir.iterdir()):
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
            else:
                item.unlink(missing_ok=True)
                removed += 1
        except Exception as e:
            logger.warning(f"Не удалось удалить временный объект {item}: {e}")
    return removed


class WhitelistMiddleware(BaseMiddleware):
    """
    Middleware для проверки доступа пользователей по белому списку ALLOWED_USERS.
    Если пользователь не в списке, запросы отклоняются с информативным сообщением.
    """

    def __init__(self, allowed_users: Set[int]):
        self.allowed_users = allowed_users

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = user.id if user else None

        # Проверка белого списка
        is_allowed = bool(user_id is not None and self.allowed_users and user_id in self.allowed_users)

        if not is_allowed:
            logger.warning(f"Игнорирован запрос от неавторизованного пользователя {user_id}")
            return None

        return await handler(event, data)


def _process_image_to_telegram_thumbnail(raw_bytes: bytes, temp_dir: Path) -> Optional[Path]:
    """
    Преобразует произвольное изображение в валидный thumbnail для Telegram:
    - Формат: JPEG
    - Размер: <= 200 КБ
    - Разрешение: до 320x320
    """
    if not raw_bytes or len(raw_bytes) < 100:
        return None

    raw_path = temp_dir / f"thumb_raw_{uuid.uuid4().hex}"
    out_path = temp_dir / f"thumb_{uuid.uuid4().hex}.jpg"
    try:
        raw_path.write_bytes(raw_bytes)
    except Exception:
        return None

    # 1. Попытка через ffmpeg смасштабировать до 320x320 и сжать в JPEG
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v", "3",
            str(out_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if res.returncode == 0 and out_path.exists() and 0 < out_path.stat().st_size <= 200 * 1024:
            return out_path
    except Exception:
        pass
    finally:
        raw_path.unlink(missing_ok=True)

    # 2. Если ffmpeg недоступен или не сработал:
    # Проверяем, является ли исходный файл уже валидным JPEG <= 200 КБ
    if len(raw_bytes) <= 200 * 1024 and raw_bytes.startswith(b"\xff\xd8\xff"):
        try:
            out_path.write_bytes(raw_bytes)
            return out_path
        except Exception:
            pass

    return None


def prepare_thumbnail(file_path: Path, album_art_url: Optional[str], temp_dir: Path) -> Optional[Path]:
    """
    Извлекает или скачивает миниатюру обложки для прикрепления к аудио Telegram.
    Масштабирует и валидирует обложку в формат JPEG <= 200 КБ.

    Функция синхронная: из async-кода вызывать через ``asyncio.to_thread``,
    чтобы не блокировать event loop (см. ``run_download_and_send``).
    """
    return _prepare_thumbnail_sync(file_path, album_art_url, temp_dir)


def _prepare_thumbnail_sync(file_path: Path, album_art_url: Optional[str], temp_dir: Path) -> Optional[Path]:
    """Синхронная реализация prepare_thumbnail для запуска в пуле потоков."""
    # 1. Попытка скачать по прямой ссылке
    if album_art_url:
        try:
            art_bytes, _ = tagger.download_cover_art(album_art_url)
            if art_bytes:
                processed = _process_image_to_telegram_thumbnail(art_bytes, temp_dir)
                if processed:
                    return processed
        except Exception as e:
            logger.debug(f"Не удалось скачать обложку по URL: {e}")

    # 2. Попытка извлечь обложку из вшитых тегов аудиофайла
    if file_path.exists():
        suffix = file_path.suffix.lower()
        try:
            raw_art: Optional[bytes] = None
            if suffix == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(str(file_path))
                if audio.pictures:
                    raw_art = audio.pictures[0].data
            elif suffix == ".mp3":
                from mutagen.mp3 import MP3
                from mutagen.id3 import ID3
                audio = MP3(str(file_path), ID3=ID3)
                if audio.tags:
                    apics = audio.tags.getall("APIC")
                    if apics:
                        raw_art = apics[0].data
            elif suffix in [".m4a", ".mp4"]:
                from mutagen.mp4 import MP4
                audio = MP4(str(file_path))
                covr = audio.get("covr")
                if covr and len(covr) > 0:
                    raw_art = covr[0]

            if raw_art:
                processed = _process_image_to_telegram_thumbnail(raw_art, temp_dir)
                if processed:
                    return processed
        except Exception as e:
            logger.debug(f"Не удалось извлечь обложку из тегов: {e}")

    return None


def create_bot(token: str, api_server_url: Optional[str] = None) -> Bot:
    """Создает экземпляр Bot с поддержкой кастомного Local Bot API Server при наличии."""
    if api_server_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api_server_url, is_local=True))
        return Bot(token=token, session=session)
    return Bot(token=token)


def build_metadata_card(meta: Dict[str, Any], lang: str = "ru") -> str:
    """Формирует локализованную карточку метаданных (эмодзи — через bot.emoji)."""
    emj = emoji_kwargs()
    artist = escape_html(meta.get("artist") or t("card.unknown_artist", lang))
    title = escape_html(meta.get("title") or t("card.unknown_track", lang))
    album = escape_html(meta.get("album") or "—")
    year = escape_html(str(meta.get("year") or "—"))
    duration = format_duration(meta.get("duration"), lang=lang)
    explicit = " [E]" if meta.get("explicit") else ""

    card = (
        f"{emj['emoji_audio']} <b>{artist} — {title}</b>{explicit}\n\n"
        f"{emj['emoji_vinyl']} <b>{t('card.album', lang)}</b> {album}\n"
        f"{emj['emoji_calendar']} <b>{t('card.year', lang)}</b> {year}\n"
        f"{emj['emoji_clock']} <b>{t('card.duration', lang)}</b> {duration}"
    )
    return card


def _get_user_id(event: Any) -> int:
    """Безопасно извлекает Telegram ID пользователя из Message/CallbackQuery."""
    user = getattr(event, "from_user", None)
    try:
        uid = getattr(user, "id", None)
    except Exception:
        uid = None
    return int(uid) if isinstance(uid, int) else 0


def resolve_lang(event: Any) -> str:
    """
    Определяет язык интерфейса пользователя:
    1. Ручной выбор в настройках (UserSettings.language) — высший приоритет.
    2. Автоопределение по Telegram language_code (ru* → ru, иначе en).
    """
    user = getattr(event, "from_user", None)
    user_id = _get_user_id(event)
    if user_id:
        override = user_settings.get_language(user_id)
        if override in ("ru", "en"):
            return override
    return detect_lang(user)





# Инициализация глобальных хранилищ
user_settings = UserSettings(config.DOWNLOAD_DIR / ".bot_user_settings.json")
query_store = QueryStore(max_items=1000)
active_download_tasks: Set[asyncio.Task] = set()
active_download_keys: Set[str] = set()
download_semaphore = asyncio.Semaphore(3)  # max 3 concurrent downloads
router = Router()



@router.message(CommandStart())
async def handle_start(message: Message):
    """Приветственное сообщение и краткая инструкция."""
    lang = resolve_lang(message)
    await message.answer(t("cmd.start", lang), parse_mode=ParseMode.HTML)


@router.message(Command("settings"))
async def handle_settings_command(message: Message):
    """Отображает единое меню настроек качества и режима поиска."""
    lang = resolve_lang(message)
    user_id = _get_user_id(message)
    quality = user_settings.get_quality(user_id)
    search_mode = user_settings.get_search_mode(user_id)

    pref_key = {"ASK": "pref.ask", "MP3": "pref.mp3", "FLAC": "pref.flac"}.get(quality, "pref.ask")
    quality_label = t(pref_key, lang)
    mode_label = t("search.mode_best" if search_mode == "BEST" else "search.mode_list", lang)

    text = t("settings.title", lang, current_pref=quality_label, current_search_mode=mode_label)
    await message.answer(
        text,
        reply_markup=build_settings_keyboard(quality, search_mode, lang),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("quality"))
async def handle_quality_command(message: Message):
    """Отображает меню настройки качества по умолчанию."""
    lang = resolve_lang(message)
    user_id = _get_user_id(message)
    pref = user_settings.get_quality(user_id)

    pref_key = {"ASK": "pref.ask", "MP3": "pref.mp3", "FLAC": "pref.flac"}.get(pref, "pref.ask")
    pref_label = t(pref_key, lang)

    text = t("settings.quality_title", lang, current_pref=pref_label)
    await message.answer(
        text,
        reply_markup=build_quality_settings_keyboard(pref, lang),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("pref:"))
async def handle_preference_callback(callback: CallbackQuery):
    """Обрабатывает выбор настройки качества по умолчанию."""
    lang = resolve_lang(callback)
    user_id = _get_user_id(callback)
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer(t_plain("error.invalid_request", lang), show_alert=True)
        return
    new_pref = parts[1]
    if new_pref not in ["ASK", "MP3", "FLAC"]:
        await callback.answer(t_plain("error.invalid_quality", lang), show_alert=True)
        return

    user_settings.set_quality(user_id, new_pref)

    pref_key = {"ASK": "pref.ask", "MP3": "pref.mp3", "FLAC": "pref.flac"}.get(new_pref, "pref.ask")
    pref_label = t(pref_key, lang)
    await callback.answer(t_plain("settings.quality_saved_alert", lang, pref=pref_label))

    search_mode = user_settings.get_search_mode(user_id)
    is_settings_menu = False
    reply_markup = getattr(callback.message, "reply_markup", None) if callback.message else None
    if reply_markup and hasattr(reply_markup, "inline_keyboard"):
        for row in reply_markup.inline_keyboard:
            for btn in row:
                cb_data = getattr(btn, "callback_data", None)
                if cb_data and cb_data.startswith("set_search:"):
                    is_settings_menu = True
                    break

    if is_settings_menu:
        mode_label = t("search.mode_best" if search_mode == "BEST" else "search.mode_list", lang)
        text = t("settings.title", lang, current_pref=pref_label, current_search_mode=mode_label)
        markup = build_settings_keyboard(new_pref, search_mode, lang)
    else:
        text = t("settings.quality_saved", lang, current_pref=pref_label)
        markup = build_quality_settings_keyboard(new_pref, lang)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("set_search:"))
async def handle_set_search_callback(callback: CallbackQuery):
    """Обрабатывает выбор режима поиска в настройках."""
    lang = resolve_lang(callback)
    user_id = _get_user_id(callback)
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer(t_plain("error.invalid_request", lang), show_alert=True)
        return
    mode = parts[1]
    if mode not in ["BEST", "LIST"]:
        return

    user_settings.set_search_mode(user_id, mode)
    await callback.answer(t_plain("settings.saved_alert", lang))

    quality = user_settings.get_quality(user_id)
    pref_key = {"ASK": "pref.ask", "MP3": "pref.mp3", "FLAC": "pref.flac"}.get(quality, "pref.ask")
    quality_label = t(pref_key, lang)
    mode_label = t("search.mode_best" if mode == "BEST" else "search.mode_list", lang)

    text = t("settings.title", lang, current_pref=quality_label, current_search_mode=mode_label)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_settings_keyboard(quality, mode, lang),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def _send_from_file_id_cache(
    bot: Bot,
    chat_id: int,
    meta: Dict[str, Any],
    quality: str,
    query_or_url: str,
    status_msg: Message,
    lang: str,
) -> bool:
    """
    Пытается отдать трек мгновенно, по сохранённому Telegram file_id.

    Возвращает True, если аудио ушло. False — если в кэше ничего не было
    либо Telegram признал file_id недействительным (тогда запись удаляется,
    и вызывающая сторона скачает трек заново).
    """
    cache = get_file_id_cache()
    entry = await asyncio.to_thread(cache.lookup, meta, quality, query_or_url)
    if not entry:
        return False

    try:
        await status_msg.edit_text(
            t("status.from_cache", lang),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        await bot.send_audio(
            chat_id=chat_id,
            audio=entry["file_id"],
            performer=entry.get("performer") or None,
            title=entry.get("title") or None,
            duration=entry.get("duration") or None,
        )
    except Exception as cache_err:
        if is_invalid_file_id_error(cache_err):
            logger.info(f"file_id устарел, удаляю из кэша: {cache_err}")
            await asyncio.to_thread(cache.invalidate_key, entry.get("key", ""))
            return False
        logger.warning(f"Не удалось отдать из кэша file_id, качаю заново: {cache_err}")
        return False

    logger.info(f"Трек отдан из кэша file_id: {entry.get('title')} [{quality}]")
    try:
        await status_msg.delete()
    except Exception:
        pass
    return True


async def run_download_and_send(
    bot: Bot,
    chat_id: int,
    query_or_url: str,
    target_quality: str,
    meta: Dict[str, Any],
    status_msg: Message,
    lang: str = "ru",
    dedup_keys: Optional[Set[str]] = None,
):
    """
    Фоновый рабочий процесс скачивания трека и отправки в Telegram.
    Выполняется в asyncio.create_task параллельно с другими задачами.
    """
    artist = meta.get("artist") or t("card.unknown_artist", lang)
    title = meta.get("title") or t("card.unknown_track", lang)
    album = meta.get("album")
    year = meta.get("year")
    duration = meta.get("duration")
    album_art_url = meta.get("album_art")

    task_dir = config.BOT_TEMP_DIR / f"dl_{uuid.uuid4().hex}"
    task_dir.mkdir(parents=True, exist_ok=True)

    emj = emoji_kwargs()
    header = f"{emj['emoji_queue']} <b>{escape_html(artist)} — {escape_html(title)}</b> [{target_quality}]"
    notifier = StatusNotifier(bot, status_msg, header=header, lang=lang)

    try:
        # 0. Пробуем отдать трек мгновенно по сохранённому file_id: без
        #    скачивания, без ffmpeg и без загрузки мегабайт в Telegram.
        if await _send_from_file_id_cache(
            bot, chat_id, meta, target_quality, query_or_url, status_msg, lang
        ):
            return

        # 1. Запуск скачивания в отдельном потоке (семафор ограничивает до 3 параллельных).
        # Статус «в очереди» показываем только если все слоты заняты.
        if download_semaphore.locked():
            notifier.notify(
                "status.queued",
                artist=escape_html(artist),
                title=escape_html(title),
                quality=target_quality,
            )
        async with download_semaphore:
            file_path = await asyncio.to_thread(
                core.download_track_by_link,
                url_or_query=query_or_url,
                target_quality=target_quality,
                dest_dir=task_dir,
                status_callback=notifier.notify,
                track_meta=meta,
                # Дисковый кэш боту не нужен: файл живёт в task_dir и
                # удаляется сразу после отправки. Повторы отдаёт кэш file_id.
                reuse_cached_file=False,
            )

        if not file_path or not file_path.exists():
            notifier.close()
            await status_msg.edit_text(
                t(
                    "error.download_failed",
                    lang,
                    artist=escape_html(artist),
                    title=escape_html(title),
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        # 2. Проверка размера файла и лимитов Bot API
        file_size = file_path.stat().st_size
        has_local_api = bool(config.BOT_API_SERVER_URL)
        if not has_local_api and hasattr(bot, "session") and hasattr(bot.session, "api"):
            is_local = getattr(bot.session.api, "is_local", None)
            if isinstance(is_local, bool):
                has_local_api = is_local

        if not has_local_api and file_size > MAX_CLOUD_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            notifier.close()
            await status_msg.edit_text(
                t("error.file_too_large_cloud", lang, size_mb=size_mb, limit_mb=50),
                parse_mode=ParseMode.HTML,
            )
            return

        if has_local_api and file_size > MAX_LOCAL_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            notifier.close()
            await status_msg.edit_text(
                t("error.file_too_large_local", lang, size_mb=size_mb),
                parse_mode=ParseMode.HTML,
            )
            return

        # 3. Подготовка миниатюры обложки (в пуле потоков, чтобы не блокировать loop)
        thumb_path = await asyncio.to_thread(prepare_thumbnail, file_path, album_art_url, task_dir)
        thumb_input = FSInputFile(str(thumb_path)) if thumb_path and thumb_path.exists() else None

        # 4. Обновление статуса перед отправкой.
        # Передаём ключ, а не готовый t(): format_status_text сам подставит
        # эмодзи, и текст не уйдёт в escape_html (иначе тег виден как текст).
        await notifier.update_now("status.uploading")
        notifier.close()

        caption_lines = [f"{emj['emoji_audio']} <b>{escape_html(artist)} — {escape_html(title)}</b>"]
        if album:
            caption_lines.append(f"{emj['emoji_vinyl']} <i>{escape_html(album)}</i>")
        if year:
            caption_lines.append(f"{emj['emoji_calendar']} {escape_html(str(year))}")
        caption = "\n".join(caption_lines)

        # 5. Отправка нативного аудио в Telegram
        clean_title = re.sub(r'[\\/*?:"<>|]', "_", f"{artist} - {title}").strip()
        clean_filename = f"{clean_title}{file_path.suffix}"
        audio_input = FSInputFile(str(file_path), filename=clean_filename)
        dur_sec = int(round(duration)) if duration and duration > 0 else None

        try:
            sent_message = await bot.send_audio(
                chat_id=chat_id,
                audio=audio_input,
                performer=artist,
                title=title,
                duration=dur_sec,
                thumbnail=thumb_input,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as send_err:
            logger.warning(f"Ошибка при отправке с миниатюрой, повтор без миниатюры: {send_err}")
            sent_message = await bot.send_audio(
                chat_id=chat_id,
                audio=audio_input,
                performer=artist,
                title=title,
                duration=dur_sec,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

        # 5.1 Запоминаем file_id: повторный запрос этого же трека любому
        #     пользователю теперь отдаётся мгновенно, без скачивания.
        cached_file_id = getattr(getattr(sent_message, "audio", None), "file_id", None)
        if cached_file_id:
            await asyncio.to_thread(
                get_file_id_cache().save,
                cached_file_id,
                meta,
                target_quality,
                query_or_url,
                performer=artist,
                title=title,
                duration=dur_sec or 0,
            )

        # 6. Удаление служебного сообщения статуса после успешной отправки
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception(f"Исключение при скачивании/отправке трека: {e}")
        notifier.close()
        try:
            await status_msg.edit_text(
                t("error.generic", lang, error=escape_html(str(e))),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        notifier.close()
        if dedup_keys:
            active_download_keys.difference_update(dedup_keys)
        # Гарантированная очистка временной папки этого скачивания
        shutil.rmtree(task_dir, ignore_errors=True)


@router.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery, bot: Bot):
    """Обрабатывает нажатие инлайн-кнопок скачивания [MP3 320k] / [FLAC Lossless]."""
    lang = resolve_lang(callback)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(t_plain("error.invalid_request", lang), show_alert=True)
        return

    _, qid, quality = parts
    if quality not in ["MP3", "FLAC"]:
        await callback.answer(t_plain("error.invalid_download_quality", lang), show_alert=True)
        return

    # Защита от дубликатов по кнопке
    btn_key = f"btn:{qid}"
    if btn_key in active_download_keys:
        await callback.answer(t_plain("status.starting_dl", lang, quality=quality))
        return

    query_data = await query_store.get(qid)

    if not query_data:
        await callback.answer(
            t_plain("error.expired_button", lang),
            show_alert=True,
        )
        return

    url_or_query = query_data["url_or_query"]
    meta = query_data["meta"]
    artist = meta.get("artist") or t("card.unknown_artist", lang)
    title = meta.get("title") or t("card.unknown_track", lang)

    chat_id = callback.message.chat.id if callback.message and callback.message.chat else 0
    track_key = f"{chat_id}:{artist.strip().lower()}:{title.strip().lower()}:{quality}"
    if track_key in active_download_keys:
        logger.info(f"Трек {artist} - {title} [{quality}] уже скачивается для чата {chat_id}")
        await callback.answer(t_plain("status.starting_dl", lang, quality=quality))
        return

    active_download_keys.add(btn_key)
    active_download_keys.add(track_key)

    await callback.answer(t_plain("status.starting_dl", lang, quality=quality))

    # Убираем клавиатуру у карточки, чтобы предотвратить повторные случайные клики
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    emj = emoji_kwargs()
    header = f"{emj['emoji_queue']} <b>{escape_html(artist)} — {escape_html(title)}</b> [{quality}]"
    # Создаем сообщение со статусом процесса
    status_msg = await bot.send_message(
        chat_id=chat_id,
        text=format_status_text("status.searching_sources", lang=lang, header=header),
        parse_mode=ParseMode.HTML,
    )

    # Запуск параллельной асинхронной задачи скачивания
    task = asyncio.create_task(
        run_download_and_send(
            bot=bot,
            chat_id=chat_id,
            query_or_url=url_or_query,
            target_quality=quality,
            meta=meta,
            status_msg=status_msg,
            lang=lang,
            dedup_keys={btn_key, track_key},
        )
    )
    active_download_tasks.add(task)
    task.add_done_callback(active_download_tasks.discard)


async def show_search_results(message_or_target: Union[Message, CallbackQuery], query: str, bot: Bot, lang: str):
    """Выполняет мульти-поиск и отправляет список из 5 треков с инлайн-кнопками выбора."""
    if isinstance(message_or_target, CallbackQuery):
        chat_id = message_or_target.message.chat.id
        reply_target = message_or_target.message
    else:
        chat_id = message_or_target.chat.id
        reply_target = message_or_target

    searching_msg = await reply_target.answer(
        t("status.searching_meta", lang),
        parse_mode=ParseMode.HTML,
    )

    try:
        candidates = await asyncio.to_thread(core.search_tracks, query, 5)
    except Exception as e:
        logger.warning(f"Ошибка при мульти-поиске: {e}")
        candidates = []

    if not candidates:
        await searching_msg.edit_text(
            t("search.not_found", lang, query=escape_html(query)),
            parse_mode=ParseMode.HTML,
        )
        return

    qid = uuid.uuid4().hex[:12]
    await query_store.put(qid, {
        "query": query,
        "candidates": candidates,
        "type": "search_list",
    })

    lines = []
    for idx, c in enumerate(candidates, 1):
        artist = escape_html(c.get("artist") or t("card.unknown_artist", lang))
        title = escape_html(c.get("title") or t("card.unknown_track", lang))
        album = c.get("album")
        year = c.get("year")
        dur = c.get("duration")
        sq = c.get("source_quality") or c.get("source") or "Web"
        explicit = " [E]" if c.get("explicit") else ""

        details = []
        if album:
            details.append(escape_html(album))
        if year:
            details.append(str(year))
        if dur:
            m = int(dur) // 60
            s = int(dur) % 60
            details.append(f"{m}:{s:02d}")
        det_str = f" <i>({', '.join(details)})</i>" if details else ""

        lines.append(f"<b>{idx}.</b> {artist} — <b>{title}</b>{explicit}{det_str}\n   <code>[{sq}]</code>")

    results_text = "\n\n".join(lines)
    keyboard = build_search_results_keyboard(qid, len(candidates), lang)

    await searching_msg.edit_text(
        t("search.title", lang, query=escape_html(query), results_text=results_text),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("search", "s"))
async def handle_search_command(message: Message, bot: Bot):
    """Команда /search <запрос> или /s <запрос>."""
    lang = resolve_lang(message)
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(t("cmd.search_prompt", lang), parse_mode=ParseMode.HTML)
        return

    query = parts[1].strip()
    await show_search_results(message, query, bot, lang)


@router.callback_query(F.data.startswith("srch:"))
async def handle_search_callback(callback: CallbackQuery, bot: Bot):
    """Обрабатывает выбор номера трека или отмену из списка результатов поиска."""
    lang = resolve_lang(callback)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(t_plain("error.invalid_request", lang), show_alert=True)
        return

    _, qid, action = parts
    query_data = await query_store.get(qid)
    if not query_data:
        await callback.answer(t_plain("error.expired_button", lang), show_alert=True)
        return

    if action == "cancel":
        await callback.answer(t_plain("search.cancelled", lang))
        try:
            await callback.message.edit_text(
                t("search.cancelled", lang),
                reply_markup=None,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if not action.isdigit():
        return

    idx = int(action) - 1
    candidates = query_data.get("candidates", [])
    if idx < 0 or idx >= len(candidates):
        await callback.answer(t_plain("error.invalid_request", lang), show_alert=True)
        return

    candidate = candidates[idx]
    user_id = _get_user_id(callback)
    pref = user_settings.get_quality(user_id)

    # Убираем клавиатуру у сообщения со списком
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Если качество уже настроено в MP3 или FLAC -> сразу запускаем скачивание!
    if pref in ["MP3", "FLAC"]:
        artist = candidate.get("artist") or t("card.unknown_artist", lang)
        title = candidate.get("title") or t("card.unknown_track", lang)
        chat_id = callback.message.chat.id
        track_key = f"{chat_id}:{artist.strip().lower()}:{title.strip().lower()}:{pref}"
        if track_key in active_download_keys:
            logger.info(f"Трек {artist} - {title} [{pref}] уже скачивается для чата {chat_id}")
            await callback.answer(t_plain("status.starting_dl", lang, quality=pref))
            return

        active_download_keys.add(track_key)
        await callback.answer(t_plain("status.starting_dl", lang, quality=pref))

        emj = emoji_kwargs()
        header = f"{emj['emoji_queue']} <b>{escape_html(artist)} — {escape_html(title)}</b> [{pref}]"
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=format_status_text("status.searching_sources", lang=lang, header=header),
            parse_mode=ParseMode.HTML,
        )

        task = asyncio.create_task(
            run_download_and_send(
                bot=bot,
                chat_id=chat_id,
                query_or_url=candidate.get("url") or f"{artist} - {title}",
                target_quality=pref,
                meta=candidate,
                status_msg=status_msg,
                lang=lang,
                dedup_keys={track_key},
            )
        )
        active_download_tasks.add(task)
        task.add_done_callback(active_download_tasks.discard)
        return

    # Если качество стоит ASK -> показываем карточку выбранного трека с кнопками качества
    await callback.answer()
    track_qid = uuid.uuid4().hex[:12]
    await query_store.put(track_qid, {
        "url_or_query": candidate.get("url") or f"{candidate.get('artist')} - {candidate.get('title')}",
        "meta": candidate
    })

    card_text = build_metadata_card(candidate, lang)
    art_url = candidate.get("album_art")
    keyboard = build_quality_keyboard(track_qid, lang)

    if art_url:
        try:
            await callback.message.answer_photo(
                photo=art_url,
                caption=card_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.warning(f"Не удалось отправить фото обложки: {e}")

    await callback.message.answer(
        card_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text)
async def handle_track_query(message: Message, bot: Bot):
    """
    Обрабатывает входящие текстовые сообщения (ссылки или названия треков).
    Извлекает метаданные, отправляет карточку трека и кнопки выбора качества.
    """
    query_text = message.text.strip()
    if not query_text:
        return

    lang = resolve_lang(message)

    if query_text.startswith("/"):
        await message.answer(
            t("status.unknown_cmd", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    user_id = _get_user_id(message)
    pref = user_settings.get_quality(user_id)
    search_mode = user_settings.get_search_mode(user_id)

    is_url = query_text.startswith("http://") or query_text.startswith("https://")

    # Если это текстовый запрос (не ссылка) и у пользователя включен режим LIST
    if not is_url and search_mode == "LIST":
        await show_search_results(message, query_text, bot, lang)
        return

    # Первичное уведомление о поиске метаданных
    searching_msg = await message.answer(t("status.searching_meta", lang), parse_mode=ParseMode.HTML)

    is_url = query_text.startswith("http://") or query_text.startswith("https://")

    # Разрешение метаданных в потоке
    def fetch_meta():
        if is_url:
            return metadata.get_track_metadata(query_text)
        return metadata.resolve_query_metadata(query_text)

    try:
        meta = await asyncio.to_thread(fetch_meta)
    except Exception as e:
        logger.warning(f"Ошибка при получении метаданных: {e}")
        meta = None

    if not meta or not meta.get("artist") or not meta.get("title"):
        await searching_msg.edit_text(
            t("status.track_not_found", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    # Сохраняем в кэш запросов
    qid = uuid.uuid4().hex[:12]
    await query_store.put(qid, {"url_or_query": query_text, "meta": meta})

    card_text = build_metadata_card(meta, lang)
    art_url = meta.get("album_art")

    # Удаляем временное сообщение о поиске
    try:
        await searching_msg.delete()
    except Exception:
        pass

    # Если у пользователя настроено авто-скачивание в MP3 или FLAC
    if pref in ["MP3", "FLAC"]:
        artist = meta.get("artist") or t("card.unknown_artist", lang)
        title = meta.get("title") or t("card.unknown_track", lang)
        track_key = f"{message.chat.id}:{artist.strip().lower()}:{title.strip().lower()}:{pref}"
        if track_key in active_download_keys:
            logger.info(f"Трек {artist} - {title} [{pref}] уже скачивается для чата {message.chat.id}, повтор пропущен.")
            return

        active_download_keys.add(track_key)

        # Качество уже выбрано — карточку шлём без кнопок
        status_msg = await message.answer(
            t("status.auto_download", lang, pref=pref, card_text=card_text),
            parse_mode=ParseMode.HTML,
        )
        task = asyncio.create_task(
            run_download_and_send(
                bot=bot,
                chat_id=message.chat.id,
                query_or_url=query_text,
                target_quality=pref,
                meta=meta,
                status_msg=status_msg,
                lang=lang,
                dedup_keys={track_key},
            )
        )
        active_download_tasks.add(task)
        task.add_done_callback(active_download_tasks.discard)
        return

    # Стандартный режим (спрашивать каждый раз): карточка трека с кнопками качества
    keyboard = build_quality_keyboard(qid, lang)

    # Пробуем отправить с красивой обложкой, при сбое отправляем текстом
    sent = False
    if art_url:
        try:
            await message.answer_photo(
                photo=art_url,
                caption=card_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            sent = True
        except Exception as e:
            logger.debug(f"Не удалось отправить фото по URL ({e}), отправляем текстом")

    if not sent:
        await message.answer(card_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@router.message()
async def handle_unsupported_content(message: Message):
    """Ответ на нетекстовые сообщения (стикеры, фото, голосовые)."""
    await message.answer(
        t("status.unsupported_content", resolve_lang(message)),
        parse_mode=ParseMode.HTML,
    )


async def main():
    """Точка входа и инициализация Telegram-бота."""
    # Настройка UTF-8 для вывода в консоль на Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    token = config.BOT_TOKEN
    if not token:
        logger.error(
            "BOT_TOKEN не задан в .env файле! "
            "Пожалуйста, добавьте BOT_TOKEN=<ваш_токен> в файл .env и перезапустите бота."
        )
        sys.exit(1)

    # Кастомные (премиум) эмодзи: включены по умолчанию, отключаются CUSTOM_EMOJI=false
    set_custom_emoji_enabled(config.CUSTOM_EMOJI)

    # Проверка единственного запущенного экземпляра бота для предотвращения TelegramConflictError
    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        logger.error(
            "Не удалось запустить бота: обнаружен уже работающий экземпляр bot.py. "
            "Завершите старый процесс перед повторным запуском."
        )
        sys.exit(1)

    api_manager: Optional[LocalBotAPIManager] = None
    bot: Optional[Bot] = None
    try:
        # Процедура очистки временных файлов от прошлых сессий
        removed_items = cleanup_temp_dir(config.BOT_TEMP_DIR)
        if removed_items > 0:
            logger.info(f"Очищены остаточные временные файлы: удалено {removed_items} объектов.")

        allowed = config.ALLOWED_USERS
        if not allowed:
            logger.warning(
                "ВНИМАНИЕ: ALLOWED_USERS пуст! Ни один пользователь не сможет использовать бота. "
                "Укажите ваш Telegram ID в .env (параметр ALLOWED_USERS)."
            )
        else:
            logger.info(f"Белый список активен. Разрешено пользователей: {len(allowed)}.")

        api_url: Optional[str] = config.BOT_API_SERVER_URL or None

        if config.is_local_bot_api_enabled():
            api_manager = LocalBotAPIManager(api_url=api_url)
            local_url = await api_manager.start()
            if local_url:
                api_url = local_url
                config.BOT_API_SERVER_URL = local_url
            else:
                api_url = None
                config.BOT_API_SERVER_URL = ""

        if api_url:
            logger.info(f"Используется локальный Telegram Bot API Server: {api_url} (файлы до 2 ГБ)")
        else:
            logger.info("Используется стандартный Telegram Cloud Bot API (лимит 50 МБ на файл)")

        bot = create_bot(token=token, api_server_url=api_url)
        dp = Dispatcher()

        # Регистрация middleware белого списка пользователей
        whitelist = WhitelistMiddleware(allowed_users=allowed)
        dp.message.middleware(whitelist)
        dp.callback_query.middleware(whitelist)

        dp.include_router(router)

        # Проверяем связь с Bot API перед запуском polling
        bot_user = None
        if hasattr(bot, "get_me"):
            for attempt in range(1, 6):
                try:
                    res = bot.get_me()
                    if asyncio.iscoroutine(res):
                        bot_user = await res
                    else:
                        bot_user = res
                    break
                except Exception as e:
                    logger.warning(f"Ожидание готовности Bot API ({attempt}/5)...")
                    await asyncio.sleep(0.8)

        if bot_user is None and (api_manager or api_url):
            logger.warning(
                "Локальный Telegram Bot API Server не ответил на getMe. "
                "Переключение на стандартный Telegram Cloud API..."
            )
            if api_manager:
                await api_manager.stop()
                api_manager = None
            api_url = None
            config.BOT_API_SERVER_URL = ""
            if bot and bot.session:
                await bot.session.close()
            bot = create_bot(token=token, api_server_url=None)
            if hasattr(bot, "get_me"):
                try:
                    res = bot.get_me()
                    bot_user = await res if asyncio.iscoroutine(res) else res
                except Exception:
                    bot_user = None

        bot_info = ""
        uname = getattr(bot_user, "username", None)
        if isinstance(uname, str) and uname:
            uid = getattr(bot_user, "id", "")
            bot_info = f"  • Бот: @{uname} (ID: {uid})\n"

        banner = (
            "\n=======================================================\n"
            "      mus-downloader Telegram Bot запущен и готов!     \n"
            f"  • Временная папка: {config.BOT_TEMP_DIR}\n"
            f"  • Пользователей в белом списке: {len(allowed)}\n"
            f"  • Сервер Bot API: {api_url or 'Telegram Cloud (50MB)'}\n"
            f"{bot_info}"
            "=======================================================\n"
        )
        print(banner)

        # Сброс вебхуков и зависших сессий polling перед запуском
        if hasattr(bot, "delete_webhook"):
            for attempt in range(1, 4):
                try:
                    res = bot.delete_webhook(drop_pending_updates=True)
                    if asyncio.iscoroutine(res):
                        await res
                    break
                except Exception as e:
                    if "conflict" in str(e).lower():
                        logger.info(f"Ожидание освобождения сессии Telegram ({attempt}/3)...")
                        await asyncio.sleep(1.5)
                    else:
                        logger.debug(f"Сброс вебхука пропущен: {e}")
                        break

        await dp.start_polling(bot)
    finally:
        if active_download_tasks:
            logger.info(f"Ожидание/отмена {len(active_download_tasks)} активных загрузок...")
            for t in list(active_download_tasks):
                t.cancel()
            await asyncio.gather(*active_download_tasks, return_exceptions=True)
        # Завершающая очистка временной папки
        cleanup_temp_dir(config.BOT_TEMP_DIR)
        if bot is not None and hasattr(bot, "session") and bot.session:
            await bot.session.close()
        if api_manager:
            await api_manager.stop()
        instance_lock.release()


def run() -> None:
    """Синхронная точка входа для console-скрипта ``bot``."""
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    run()
