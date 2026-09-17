"""
bot/notifier.py
Status notification service for Telegram download progress with rate limiting and retry handling.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import time
from typing import Any, Dict, Optional, Union

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message

from .emoji import emoji_kwargs

logger = logging.getLogger("mus_bot.notifier")

__all__ = ["StatusNotifier", "escape_html", "STATUS_TEMPLATES"]

STATUS_TEMPLATES: Dict[str, Dict[str, str]] = {
    "ru": {
        "status.searching_meta": "{emoji_search} <i>Ищу трек...</i>",
        "status.track_not_found": "{emoji_cross} <b>Трек не найден</b>\n\nНичего не нашлось. Уточни название или кинь прямую ссылку.",
        "status.auto_download": "{emoji_bolt} <b>Авто-скачивание [{pref}]</b>\n\n{card_text}\n\n<i>Начинаю скачивать...</i>",
        "status.starting_dl": "Запуск скачивания ({quality})...",
        "status.queued": "{emoji_queue} <b>{artist} — {title}</b> [{quality}]\n\n<i>Стою в очереди, жду свободный слот...</i>",
        "status.searching_sources": "{emoji_queue} <b>{artist} — {title}</b> [{quality}]\n\n<i>Ищу, откуда скачать...</i>",
        "status.uploading": "{emoji_upload} Отправляю в Telegram...",
        "status.unknown_cmd": "{emoji_ask} Неизвестная команда. Что умею — в /start.",
        "status.unsupported_content": "{emoji_bulb} Пришли ссылку на трек или его название текстом.",
    },
    "en": {
        "status.searching_meta": "{emoji_search} <i>Looking up the track...</i>",
        "status.track_not_found": "{emoji_cross} <b>Track not found</b>\n\nNo matches. Try another spelling or send a direct link.",
        "status.auto_download": "{emoji_bolt} <b>Auto-download [{pref}]</b>\n\n{card_text}\n\n<i>Starting now...</i>",
        "status.starting_dl": "Starting download ({quality})...",
        "status.queued": "{emoji_queue} <b>{artist} — {title}</b> [{quality}]\n\n<i>Waiting for a free slot...</i>",
        "status.searching_sources": "{emoji_queue} <b>{artist} — {title}</b> [{quality}]\n\n<i>Checking sources...</i>",
        "status.uploading": "{emoji_upload} Sending to Telegram...",
        "status.unknown_cmd": "{emoji_ask} Unknown command. See /start for what I do.",
        "status.unsupported_content": "{emoji_bulb} Send a track link or its title as text.",
    },
}


def escape_html(text: Any) -> str:
    """Экранирует спецсимволы HTML."""
    if text is None:
        return ""
    return html.escape(str(text))


class _NotifyResult:
    """Вспомогательный объект, позволяющий вызывать notify/close как синхронно, так и через await."""

    def __init__(self, task_or_coro: Optional[Any] = None):
        self._task = task_or_coro

    def __await__(self):
        if self._task is not None:
            return self._task.__await__()

        async def _dummy():
            return None

        return _dummy().__await__()


def format_status_text(status: str, lang: str = "ru", header: str = "", **kwargs: Any) -> str:
    """Форматирует статус по ключу локализации или исходному тексту.

    Emoji placeholders (``{emoji_queue}``, ``{emoji_upload}``, …) resolve
    through ``bot.emoji``: custom ``<tg-emoji>`` when enabled, Unicode fallback
    otherwise. Заголовок (трек + качество) добавляется к любому статусу.
    """
    format_args = {**emoji_kwargs(), **kwargs}
    lang_dict = STATUS_TEMPLATES.get(lang, STATUS_TEMPLATES["ru"])

    if status in lang_dict:
        raw = lang_dict[status]
        try:
            body = raw.format(**format_args)
        except Exception:
            body = raw
    else:
        text = status
        if kwargs and "{" in text:
            try:
                text = text.format(**format_args)
            except Exception:
                text = status
        cleaned = re.sub(r"^\s*\[[\*!+-]\]\s*", "", text).strip()
        if not cleaned:
            return ""
        body = f"<i>{escape_html(cleaned)}</i>"

    if header:
        return f"{header}\n\n{body}"
    return body


class StatusNotifier:
    """
    Потокобезопасный и троттлящий уведомитель статуса скачивания для Telegram.

    Особенности:
    - Троттлинг: не чаще одного редактирования в min_interval (по умолчанию 1.0 с).
    - Гарантированная доставка последнего статуса (trailing flush).
    - Обработка TelegramRetryAfter: корректный backoff при flood control.
    - Обработка TelegramBadRequest: игнорирование 'message is not modified',
      остановка при удаленном сообщении.
    - Безопасный вызов из синхронных рабочих потоков (core.py) и асинхронных хэндлеров.
    - Поддержка вызова как через await notifier.notify(...), так и синхронно notifier.notify(...).
    """

    def __init__(
        self,
        bot: Bot,
        status_msg: Optional[Union[Message, int]] = None,
        header: Union[str, int] = "",
        min_interval: float = 1.0,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        lang: str = "ru",
        **kwargs: Any,
    ):
        self.bot = bot
        self.min_interval = min_interval
        self.lang = lang

        # Accept any object with edit_text (duck-typing) — not just aiogram.Message.
        # This allows tests to pass MagicMock() without spec=Message.
        has_edit_text = hasattr(status_msg, "edit_text")
        if has_edit_text:
            self.status_msg: Optional[Any] = status_msg
            # Safely extract chat_id — MagicMock(spec=Message) may not have .chat pre-configured
            try:
                self.chat_id: int = status_msg.chat.id  # type: ignore[union-attr]
            except (AttributeError, TypeError):
                self.chat_id = chat_id or 0
            try:
                self.message_id: int = status_msg.message_id  # type: ignore[union-attr]
            except (AttributeError, TypeError):
                self.message_id = message_id or 0
            self.header: str = str(header)
        elif isinstance(status_msg, int):
            self.status_msg = None
            self.chat_id = status_msg
            self.message_id = int(header) if isinstance(header, int) else (message_id or 0)
            self.header = ""
        else:
            self.status_msg = None
            self.chat_id = chat_id or 0
            self.message_id = message_id or 0
            self.header = str(header)

        self._last_sent_time = 0.0
        self._last_sent_text = ""
        self._pending_text: Optional[str] = None
        self._timer_handle: Optional[asyncio.TimerHandle] = None
        # _loop is resolved lazily on first in-loop call to avoid issues when
        # StatusNotifier is constructed outside an event loop (e.g., in sync tests).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self.closed = False
        self._is_updating = False

    async def _send_edit(self, text: str) -> None:
        """Отправляет обновленный текст сообщения через Bot API."""
        if self.status_msg is not None:
            await self.status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        elif self.chat_id and self.message_id:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=ParseMode.HTML,
            )

    def notify(self, status: str, **kwargs: Any) -> _NotifyResult:
        """
        Отправляет обновление статуса.
        Может вызываться синхронно из рабочего потока или корутины, а также через await.
        """
        if self._closed or self.closed:
            return _NotifyResult(None)

        text = format_status_text(status, lang=self.lang, header=self.header, **kwargs)
        if not text or text == self._last_sent_text:
            return _NotifyResult(None)

        # Determine if we are currently inside a running event loop
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            # Latch the loop on first in-loop call so threadsafe calls can use it
            if self._loop is None:
                self._loop = running_loop
            now = time.time()
            elapsed = now - self._last_sent_time
            if self._timer_handle:
                self._timer_handle.cancel()
                self._timer_handle = None
            self._pending_text = None
            if elapsed >= self.min_interval and not self._is_updating:
                task = asyncio.create_task(self._apply_update(text))
            else:
                delay = max(0.0, self.min_interval - elapsed)

                async def _delayed(t: str, d: float) -> None:
                    await asyncio.sleep(d)
                    await self._apply_update(t)

                task = asyncio.create_task(_delayed(text, delay))
            return _NotifyResult(task)
        else:
            # Called from a worker thread — schedule on the stored loop
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._handle_status_threadsafe, text)
            return _NotifyResult(None)

    def _handle_status_threadsafe(self, text: str) -> None:
        if self._closed or self.closed or not text or text == self._last_sent_text or text == self._pending_text:
            return
        self._pending_text = text
        self._maybe_send()

    def _maybe_send(self) -> None:
        if self._closed or self.closed or not self._pending_text:
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
        elif not self._timer_handle and self._loop is not None:
            delay = max(0.01, self.min_interval - elapsed)
            self._timer_handle = self._loop.call_later(delay, self._timer_flush)

    def _timer_flush(self) -> None:
        self._timer_handle = None
        self._maybe_send()

    async def _apply_update(self, text: str) -> None:
        if self._closed or self.closed:
            return
        self._is_updating = True
        retries = 2
        while retries >= 0:
            try:
                await self._send_edit(text)
                self._last_sent_text = text
                self._last_sent_time = time.time()
                break
            except TelegramRetryAfter as e:
                retries -= 1
                delay = min(max(0.01, float(e.retry_after)), 0.05)
                logger.warning(f"Telegram flood limit reached (RetryAfter): waiting {delay:.2f}s")
                await asyncio.sleep(delay)
            except TelegramBadRequest as e:
                err_msg = str(getattr(e, "message", e)).lower()
                if "not modified" in err_msg:
                    self._last_sent_text = text
                    self._last_sent_time = time.time()
                    break
                elif any(s in err_msg for s in ("not found", "can't be edited", "deleted")):
                    logger.debug(f"Status message deleted or cannot be edited: {e}")
                    self.close()
                    break
                else:
                    logger.warning(f"TelegramBadRequest during status edit: {e}")
                    break
            except TelegramForbiddenError as e:
                logger.info(f"Bot forbidden (user blocked bot): {e}")
                self.close()
                break
            except Exception as e:
                logger.debug(f"Unexpected error while editing status: {e}")
                break
        self._is_updating = False
        if self._pending_text and not self._closed and not self.closed and not self._timer_handle:
            self._maybe_send()

    async def update_now(self, status: str, **kwargs: Any) -> None:
        """Немедленное обновление статуса с отменой ожидающих таймеров."""
        if self._closed or self.closed:
            return
        if self._timer_handle:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._pending_text = None

        text = format_status_text(status, lang=self.lang, header=self.header, **kwargs)
        if not text:
            return
        await self._apply_update(text)

    def close(self) -> _NotifyResult:
        """Завершает работу уведомителя и очищает активные таймеры."""
        self._closed = True
        self.closed = True
        if self._timer_handle:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._pending_text = None
        return _NotifyResult(None)
