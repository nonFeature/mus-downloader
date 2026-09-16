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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

import config
import core
import metadata
import tagger

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mus_bot")

MAX_CLOUD_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ лимит для Telegram Cloud Bot API
MAX_LOCAL_FILE_SIZE = 2000 * 1024 * 1024  # 2 ГБ лимит для локального Bot API сервера


def format_duration(seconds: Optional[float | int]) -> str:
    """Форматирует длительность в секундах в строку MM:SS."""
    if seconds is None or seconds <= 0:
        return "Неизвестно"
    total_sec = int(round(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    return f"{mins}:{secs:02d}"


def escape_html(text: Any) -> str:
    """Экранирует спецсимволы HTML."""
    if text is None:
        return ""
    return html.escape(str(text))


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


class UserSettings:
    """Хранилище персональных настроек пользователей (качество по умолчанию)."""

    def __init__(self, filepath: Optional[Path] = None):
        self.filepath = filepath
        self._settings: Dict[int, str] = {}
        self._load()

    def _load(self):
        if self.filepath and self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._settings = {int(k): str(v) for k, v in data.items()}
            except Exception as e:
                logger.warning(f"Ошибка загрузки настроек пользователей: {e}")
                self._settings = {}

    def _save(self):
        if self.filepath:
            try:
                self.filepath.parent.mkdir(parents=True, exist_ok=True)
                temp_file = self.filepath.with_suffix(f".tmp_{uuid.uuid4().hex}")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump({str(k): v for k, v in self._settings.items()}, f, ensure_ascii=False, indent=2)
                temp_file.replace(self.filepath)
            except Exception as e:
                logger.warning(f"Ошибка сохранения настроек пользователей: {e}")

    def get_quality(self, user_id: int) -> str:
        """Возвращает 'ASK', 'MP3' или 'FLAC'."""
        val = self._settings.get(user_id, "ASK")
        return val if val in ["ASK", "MP3", "FLAC"] else "ASK"

    def set_quality(self, user_id: int, quality: str):
        if quality in ["ASK", "MP3", "FLAC"]:
            self._settings[user_id] = quality
            self._save()


class QueryStore:
    """Потокобезопасное кэш-хранилище метаданных запросов для инлайн-кнопок."""

    def __init__(self, max_items: int = 1000, ttl_seconds: float = 7200.0):
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._store: Dict[str, Dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    async def put(self, qid: str, data: Dict[str, Any]):
        async with self._lock:
            data["_created_at"] = time.time()
            if qid in self._store:
                if qid in self._order:
                    self._order.remove(qid)
            elif len(self._order) >= self.max_items:
                oldest = self._order.pop(0)
                self._store.pop(oldest, None)
            self._order.append(qid)
            self._store[qid] = data

    async def get(self, qid: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            entry = self._store.get(qid)
            if not entry:
                return None
            if time.time() - entry.get("_created_at", 0) > self.ttl_seconds:
                self._store.pop(qid, None)
                if qid in self._order:
                    self._order.remove(qid)
                return None
            return dict(entry)


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


class StatusNotifier:
    """
    Потокобезопасный и троттлящий уведомитель статуса скачивания для Telegram.
    Защищает от превышения лимитов Telegram (429 Retry After) при частых обновлениях
    с гарантированной доставкой последнего обновления (trailing flush).
    """

    def __init__(self, bot: Bot, status_msg: Message, header: str, min_interval: float = 1.2):
        self.bot = bot
        self.status_msg = status_msg
        self.header = header
        self.min_interval = min_interval
        self._last_sent_time = 0.0
        self._last_sent_text = ""
        self._pending_text: Optional[str] = None
        self._timer_handle: Optional[asyncio.TimerHandle] = None
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._is_updating = False

    def notify(self, status: str):
        """Вызывается из синхронного рабочего потока."""
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._handle_status, status)

    def _handle_status(self, status: str):
        if self._closed:
            return
        # Очищаем внутренние префиксы [*], [!] для красивого отображения в Telegram
        cleaned = re.sub(r"^\s*\[[\*!+-]\]\s*", "", status).strip()
        if not cleaned:
            return
        text = f"{self.header}\n\n<i>{escape_html(cleaned)}</i>"
        if text == self._last_sent_text or text == self._pending_text:
            return
        self._pending_text = text
        self._maybe_send()

    def _maybe_send(self):
        if self._closed or not self._pending_text:
            return
        now = time.time()
        elapsed = now - self._last_sent_time
        if elapsed >= self.min_interval and not self._is_updating:
            if self._timer_handle:
                self._timer_handle.cancel()
                self._timer_handle = None
            text = self._pending_text
            self._pending_text = None
            asyncio.create_task(self._apply_update(text))
        elif not self._timer_handle:
            delay = max(0.05, self.min_interval - elapsed)
            self._timer_handle = self._loop.call_later(delay, self._timer_flush)

    def _timer_flush(self):
        self._timer_handle = None
        self._maybe_send()

    async def _apply_update(self, text: str):
        if self._closed:
            return
        self._is_updating = True
        self._last_sent_time = time.time()
        self._last_sent_text = text
        try:
            await self.status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        finally:
            self._is_updating = False
            if self._pending_text and not self._closed:
                self._maybe_send()

    async def update_now(self, status: str):
        """Немедленное обновление статуса (например, перед отправкой)."""
        if self._closed:
            return
        if self._timer_handle:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._pending_text = None
        cleaned = re.sub(r"^\s*\[[\*!+-]\]\s*", "", status).strip()
        text = f"{self.header}\n\n<i>{escape_html(cleaned)}</i>"
        self._last_sent_text = text
        self._last_sent_time = time.time()
        try:
            await self.status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    def close(self):
        self._closed = True
        if self._timer_handle:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._pending_text = None


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
    """
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


class LocalBotAPIManager:
    """
    Менеджер жизненного цикла локального Telegram Bot API Server.
    Автоматически запускает и останавливает локальный сервер Bot API
    (через скомпилированный бинарный файл или Docker) для отправки файлов до 2 ГБ.
    """

    def __init__(
        self,
        api_id: Optional[Union[int, str]] = None,
        api_hash: Optional[str] = None,
        port: Optional[int] = None,
        data_dir: Optional[Union[Path, str]] = None,
        bin_path: Optional[Union[Path, str]] = None,
        container_name: str = "mus-bot-api",
    ):
        raw_api_id = api_id if api_id is not None else config.TELEGRAM_API_ID
        self.api_id = str(raw_api_id).strip().strip("'\"") if raw_api_id else ""
        raw_api_hash = api_hash if api_hash is not None else config.TELEGRAM_API_HASH
        self.api_hash = str(raw_api_hash).strip().strip("'\"") if raw_api_hash else ""
        self.port = int(port if port is not None else config.BOT_API_PORT)
        raw_dir = data_dir if data_dir is not None else config.BOT_API_DATA_DIR
        self.data_dir = Path(raw_dir)
        self.custom_bin = str(bin_path if bin_path is not None else config.TELEGRAM_BOT_API_BIN).strip().strip("'\"")
        self.container_name = container_name

        self.process: Optional[asyncio.subprocess.Process] = None
        self.started_docker: bool = False
        self._is_external: bool = False

    def find_binary(self) -> Optional[Path]:
        """
        Ищет локальный исполняемый файл telegram-bot-api.
        Проверяет:
        1. TELEGRAM_BOT_API_BIN (custom_bin)
        2. bin/telegram-bot-api.exe, telegram-bot-api.exe, bin/telegram-bot-api, telegram-bot-api
        3. telegram-bot-api в системном PATH через shutil.which
        """
        if self.custom_bin:
            custom = Path(self.custom_bin)
            if custom.is_file():
                return custom.resolve()
            if not custom.is_absolute():
                rel_custom = (Path(__file__).parent / custom).resolve()
                if rel_custom.is_file():
                    return rel_custom
            which_custom = shutil.which(self.custom_bin)
            if which_custom:
                return Path(which_custom).resolve()

        search_bases = [Path.cwd(), Path(__file__).parent]
        relative_names = [
            "bin/telegram-bot-api.exe",
            "telegram-bot-api.exe",
            "bin/telegram-bot-api",
            "telegram-bot-api",
        ]
        seen = set()
        for base in search_bases:
            for name in relative_names:
                cand = (base / name).resolve()
                if cand in seen:
                    continue
                seen.add(cand)
                if cand.is_file():
                    return cand

        which_bin = shutil.which("telegram-bot-api") or shutil.which("telegram-bot-api.exe")
        if which_bin:
            return Path(which_bin).resolve()

        return None

    async def check_health(self, url: Optional[str] = None, timeout: float = 1.0) -> bool:
        """
        Проверяет реальную готовность HTTP-сервера Bot API.
        Важно: сырой TCP-сокет не используется, так как docker-proxy на Windows
        открывает порт мгновенно, до готовности процесса telegram-bot-api внутри контейнера.
        """
        check_url = url or f"http://127.0.0.1:{self.port}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(check_url) as resp:
                    return resp.status < 500
        except Exception:
            return False
        return False

    async def _wait_until_ready(self, url: str, timeout: float = 25.0, poll_interval: float = 0.25) -> bool:
        """Опрашивает сервер до подтверждения готовности или истечения таймаута."""
        start = time.time()
        while time.time() - start < timeout:
            if self.process and self.process.returncode is not None:
                logger.warning(
                    f"Процесс telegram-bot-api завершился преждевременно с кодом {self.process.returncode}"
                )
                return False
            if await self.check_health(url, timeout=0.5):
                # Микропауза для полной стабилизации сокета в контейнере
                await asyncio.sleep(0.5)
                return True
            await asyncio.sleep(poll_interval)
        return False

    async def start(self) -> Optional[str]:
        """
        Запускает локальный Bot API сервер или переиспользует уже запущенный.
        Возвращает URL сервера (например http://127.0.0.1:8081) или None при ошибке/откате.
        """
        server_url = f"http://127.0.0.1:{self.port}"

        # 1. Проверяем, запущен ли сервер уже
        if await self.check_health(server_url, timeout=1.0):
            logger.info(f"Локальный Telegram Bot API Server уже доступен на {server_url}, переиспользуем.")
            self._is_external = True
            return server_url

        # 2. Проверка учетных данных
        if not self.api_id or not self.api_hash:
            logger.warning(
                "Локальный Telegram Bot API Server включен, но TELEGRAM_API_ID или TELEGRAM_API_HASH не заданы. "
                "Переход на стандартный Telegram Cloud API (лимит 50 МБ)."
            )
            return None

        # 3. Попытка запуска локального бинарного файла
        bin_path = self.find_binary()
        if bin_path:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                str(bin_path),
                "--local",
                f"--api-id={self.api_id}",
                f"--api-hash={self.api_hash}",
                f"--http-port={self.port}",
                f"--dir={self.data_dir.resolve()}",
            ]
            logger.info(f"Запуск бинарного файла telegram-bot-api ({bin_path}) на порту {self.port}...")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as e:
                logger.warning(f"Ошибка при запуске бинарного файла {bin_path}: {e}")
                self.process = None

        # 4. Если бинарный файл не найден, попытка запуска через Docker
        if not self.process:
            docker_bin = shutil.which("docker")
            if docker_bin:
                logger.info("Бинарный файл telegram-bot-api не найден, запуск через Docker...")
                self.data_dir.mkdir(parents=True, exist_ok=True)
                # Удаляем зависший контейнер с тем же именем при наличии
                try:
                    rm_proc = await asyncio.create_subprocess_exec(
                        "docker", "rm", "-f", self.container_name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(rm_proc.wait(), timeout=5.0)
                except Exception:
                    pass

                # Преобразуем путь для Docker volume (с прямыми слешами для совместимости с Windows)
                docker_volume = str(self.data_dir.resolve()).replace("\\", "/")
                docker_cmd = [
                    "docker", "run", "-d", "--rm",
                    "--name", self.container_name,
                    "-p", f"{self.port}:{self.port}",
                    "-v", f"{docker_volume}:/var/lib/telegram-bot-api",
                    "-e", f"TELEGRAM_API_ID={self.api_id}",
                    "-e", f"TELEGRAM_API_HASH={self.api_hash}",
                    "-e", "TELEGRAM_LOCAL=1",
                    "-e", f"TELEGRAM_HTTP_PORT={self.port}",
                    "aiogram/telegram-bot-api:latest",
                ]
                try:
                    dproc = await asyncio.create_subprocess_exec(
                        *docker_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await dproc.communicate()
                    if dproc.returncode == 0:
                        self.started_docker = True
                        logger.info(f"Docker контейнер {self.container_name} успешно запущен.")
                    else:
                        err_msg = stderr.decode(errors="replace").strip() if stderr else f"код {dproc.returncode}"
                        logger.warning(f"Ошибка запуска Docker контейнера: {err_msg}")
                except Exception as e:
                    logger.warning(f"Не удалось выполнить docker run: {e}")

        # 5. Если не удалось запустить ни бинарник, ни Docker
        if not self.process and not self.started_docker:
            logger.warning(
                "Не удалось запустить локальный Telegram Bot API Server: "
                "бинарный файл telegram-bot-api не найден и Docker недоступен. "
                "Переход на Telegram Cloud API."
            )
            return None

        # 6. Опрос готовности сервера
        ready = await self._wait_until_ready(server_url, timeout=25.0)
        if ready:
            logger.info(f"Локальный Telegram Bot API Server успешно запущен и готов: {server_url}")
            return server_url

        if self.process and self.process.returncode is not None:
            logger.warning(
                f"Локальный Telegram Bot API Server завершился с кодом ошибки {self.process.returncode}. "
                "Остановка и переход на Telegram Cloud API."
            )
        else:
            logger.warning(
                f"Локальный Telegram Bot API Server не ответил в течение 25 секунд на {server_url}. "
                "Остановка и переход на Telegram Cloud API."
            )
        await self.stop()
        return None

    async def stop(self):
        """Останавливает локальный сервер (процесс или Docker контейнер)."""
        if self.process:
            logger.info("Остановка процесса telegram-bot-api...")
            try:
                try:
                    self.process.terminate()
                except (ProcessLookupError, PermissionError):
                    pass

                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Процесс telegram-bot-api не ответил на SIGTERM, принудительное завершение (kill)...")
                    try:
                        self.process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=3.0)
                    except Exception:
                        pass
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as e:
                logger.warning(f"Ошибка при остановке процесса telegram-bot-api: {e}")
            finally:
                self.process = None

        if self.started_docker:
            logger.info(f"Остановка Docker контейнера {self.container_name}...")
            try:
                stop_proc = await asyncio.create_subprocess_exec(
                    "docker", "stop", "-t", "2", self.container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(stop_proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Docker stop превысил таймаут для {self.container_name}, принудительное удаление (rm -f)...")
                    rm_force = await asyncio.create_subprocess_exec(
                        "docker", "rm", "-f", self.container_name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(rm_force.wait(), timeout=5.0)
            except Exception as e:
                logger.warning(f"Ошибка при остановке Docker контейнера {self.container_name}: {e}")
            finally:
                self.started_docker = False


class SingleInstanceLock:
    """
    Гарантирует запуск только одного экземпляра Telegram-бота.
    Предотвращает конфликт параллельных getUpdates (TelegramConflictError).
    При обнаружении работающего предыдущего процесса завершает его или отклоняет запуск.
    """

    def __init__(
        self,
        lock_path: Optional[Path] = None,
        pid_path: Optional[Path] = None,
    ):
        self.lock_path = lock_path or (config.DOWNLOAD_DIR / "bot.lock")
        self.pid_path = pid_path or (config.DOWNLOAD_DIR / "bot.pid")
        self._fd = None
        self._is_locked = False

    def acquire(self, auto_terminate_stale: bool = True) -> bool:
        """
        Захватывает системную эксклюзивную файловую блокировку.
        Если блокировка занята другим процессом:
        - при auto_terminate_stale=True пытается найти и завершить старый процесс, после чего повторить захват.
        - иначе возвращает False.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        max_attempts = 2 if auto_terminate_stale else 1

        for attempt in range(max_attempts):
            try:
                self._fd = open(self.lock_path, "a+b")
                self._fd.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    # Записываем начальный байт, если файл пуст, для корректной работы msvcrt.locking
                    if not self.lock_path.exists() or self.lock_path.stat().st_size == 0:
                        self._fd.write(b"0")
                        self._fd.flush()
                        self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                self._is_locked = True
                try:
                    self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
                except Exception:
                    pass
                return True
            except (OSError, IOError):
                if self._fd:
                    try:
                        self._fd.close()
                    except Exception:
                        pass
                    self._fd = None

                stale_pids = self._get_stale_pids()
                if auto_terminate_stale and attempt == 0:
                    terminated_any = False
                    for pid in stale_pids:
                        if pid != os.getpid():
                            logger.warning(
                                f"Обнаружен предыдущий работающий экземпляр бота (PID: {pid}). "
                                f"Завершаем старый процесс перед запуском..."
                            )
                            self._terminate_pid(pid)
                            terminated_any = True
                    if terminated_any:
                        time.sleep(1.0)
                        continue

                return False
        return False

    def _get_stale_pids(self) -> list[int]:
        pids = []
        if self.pid_path.exists():
            try:
                content = self.pid_path.read_text(encoding="utf-8").strip()
                if content.isdigit():
                    pids.append(int(content))
            except Exception:
                pass

        # Если в pid_path ничего нет, на Windows ищем зависшие python-процессы с bot.py
        if not pids and sys.platform == "win32":
            try:
                ps_script = (
                    f"Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
                    f"Where-Object {{ $_.ProcessId -ne {os.getpid()} -and $_.CommandLine -like '*bot.py*' }} | "
                    f"Select-Object -ExpandProperty ProcessId"
                )
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
            except Exception:
                pass
        return pids

    def _terminate_pid(self, pid: int):
        """Принудительно останавливает процесс по указанному PID."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"Не удалось остановить процесс {pid}: {e}")

    def release(self):
        """Освобождает блокировку и удаляет PID-файл."""
        if self._is_locked and self._fd:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fd.seek(0)
                    try:
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._is_locked = False

        if self.pid_path.exists():
            try:
                content = self.pid_path.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    self.pid_path.unlink(missing_ok=True)
            except Exception:
                pass


def create_bot(token: str, api_server_url: Optional[str] = None) -> Bot:
    """Создает экземпляр Bot с поддержкой кастомного Local Bot API Server при наличии."""
    if api_server_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api_server_url, is_local=True))
        return Bot(token=token, session=session)
    return Bot(token=token)


def build_metadata_card(meta: Dict[str, Any]) -> str:
    """Формирует текстовую карточку метаданных трека."""
    artist = escape_html(meta.get("artist") or "Unknown Artist")
    title = escape_html(meta.get("title") or "Unknown Track")
    album = escape_html(meta.get("album") or "—")
    year = escape_html(str(meta.get("year") or "—"))
    duration = format_duration(meta.get("duration"))
    explicit = " [E]" if meta.get("explicit") else ""

    card = (
        f"🎵 <b>{artist} — {title}</b>{explicit}\n\n"
        f"💿 <b>Альбом:</b> {album}\n"
        f"📅 <b>Год:</b> {year}\n"
        f"⏱ <b>Длительность:</b> {duration}"
    )
    return card


def build_quality_keyboard(qid: str) -> InlineKeyboardMarkup:
    """Создает кнопки выбора качества скачивания."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎵 MP3 320k", callback_data=f"dl:{qid}:MP3"),
                InlineKeyboardButton(text="💿 FLAC Lossless", callback_data=f"dl:{qid}:FLAC"),
            ]
        ]
    )


def build_quality_settings_keyboard(current_pref: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру настройки качества по умолчанию."""
    mp3_mark = " ✓" if current_pref == "MP3" else ""
    flac_mark = " ✓" if current_pref == "FLAC" else ""
    ask_mark = " ✓" if current_pref == "ASK" else ""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🎵 MP3 320k{mp3_mark}", callback_data="pref:MP3"),
                InlineKeyboardButton(text=f"💿 FLAC Lossless{flac_mark}", callback_data="pref:FLAC"),
            ],
            [
                InlineKeyboardButton(text=f"❓ Спрашивать каждый раз{ask_mark}", callback_data="pref:ASK"),
            ],
        ]
    )


# Инициализация глобальных хранилищ
user_settings = UserSettings(config.DOWNLOAD_DIR / ".bot_user_settings.json")
query_store = QueryStore(max_items=1000)
active_download_tasks: Set[asyncio.Task] = set()
router = Router()


@router.message(CommandStart())
async def handle_start(message: Message):
    """Приветственное сообщение и краткая инструкция."""
    welcome_text = (
        "👋 <b>Добро пожаловать в Music Downloader Bot!</b>\n\n"
        "Отправьте мне <b>ссылку на трек</b> или <b>название трека</b> (Артист — Название), "
        "и я скачаю его в лучшем качестве и пришлю аудиофайлом прямо в чат!\n\n"
        "<b>Поддерживаемые ссылки:</b>\n"
        "• Spotify, Apple Music, Deezer\n"
        "• YouTube Music, SoundCloud, Яндекс.Музыка\n"
        "• Текстовые поисковые запросы\n\n"
        "<b>Команды:</b>\n"
        "• /quality — настроить качество по умолчанию (MP3 320k / FLAC)\n"
        "• /help — подробная справка\n"
        "• /id — узнать ваш Telegram ID"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def handle_help(message: Message):
    """Подробная справка по боту."""
    help_text = (
        "📖 <b>Справка по использованию:</b>\n\n"
        "1. <b>Как скачать трек?</b>\n"
        "Просто отправьте боту ссылку на трек из любого поддерживаемого стриминга "
        "или напишите название (например: <code>Queen Bohemian Rhapsody</code>).\n\n"
        "2. <b>Выбор качества:</b>\n"
        "• <b>MP3 320k</b>: честный 320 kbps CBR без пережатия (отлично подходит для телефонов, размер ~8-15 МБ).\n"
        "• <b>FLAC Lossless</b>: несжатый студийный Lossless из Soulseek P2P или Deezer HiFi.\n\n"
        "3. <b>Настройка качества по умолчанию:</b>\n"
        "Команда /quality позволяет выбрать, спрашивать ли качество каждый раз или скачивать автоматически.\n\n"
        "4. <b>Ограничения по размеру файлов:</b>\n"
        "Стандартный лимит отправки через Telegram — 50 МБ. Если FLAC трек превышает этот лимит, "
        "бот предложит скачать его в MP3 или подключить локальный Bot API Server."
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)


@router.message(Command("id"))
async def handle_id(message: Message):
    """Показывает Telegram ID пользователя."""
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(f"Ваш Telegram ID: <code>{user_id}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("quality"))
async def handle_quality_command(message: Message):
    """Отображает меню настройки качества по умолчанию."""
    user_id = message.from_user.id if message.from_user else 0
    pref = user_settings.get_quality(user_id)

    pref_names = {
        "ASK": "❓ Спрашивать каждый раз",
        "MP3": "🎵 MP3 320k (автоматически)",
        "FLAC": "💿 FLAC Lossless (автоматически)",
    }

    text = (
        "⚙️ <b>Настройка качества по умолчанию</b>\n\n"
        f"Текущий режим: <b>{pref_names.get(pref, '❓ Спрашивать каждый раз')}</b>\n\n"
        "Выберите желаемый режим для новых запросов:"
    )
    await message.answer(text, reply_markup=build_quality_settings_keyboard(pref), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("pref:"))
async def handle_preference_callback(callback: CallbackQuery):
    """Обрабатывает выбор настройки качества по умолчанию."""
    from_user = getattr(callback, "from_user", None)
    user_id = from_user.id if from_user else 0
    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Неверный формат запроса.", show_alert=True)
        return
    new_pref = parts[1]
    if new_pref not in ["ASK", "MP3", "FLAC"]:
        await callback.answer("Неверное значение качества.", show_alert=True)
        return

    user_settings.set_quality(user_id, new_pref)

    pref_names = {
        "ASK": "❓ Спрашивать каждый раз",
        "MP3": "🎵 MP3 320k",
        "FLAC": "💿 FLAC Lossless",
    }
    await callback.answer(f"Установлено: {pref_names.get(new_pref, new_pref)}")

    text = (
        "⚙️ <b>Настройка качества по умолчанию</b>\n\n"
        f"Текущий режим: <b>{pref_names.get(new_pref, '❓ Спрашивать каждый раз')}</b>\n\n"
        "Настройки успешно сохранены!"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_quality_settings_keyboard(new_pref),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def run_download_and_send(
    bot: Bot,
    chat_id: int,
    query_or_url: str,
    target_quality: str,
    meta: Dict[str, Any],
    status_msg: Message,
):
    """
    Фоновый рабочий процесс скачивания трека и отправки в Telegram.
    Выполняется в asyncio.create_task параллельно с другими задачами.
    """
    artist = meta.get("artist") or "Unknown Artist"
    title = meta.get("title") or "Unknown Track"
    album = meta.get("album")
    year = meta.get("year")
    duration = meta.get("duration")
    album_art_url = meta.get("album_art")

    task_dir = config.BOT_TEMP_DIR / f"dl_{uuid.uuid4().hex}"
    task_dir.mkdir(parents=True, exist_ok=True)

    header = f"⏳ <b>{escape_html(artist)} — {escape_html(title)}</b> [{target_quality}]"
    notifier = StatusNotifier(bot, status_msg, header=header)

    try:
        # 1. Запуск скачивания в отдельном потоке
        file_path = await asyncio.to_thread(
            core.download_track_by_link,
            url_or_query=query_or_url,
            target_quality=target_quality,
            dest_dir=task_dir,
            status_callback=notifier.notify,
        )

        if not file_path or not file_path.exists():
            notifier.close()
            await status_msg.edit_text(
                f"❌ <b>{escape_html(artist)} — {escape_html(title)}</b>\n\n"
                "Не удалось скачать трек ни с одного источника. Попробуйте другой источник или поиск.",
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
                f"⚠️ <b>Файл слишком большой ({size_mb:.1f} МБ)</b>\n\n"
                "Стандартный Telegram Cloud Bot API ограничивает размер отправляемых файлов до <b>50 МБ</b>.\n\n"
                "💡 <b>Рекомендации:</b>\n"
                "• Скачайте трек в формате <b>[🎵 MP3 320k]</b> (размер будет в 3-4 раза меньше)\n"
                "• Или настройте локальный Telegram Bot API Server (параметры <code>TELEGRAM_API_ID</code> и <code>TELEGRAM_API_HASH</code> в <code>.env</code>) для отправки файлов до 2 ГБ.",
                parse_mode=ParseMode.HTML,
            )
            return

        if has_local_api and file_size > MAX_LOCAL_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            notifier.close()
            await status_msg.edit_text(
                f"❌ <b>Файл превышает лимит сервера в 2 ГБ ({size_mb:.1f} МБ)</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        # 3. Подготовка миниатюры обложки
        thumb_path = prepare_thumbnail(file_path, album_art_url, task_dir)
        thumb_input = FSInputFile(str(thumb_path)) if thumb_path and thumb_path.exists() else None

        # 4. Обновление статуса перед отправкой
        await notifier.update_now("🚀 Отправка аудио в Telegram...")
        notifier.close()

        caption_lines = [f"🎵 <b>{escape_html(artist)} — {escape_html(title)}</b>"]
        if album:
            caption_lines.append(f"💿 <i>{escape_html(album)}</i>")
        if year:
            caption_lines.append(f"📅 {escape_html(str(year))}")
        caption = "\n".join(caption_lines)

        # 5. Отправка нативного аудио в Telegram
        clean_title = re.sub(r'[\\/*?:"<>|]', "_", f"{artist} - {title}").strip()
        clean_filename = f"{clean_title}{file_path.suffix}"
        audio_input = FSInputFile(str(file_path), filename=clean_filename)
        dur_sec = int(round(duration)) if duration and duration > 0 else None

        try:
            await bot.send_audio(
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
            await bot.send_audio(
                chat_id=chat_id,
                audio=audio_input,
                performer=artist,
                title=title,
                duration=dur_sec,
                caption=caption,
                parse_mode=ParseMode.HTML,
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
                f"❌ <b>Ошибка:</b> {escape_html(str(e))}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        notifier.close()
        # Гарантированная очистка временной папки этого скачивания
        shutil.rmtree(task_dir, ignore_errors=True)


@router.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery, bot: Bot):
    """Обрабатывает нажатие инлайн-кнопок скачивания [MP3 320k] / [FLAC Lossless]."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неверный формат запроса.", show_alert=True)
        return

    _, qid, quality = parts
    if quality not in ["MP3", "FLAC"]:
        await callback.answer("Неверное качество скачивания.", show_alert=True)
        return

    query_data = await query_store.get(qid)

    if not query_data:
        await callback.answer(
            "⚠️ Срок действия этой кнопки истёк. Пожалуйста, отправьте ссылку на трек заново.",
            show_alert=True,
        )
        return

    await callback.answer(f"Запуск скачивания ({quality})...")

    # Убираем клавиатуру у карточки, чтобы предотвратить повторные случайные клики
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    url_or_query = query_data["url_or_query"]
    meta = query_data["meta"]
    artist = meta.get("artist") or "Unknown Artist"
    title = meta.get("title") or "Unknown Track"

    # Создаем сообщение со статусом процесса
    status_msg = await bot.send_message(
        chat_id=callback.message.chat.id,
        text=f"⏳ <b>{escape_html(artist)} — {escape_html(title)}</b> [{quality}]\n\n<i>Поиск источников...</i>",
        parse_mode=ParseMode.HTML,
    )

    # Запуск параллельной асинхронной задачи скачивания
    task = asyncio.create_task(
        run_download_and_send(
            bot=bot,
            chat_id=callback.message.chat.id,
            query_or_url=url_or_query,
            target_quality=quality,
            meta=meta,
            status_msg=status_msg,
        )
    )
    active_download_tasks.add(task)
    task.add_done_callback(active_download_tasks.discard)


@router.message(F.text)
async def handle_track_query(message: Message, bot: Bot):
    """
    Обрабатывает входящие текстовые сообщения (ссылки или названия треков).
    Извлекает метаданные, отправляет карточку трека и кнопки выбора качества.
    """
    query_text = message.text.strip()
    if not query_text:
        return

    if query_text.startswith("/"):
        await message.answer(
            "❓ Неизвестная команда. Введите /help для просмотра списка команд.",
            parse_mode=ParseMode.HTML,
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    pref = user_settings.get_quality(user_id)

    # Первичное уведомление о поиске метаданных
    searching_msg = await message.answer("🔍 <i>Поиск метаданных трека...</i>", parse_mode=ParseMode.HTML)

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
            "❌ <b>Трек не найден</b>\n\n"
            "Не удалось извлечь информацию о треке по вашему запросу. "
            "Попробуйте уточнить название (например: <code>Queen — Bohemian Rhapsody</code>) "
            "или отправить прямую ссылку со Spotify / Apple Music / Deezer / YouTube.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Сохраняем в кэш запросов
    qid = uuid.uuid4().hex[:12]
    await query_store.put(qid, {"url_or_query": query_text, "meta": meta})

    card_text = build_metadata_card(meta)
    art_url = meta.get("album_art")

    # Удаляем временное сообщение о поиске
    try:
        await searching_msg.delete()
    except Exception:
        pass

    # Если у пользователя настроено авто-скачивание в MP3 или FLAC
    if pref in ["MP3", "FLAC"]:
        status_msg = await message.answer(
            f"⚡ <b>Авто-скачивание [{pref}]</b>\n\n"
            f"{card_text}\n\n"
            f"<i>Запуск процесса скачивания...</i>",
            reply_markup=build_quality_keyboard(qid),
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
            )
        )
        active_download_tasks.add(task)
        task.add_done_callback(active_download_tasks.discard)
        return

    # Стандартный режим (спрашивать каждый раз): карточка трека с кнопками качества
    keyboard = build_quality_keyboard(qid)

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
        "💡 Пожалуйста, отправьте ссылку на трек или его название текстом.",
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
            api_manager = LocalBotAPIManager()
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

        if bot_user is None and api_manager:
            logger.warning(
                "Локальный Telegram Bot API Server не ответил на getMe. "
                "Остановка и переключение на стандартный Telegram Cloud API..."
            )
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
