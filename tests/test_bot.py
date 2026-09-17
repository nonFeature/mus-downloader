"""
Unit tests for Telegram bot wrapper (bot.py).
Tests cover:
- Whitelist authorization middleware (access control)
- Storage management and temp directory cleanup
- User preferences (UserSettings) and QueryStore caching
- Command and callback parsing
- File size handling and local Bot API vs cloud limits
- UI helpers and metadata cards
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message, User, Chat

import bot
import config


# =====================================================================
# 1. Access Control (Whitelist & Parsing)
# =====================================================================

def test_parse_allowed_users():
    """Тестирует парсинг списка разрешенных пользователей."""
    assert config._parse_allowed_users("") == set()
    assert config._parse_allowed_users("   ") == set()
    assert config._parse_allowed_users("123, 456, 789") == {123, 456, 789}
    assert config._parse_allowed_users("123, abc, 456, , -100") == {123, 456, -100}


def test_is_user_allowed():
    """Тестирует функцию проверки доступа пользователя."""
    allowed = {111, 222, 333}
    assert config.is_user_allowed(111, allowed) is True
    assert config.is_user_allowed(999, allowed) is False
    assert config.is_user_allowed(None, allowed) is False
    assert config.is_user_allowed(111, set()) is False


def test_whitelist_middleware_allowed_user():
    """Разрешенный пользователь успешно проходит через middleware."""
    async def run():
        middleware = bot.WhitelistMiddleware(allowed_users={12345})
        mock_handler = AsyncMock(return_value="handler_ok")
        mock_user = MagicMock(spec=User)
        mock_user.id = 12345
        mock_event = MagicMock(spec=Message)

        data = {"event_from_user": mock_user}
        result = await middleware(mock_handler, mock_event, data)

        assert result == "handler_ok"
        mock_handler.assert_awaited_once_with(mock_event, data)

    asyncio.run(run())


def test_whitelist_middleware_rejected_user():
    """Неавторизованный пользователь молча игнорируется без ответа."""
    async def run():
        middleware = bot.WhitelistMiddleware(allowed_users={12345})
        mock_handler = AsyncMock()
        mock_user = MagicMock(spec=User)
        mock_user.id = 99999
        mock_event = MagicMock(spec=Message)
        mock_event.answer = AsyncMock()

        data = {"event_from_user": mock_user}
        result = await middleware(mock_handler, mock_event, data)

        assert result is None
        mock_handler.assert_not_awaited()
        mock_event.answer.assert_not_awaited()

    asyncio.run(run())


def test_whitelist_middleware_empty_whitelist_rejects():
    """При пустом ALLOWED_USERS запросы молча игнорируются."""
    async def run():
        middleware = bot.WhitelistMiddleware(allowed_users=set())
        mock_handler = AsyncMock()
        mock_user = MagicMock(spec=User)
        mock_user.id = 12345
        mock_event = MagicMock(spec=Message)
        mock_event.answer = AsyncMock()

        data = {"event_from_user": mock_user}
        result = await middleware(mock_handler, mock_event, data)

        assert result is None
        mock_handler.assert_not_awaited()
        mock_event.answer.assert_not_awaited()

    asyncio.run(run())


def test_whitelist_middleware_callback_rejected():
    """Неавторизованный клик на кнопку молча игнорируется."""
    async def run():
        middleware = bot.WhitelistMiddleware(allowed_users={12345})
        mock_handler = AsyncMock()
        mock_user = MagicMock(spec=User)
        mock_user.id = 77777
        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.answer = AsyncMock()

        data = {"event_from_user": mock_user}
        result = await middleware(mock_handler, mock_callback, data)

        assert result is None
        mock_handler.assert_not_awaited()
        mock_callback.answer.assert_not_awaited()

    asyncio.run(run())


# =====================================================================
# 2. Storage Management & Temp Cleanup
# =====================================================================

def test_cleanup_temp_dir_creates_if_not_exists(tmp_path):
    """cleanup_temp_dir создает директорию, если она отсутствует."""
    target_dir = tmp_path / "non_existing_dir"
    assert not target_dir.exists()
    removed = bot.cleanup_temp_dir(target_dir)
    assert removed == 0
    assert target_dir.exists()


def test_cleanup_temp_dir_removes_files_and_folders(tmp_path):
    """cleanup_temp_dir удаляет файлы и подкаталоги и возвращает их количество."""
    target_dir = tmp_path / "temp_bot"
    target_dir.mkdir()

    (target_dir / "file1.mp3").write_text("dummy")
    (target_dir / "file2.flac").write_text("dummy")
    sub = target_dir / "task_1"
    sub.mkdir()
    (sub / "nested.tmp").write_text("dummy")

    assert len(list(target_dir.iterdir())) == 3
    removed = bot.cleanup_temp_dir(target_dir)
    assert removed == 3
    assert len(list(target_dir.iterdir())) == 0


# =====================================================================
# 3. UserSettings & QueryStore
# =====================================================================

def test_user_settings_persistence(tmp_path):
    """Тестирует сохранение и чтение пользовательских настроек качества."""
    settings_file = tmp_path / ".user_settings.json"
    store = bot.UserSettings(settings_file)

    assert store.get_quality(100) == "ASK"

    store.set_quality(100, "MP3")
    assert store.get_quality(100) == "MP3"

    store.set_quality(200, "FLAC")
    assert store.get_quality(200) == "FLAC"

    # Недопустимое значение не сохраняется
    store.set_quality(100, "INVALID_QUALITY")
    assert store.get_quality(100) == "MP3"

    # Создаем новый экземпляр, читающий тот же файл
    store_reloaded = bot.UserSettings(settings_file)
    assert store_reloaded.get_quality(100) == "MP3"
    assert store_reloaded.get_quality(200) == "FLAC"


def test_query_store_put_and_get():
    """Тестирует кэш запросов QueryStore с проверкой TTL и вместимости."""
    async def run():
        store = bot.QueryStore(max_items=3, ttl_seconds=1.0)
        await store.put("q1", {"data": 1})
        await store.put("q2", {"data": 2})

        res1 = await store.get("q1")
        assert res1 is not None and res1["data"] == 1

        # Переполнение лимита (max_items=3)
        await store.put("q3", {"data": 3})
        await store.put("q4", {"data": 4})
        # q1 должен быть вытеснен как самый старый
        assert await store.get("q1") is None
        assert await store.get("q4") is not None

        # Проверка истечения TTL
        short_store = bot.QueryStore(max_items=10, ttl_seconds=0.05)
        await short_store.put("expire_me", {"data": "old"})
        await asyncio.sleep(0.06)
        assert await short_store.get("expire_me") is None

    asyncio.run(run())


# =====================================================================
# 4. Helpers and Formatters
# =====================================================================

def test_format_duration():
    """Тестирует форматирование секунд в строку MM:SS."""
    assert bot.format_duration(None) == "Неизвестно"
    assert bot.format_duration(0) == "Неизвестно"
    assert bot.format_duration(-5) == "Неизвестно"
    assert bot.format_duration(65) == "1:05"
    assert bot.format_duration(214) == "3:34"
    assert bot.format_duration(3605) == "60:05"


def test_escape_html():
    """Тестирует экранирование HTML."""
    assert bot.escape_html("<script>") == "&lt;script&gt;"
    assert bot.escape_html("Rock & Roll") == "Rock &amp; Roll"
    assert bot.escape_html(None) == ""


def test_build_metadata_card():
    """Тестирует формирование карточки метаданных трека."""
    meta = {
        "artist": "Daft Punk",
        "title": "Get Lucky",
        "album": "Random Access Memories",
        "year": "2013",
        "duration": 248,
        "explicit": True
    }
    card = bot.build_metadata_card(meta)
    assert "Daft Punk — Get Lucky" in card
    assert "[E]" in card
    assert "Random Access Memories" in card
    assert "2013" in card
    assert "4:08" in card


def test_build_quality_keyboard():
    """Тестирует генерацию кнопок выбора качества."""
    kb = bot.build_quality_keyboard("abc123")
    buttons = kb.inline_keyboard[0]
    assert len(buttons) == 2
    assert buttons[0].text == "🎵 MP3 320k"
    assert buttons[0].callback_data == "dl:abc123:MP3"
    assert buttons[1].text == "💿 FLAC Lossless"
    assert buttons[1].callback_data == "dl:abc123:FLAC"


# =====================================================================
# 5. Commands and Callback Handlers
# =====================================================================

def test_handle_start_command():
    """Команда /start отправляет приветственное сообщение."""
    async def run():
        mock_msg = MagicMock(spec=Message)
        mock_msg.answer = AsyncMock()

        await bot.handle_start(mock_msg)
        mock_msg.answer.assert_awaited_once()
        text = mock_msg.answer.call_args[0][0]
        assert "Music Downloader Bot" in text
        assert "/quality" in text

    asyncio.run(run())


def test_handle_quality_command(tmp_path):
    """Команда /quality показывает меню выбора качества."""
    async def run():
        test_settings = bot.UserSettings(tmp_path / "settings.json")
        test_settings.set_quality(555, "FLAC")

        with patch("bot.user_settings", test_settings):
            mock_msg = MagicMock(spec=Message)
            mock_msg.from_user = MagicMock()
            mock_msg.from_user.id = 555
            mock_msg.answer = AsyncMock()

            await bot.handle_quality_command(mock_msg)

            mock_msg.answer.assert_awaited_once()
            call_args = mock_msg.answer.call_args
            assert "Настройка качества по умолчанию" in call_args[0][0]
            assert "FLAC Lossless" in call_args[0][0]

    asyncio.run(run())


def test_handle_preference_callback(tmp_path):
    """Клик по кнопке качества обновляет настройку пользователя."""
    async def run():
        test_settings = bot.UserSettings(tmp_path / "settings.json")

        with patch("bot.user_settings", test_settings):
            mock_callback = MagicMock(spec=CallbackQuery)
            mock_callback.from_user = MagicMock()
            mock_callback.from_user.id = 123
            mock_callback.data = "pref:MP3"
            mock_callback.answer = AsyncMock()
            mock_callback.message = MagicMock(spec=Message)
            mock_callback.message.edit_text = AsyncMock()

            await bot.handle_preference_callback(mock_callback)

            assert test_settings.get_quality(123) == "MP3"
            mock_callback.answer.assert_awaited_once()
            mock_callback.message.edit_text.assert_awaited_once()

    asyncio.run(run())


def test_handle_download_callback_expired():
    """Клик по устаревшей кнопке скачивания сообщает пользователю об истечении срока."""
    async def run():
        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "dl:expired_id:MP3"
        mock_callback.answer = AsyncMock()

        mock_bot = MagicMock(spec=bot.Bot)

        await bot.handle_download_callback(mock_callback, mock_bot)

        mock_callback.answer.assert_awaited_once_with(
            "⚠️ Срок действия этой кнопки истёк. Пожалуйста, отправьте ссылку на трек заново.",
            show_alert=True
        )

    asyncio.run(run())


def test_handle_download_callback_valid():
    """Клик по валидной кнопке запускает процесс скачивания."""
    async def run():
        mock_store = bot.QueryStore()
        await mock_store.put("valid_qid", {
            "url_or_query": "Queen - Bohemian Rhapsody",
            "meta": {"artist": "Queen", "title": "Bohemian Rhapsody"}
        })

        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "dl:valid_qid:MP3"
        mock_callback.answer = AsyncMock()
        mock_callback.message = MagicMock(spec=Message)
        mock_callback.message.chat = MagicMock()
        mock_callback.message.chat.id = 12345
        mock_callback.message.edit_reply_markup = AsyncMock()

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_message = AsyncMock(return_value=MagicMock(spec=Message))

        async def dummy_run(*args, **kwargs):
            pass

        with patch("bot.query_store", mock_store), \
             patch("bot.run_download_and_send", side_effect=dummy_run):
            await bot.handle_download_callback(mock_callback, mock_bot)

            mock_callback.answer.assert_awaited_once_with("Запуск скачивания (MP3)...")
            mock_callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
            mock_bot.send_message.assert_awaited_once()

    asyncio.run(run())


# =====================================================================
# 6. File Size Limits & Cloud Bot API
# =====================================================================

def test_run_download_cloud_limit_exceeded(tmp_path):
    """Файл > 50 МБ отклоняется со справочным сообщением в режиме Cloud Bot API."""
    async def run():
        dummy_large_file = tmp_path / "big_track.flac"
        dummy_large_file.write_bytes(b"0" * (52 * 1024 * 1024))  # 52 MB

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock()
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=dummy_large_file), \
             patch.object(config, "BOT_API_SERVER_URL", ""):

            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="test_query",
                target_quality="FLAC",
                meta={"artist": "Artist", "title": "Title"},
                status_msg=mock_status
            )

            # send_audio НЕ должен быть вызван
            mock_bot.send_audio.assert_not_awaited()
            mock_status.edit_text.assert_awaited()
            error_text = mock_status.edit_text.call_args[0][0]
            assert "Файл слишком большой" in error_text
            assert "52.0 МБ" in error_text
            assert "MP3 320k" in error_text

    asyncio.run(run())


def test_run_download_local_api_allows_large_file(tmp_path):
    """При наличии BOT_API_SERVER_URL файлы > 50 МБ отправляются успешно."""
    async def run():
        dummy_large_file = tmp_path / "big_track.flac"
        dummy_large_file.write_bytes(b"0" * (60 * 1024 * 1024))  # 60 MB

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock()
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=dummy_large_file), \
             patch.object(config, "BOT_API_SERVER_URL", "http://localhost:8081"), \
             patch("bot.prepare_thumbnail", return_value=None):

            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="test_query",
                target_quality="FLAC",
                meta={"artist": "Artist", "title": "Title", "duration": 300},
                status_msg=mock_status
            )

            # send_audio ДОЛЖЕН быть вызван
            mock_bot.send_audio.assert_awaited_once()
            mock_status.delete.assert_awaited_once()

    asyncio.run(run())


def test_run_download_failed_reports_error():
    """Если трек не найден ни на одном источнике, выводится корректная ошибка."""
    async def run():
        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock()
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=None):
            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="unknown_song",
                target_quality="MP3",
                meta={"artist": "Nobody", "title": "Nothing"},
                status_msg=mock_status
            )

            mock_bot.send_audio.assert_not_awaited()
            mock_status.edit_text.assert_awaited_once()
            err_text = mock_status.edit_text.call_args[0][0]
            assert "Не удалось скачать трек" in err_text

    asyncio.run(run())


# =====================================================================
# 7. Bot Creation & Thumbnail Handling
# =====================================================================

def test_create_bot_cloud_and_local():
    """Тестирует создание бота в режиме Telegram Cloud и с локальным сервером."""
    token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
    bot_cloud = bot.create_bot(token, api_server_url=None)
    assert bot_cloud.token == token
    assert bot_cloud.session.api.is_local is False

    bot_local = bot.create_bot(token, api_server_url="http://localhost:8081")
    assert bot_local.token == token
    assert bot_local.session.api.is_local is True


def test_prepare_thumbnail_from_url(tmp_path):
    """prepare_thumbnail скачивает обложку по URL."""
    fake_img = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1000
    with patch("bot.tagger.download_cover_art", return_value=(fake_img, "image/jpeg")):
        thumb = bot.prepare_thumbnail(
            file_path=tmp_path / "non_existing.mp3",
            album_art_url="https://example.com/art.jpg",
            temp_dir=tmp_path
        )
        assert thumb is not None
        assert thumb.exists()
        assert thumb.stat().st_size == len(fake_img)


def test_handle_id_command_removed_fallback():
    """Команда /id удалена: отсутствует в модуле и в /start, а отправка /id вызывает ответ о неизвестной команде."""
    async def run():
        # 1. Проверяем отсутствие функции handle_id в bot.py
        assert not hasattr(bot, "handle_id"), "bot.py не должен экспортировать handle_id"

        # 2. Проверяем отсутствие /id в приветственном сообщении handle_start
        mock_msg_start = MagicMock(spec=Message)
        mock_msg_start.answer = AsyncMock()
        await bot.handle_start(mock_msg_start)
        start_text = mock_msg_start.answer.call_args[0][0]
        assert "/id" not in start_text, "/id не должен присутствовать в приветственном сообщении /start"

        # 3. Проверяем fallback: входящий текст '/id' перенаправляется на handle_track_query и возвращает отказ
        mock_msg_query = MagicMock(spec=Message)
        mock_msg_query.text = "/id"
        mock_msg_query.from_user = MagicMock()
        mock_msg_query.from_user.id = 123456789
        mock_msg_query.answer = AsyncMock()
        mock_bot = MagicMock(spec=bot.Bot)

        await bot.handle_track_query(mock_msg_query, mock_bot)
        mock_msg_query.answer.assert_awaited_once()
        answer_text = mock_msg_query.answer.call_args[0][0]
        assert "Неизвестная команда" in answer_text
        assert "123456789" not in answer_text

    asyncio.run(run())


def test_handle_unsupported_content():
    """Неподдерживаемый контент вызывает подсказку отправить ссылку или текст."""
    async def run():
        mock_msg = MagicMock(spec=Message)
        mock_msg.answer = AsyncMock()

        await bot.handle_unsupported_content(mock_msg)
        mock_msg.answer.assert_awaited_once()
        assert "ссылку на трек или его название текстом" in mock_msg.answer.call_args[0][0]

    asyncio.run(run())


def test_handle_track_query_not_found():
    """Если метаданные не найдены, выводится сообщение об ошибке."""
    async def run():
        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "Unknown Song 123456789"
        mock_msg.from_user = MagicMock()
        mock_msg.from_user.id = 111
        searching_msg = MagicMock(spec=Message)
        searching_msg.edit_text = AsyncMock()
        mock_msg.answer = AsyncMock(return_value=searching_msg)

        mock_bot = MagicMock(spec=bot.Bot)

        with patch("bot.metadata.resolve_query_metadata", return_value=None):
            await bot.handle_track_query(mock_msg, mock_bot)

            mock_msg.answer.assert_awaited_once()
            searching_msg.edit_text.assert_awaited_once()
            assert "Трек не найден" in searching_msg.edit_text.call_args[0][0]

    asyncio.run(run())


def test_handle_track_query_ask_mode():
    """В режиме ASK бот присылает карточку с кнопками выбора качества."""
    async def run():
        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "Queen - Bohemian Rhapsody"
        mock_msg.from_user = MagicMock()
        mock_msg.from_user.id = 222
        searching_msg = MagicMock(spec=Message)
        searching_msg.delete = AsyncMock()
        mock_msg.answer = AsyncMock(side_effect=[searching_msg, MagicMock(spec=Message)])

        mock_bot = MagicMock(spec=bot.Bot)
        mock_meta = {
            "artist": "Queen",
            "title": "Bohemian Rhapsody",
            "album": "A Night at the Opera",
            "year": "1975",
            "duration": 354,
        }

        with patch("bot.metadata.resolve_query_metadata", return_value=mock_meta):
            await bot.handle_track_query(mock_msg, mock_bot)

            assert mock_msg.answer.await_count == 2
            card_args = mock_msg.answer.await_args_list[1]
            card_text = card_args[0][0]
            assert "Queen — Bohemian Rhapsody" in card_text
            assert "A Night at the Opera" in card_text
            reply_markup = card_args[1]["reply_markup"]
            assert reply_markup is not None
            assert len(reply_markup.inline_keyboard[0]) == 2

    asyncio.run(run())


def test_handle_track_query_auto_download_mode(tmp_path):
    """В режиме с авто-скачиванием бот запускает процесс загрузки."""
    async def run():
        test_settings = bot.UserSettings(tmp_path / "settings.json")
        test_settings.set_quality(333, "MP3")

        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "Queen - Radio Ga Ga"
        mock_msg.from_user = MagicMock()
        mock_msg.from_user.id = 333
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 12345
        searching_msg = MagicMock(spec=Message)
        searching_msg.delete = AsyncMock()
        status_msg = MagicMock(spec=Message)
        mock_msg.answer = AsyncMock(side_effect=[searching_msg, status_msg])

        mock_bot = MagicMock(spec=bot.Bot)
        mock_meta = {
            "artist": "Queen",
            "title": "Radio Ga Ga",
            "album": "The Works",
            "year": "1984",
            "duration": 348,
        }

        async def dummy_run(*args, **kwargs):
            pass

        with patch("bot.user_settings", test_settings), \
             patch("bot.metadata.resolve_query_metadata", return_value=mock_meta), \
             patch("bot.run_download_and_send", side_effect=dummy_run) as mock_download_task:

            await bot.handle_track_query(mock_msg, mock_bot)

            assert mock_msg.answer.await_count == 2
            auto_text = mock_msg.answer.await_args_list[1][0][0]
            assert "Авто-скачивание [MP3]" in auto_text

    asyncio.run(run())


# =====================================================================
# 8. Edge Cases & Robustness Tests
# =====================================================================

def test_parse_allowed_users_quoted_and_whitespace():
    """Тестирует парсинг списка с кавычками и пробелами."""
    assert config._parse_allowed_users('"123, 456"') == {123, 456}
    assert config._parse_allowed_users("'123, 456'") == {123, 456}
    assert config._parse_allowed_users(' "123", "456" ') == {123, 456}


def test_escape_html_edge_cases():
    """Тестирует граничные случаи экранирования HTML (None, 0, спецсимволы)."""
    assert bot.escape_html(0) == "0"
    assert bot.escape_html(None) == ""
    assert bot.escape_html("&<>\"'") == "&amp;&lt;&gt;&quot;&#x27;"


def test_user_settings_corrupted_value(tmp_path):
    """При наличии некорректных значений в файле настроек возвращается ASK."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"123": "CORRUPTED_VALUE"}', encoding="utf-8")
    store = bot.UserSettings(settings_file)
    assert store.get_quality(123) == "ASK"


def test_query_store_update_existing_key():
    """Обновление существующего ключа не дублирует его в очереди вытеснения."""
    async def run():
        store = bot.QueryStore(max_items=2)
        await store.put("k1", {"val": 1})
        await store.put("k2", {"val": 2})
        await store.put("k1", {"val": 10})  # обновление k1
        await store.put("k3", {"val": 3})   # k2 должен вытесниться, k1 остаться
        assert await store.get("k2") is None
        assert await store.get("k1") is not None
        assert await store.get("k3") is not None
    asyncio.run(run())


def test_handle_download_callback_invalid_quality():
    """Поддельный callback с неизвестным качеством отклоняется."""
    async def run():
        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "dl:qid:INVALID"
        mock_callback.answer = AsyncMock()
        mock_bot = MagicMock(spec=bot.Bot)
        await bot.handle_download_callback(mock_callback, mock_bot)
        mock_callback.answer.assert_awaited_once_with("Неверное качество скачивания.", show_alert=True)
    asyncio.run(run())


def test_handle_preference_callback_invalid():
    """Поддельный callback с неизвестной настройкой качества отклоняется."""
    async def run():
        mock_callback = MagicMock(spec=CallbackQuery)
        mock_callback.data = "pref:MALICIOUS"
        mock_callback.from_user = MagicMock(id=12345)
        mock_callback.answer = AsyncMock()
        await bot.handle_preference_callback(mock_callback)
        mock_callback.answer.assert_awaited_once_with("Неверное значение качества.", show_alert=True)
    asyncio.run(run())


def test_handle_track_query_unknown_command():
    """Неизвестная команда не ищется как трек, а возвращает подсказку."""
    async def run():
        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "/unknown_command"
        mock_msg.answer = AsyncMock()
        mock_bot = MagicMock(spec=bot.Bot)
        await bot.handle_track_query(mock_msg, mock_bot)
        mock_msg.answer.assert_awaited_once()
        assert "Неизвестная команда" in mock_msg.answer.call_args[0][0]
    asyncio.run(run())


def test_status_notifier_throttling_and_flush():
    """Быстрые обновления статуса не теряются благодаря trailing flush."""
    async def run():
        mock_bot = MagicMock(spec=bot.Bot)
        mock_msg = MagicMock(spec=Message)
        mock_msg.edit_text = AsyncMock()

        notifier = bot.StatusNotifier(mock_bot, mock_msg, header="Header", min_interval=0.04)

        # Отправляем первое обновление
        notifier.notify("[*] Step 1")
        await asyncio.sleep(0.01)
        # Отправляем второе обновление быстро следом (< min_interval)
        notifier.notify("[*] Step 2")
        await asyncio.sleep(0.01)
        # Сразу второе не должно быть отправлено
        assert mock_msg.edit_text.await_count == 1

        # Ждем истечения интервала (trailing flush)
        await asyncio.sleep(0.06)
        assert mock_msg.edit_text.await_count == 2
        assert "Step 2" in mock_msg.edit_text.await_args_list[1][0][0]

        notifier.close()

    asyncio.run(run())


def test_prepare_thumbnail_oversized_fallback(tmp_path):
    """Изображение больше 200 КБ без ffmpeg безопасно отсекается."""
    oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (250 * 1024)
    with patch("bot.tagger.download_cover_art", return_value=(oversized, "image/jpeg")), \
         patch("subprocess.run", side_effect=FileNotFoundError):
        res = bot.prepare_thumbnail(
            file_path=tmp_path / "song.mp3",
            album_art_url="https://example.com/huge.jpg",
            temp_dir=tmp_path
        )
        assert res is None


def test_prepare_thumbnail_ffmpeg_scaling(tmp_path):
    """ffmpeg успешно масштабирует и конвертирует обложку."""
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
    def fake_run(cmd, *args, **kwargs):
        out_file = Path(cmd[-1])
        out_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 1000)
        res = MagicMock()
        res.returncode = 0
        return res

    with patch("bot.tagger.download_cover_art", return_value=(fake_png, "image/png")), \
         patch("subprocess.run", side_effect=fake_run):
        res = bot.prepare_thumbnail(
            file_path=tmp_path / "song.mp3",
            album_art_url="https://example.com/art.png",
            temp_dir=tmp_path
        )
        assert res is not None
        assert res.exists()


def test_run_download_thumbnail_failure_retries_without(tmp_path):
    """При сбое отправки с миниатюрой происходит повторная отправка без неё."""
    async def run():
        dummy_file = tmp_path / "test.mp3"
        dummy_file.write_bytes(b"12345")
        dummy_thumb = tmp_path / "thumb.jpg"
        dummy_thumb.write_bytes(b"thumb")

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock(side_effect=[Exception("Bad thumbnail"), MagicMock()])
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=dummy_file), \
             patch("bot.prepare_thumbnail", return_value=dummy_thumb):
            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="test",
                target_quality="MP3",
                meta={"artist": "A", "title": "B"},
                status_msg=mock_status
            )
            assert mock_bot.send_audio.await_count == 2
            assert "thumbnail" in mock_bot.send_audio.await_args_list[0][1]
            assert "thumbnail" not in mock_bot.send_audio.await_args_list[1][1]

    asyncio.run(run())


# =====================================================================
# 9. Local Telegram Bot API Manager & Lifecycle Tests
# =====================================================================

def test_config_local_bot_api_settings():
    """Тестирует конфигурационные переменные и хелпер is_local_bot_api_enabled."""
    # По умолчанию при отсутствии ключей выключено
    with patch.object(config, "LOCAL_BOT_API", False):
        assert config.is_local_bot_api_enabled() is False

    with patch.object(config, "LOCAL_BOT_API", True):
        assert config.is_local_bot_api_enabled() is True

    # Проверка порта по умолчанию
    assert isinstance(config.BOT_API_PORT, int)
    assert config.BOT_API_PORT > 0

    # Проверка директории данных
    assert isinstance(config.BOT_API_DATA_DIR, Path)


def test_local_bot_api_manager_reuse_existing_server():
    """Если локальный сервер уже отвечает по HTTP, переиспользуем его без запуска процессов."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)
        with patch.object(manager, "check_health", AsyncMock(return_value=True)), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("shutil.which") as mock_which:

            url = await manager.start()
            assert url == "http://127.0.0.1:8081"
            assert manager.process is None
            assert manager.started_docker is False
            assert manager._is_external is True

            # Никакой подпроцесс не запускался
            mock_exec.assert_not_called()
            mock_which.assert_not_called()

            # Остановка не трогает внешний сервер
            await manager.stop()
            assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_missing_credentials_fallback():
    """Если сервер не запущен и API credentials отсутствуют, логирует предупреждение и возвращает None."""
    async def run():
        manager = bot.LocalBotAPIManager(api_id="", api_hash="", port=8081)
        with patch.object(manager, "check_health", AsyncMock(return_value=False)), \
             patch("asyncio.create_subprocess_exec") as mock_exec:

            url = await manager.start()
            assert url is None
            assert manager.process is None
            assert manager.started_docker is False
            mock_exec.assert_not_called()

    asyncio.run(run())


def test_local_bot_api_manager_start_binary_success(tmp_path):
    """Успешный запуск локального бинарного файла telegram-bot-api."""
    async def run():
        fake_bin = tmp_path / "telegram-bot-api.exe"
        fake_bin.write_text("binary")

        manager = bot.LocalBotAPIManager(
            api_id=123456,
            api_hash="secret_hash_value",
            port=8081,
            data_dir=tmp_path / "data",
            bin_path=fake_bin,
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.terminate = MagicMock()

        # Первичная проверка healthcheck: False (сервер еще не запущен), затем True (сервер поднялся)
        health_mock = AsyncMock(side_effect=[False, True])

        with patch.object(manager, "check_health", health_mock), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)) as mock_exec:

            url = await manager.start()
            assert url == "http://127.0.0.1:8081"
            assert manager.process is mock_proc
            assert manager.started_docker is False

            # Проверяем аргументы командной строки
            mock_exec.assert_awaited_once()
            called_cmd = mock_exec.await_args[0]
            assert str(fake_bin.resolve()) in [str(Path(x).resolve()) if Path(x).exists() else x for x in called_cmd]
            assert "--local" in called_cmd
            assert "--api-id=123456" in called_cmd
            assert "--api-hash=secret_hash_value" in called_cmd
            assert "--http-port=8081" in called_cmd
            assert f"--dir={manager.data_dir.resolve()}" in called_cmd

            # Корректная остановка процесса
            await manager.stop()
            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_awaited_once()
            assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_docker_fallback_success(tmp_path):
    """Если бинарный файл не найден, но есть Docker, запускает контейнер aiogram/telegram-bot-api."""
    async def run():
        manager = bot.LocalBotAPIManager(
            api_id="999888",
            api_hash="docker_hash",
            port=8085,
            data_dir=tmp_path / "bot_data",
            bin_path=None,
        )

        # Мокируем отсутствие бинарника и наличие docker в PATH
        with patch.object(manager, "find_binary", return_value=None), \
             patch("shutil.which", return_value="/usr/bin/docker"):

            mock_rm = MagicMock()
            mock_rm.wait = AsyncMock(return_value=0)

            mock_run = MagicMock()
            mock_run.returncode = 0
            mock_run.communicate = AsyncMock(return_value=(b"container_id_123", b""))

            mock_stop = MagicMock()
            mock_stop.wait = AsyncMock(return_value=0)

            mock_exec = AsyncMock(side_effect=[mock_rm, mock_run, mock_stop])

            health_mock = AsyncMock(side_effect=[False, True])

            with patch.object(manager, "check_health", health_mock), \
                 patch("asyncio.create_subprocess_exec", mock_exec):

                url = await manager.start()
                assert url == "http://127.0.0.1:8085"
                assert manager.started_docker is True
                assert manager.process is None

                # Проверяем аргументы docker run
                run_call_args = mock_exec.await_args_list[1][0]
                assert "docker" in run_call_args
                assert "run" in run_call_args
                assert "-d" in run_call_args
                assert "--rm" in run_call_args
                assert "aiogram/telegram-bot-api:latest" in run_call_args
                assert "-e" in run_call_args
                assert "TELEGRAM_LOCAL=1" in run_call_args
                assert "TELEGRAM_API_ID=999888" in run_call_args

                # Остановка контейнера
                await manager.stop()
                assert manager.started_docker is False
                stop_call_args = mock_exec.await_args_list[2][0]
                assert stop_call_args[:3] == ("docker", "stop", "-t")

    asyncio.run(run())


def test_local_bot_api_manager_neither_binary_nor_docker_fallback():
    """Если ни бинарник, ни Docker не доступны, возвращается None (fallback на Cloud API)."""
    async def run():
        manager = bot.LocalBotAPIManager(
            api_id="123",
            api_hash="abc",
            port=8081,
        )
        with patch.object(manager, "check_health", AsyncMock(return_value=False)), \
             patch.object(manager, "find_binary", return_value=None), \
             patch("shutil.which", return_value=None), \
             patch("asyncio.create_subprocess_exec") as mock_exec:

            url = await manager.start()
            assert url is None
            assert manager.process is None
            assert manager.started_docker is False
            mock_exec.assert_not_called()

    asyncio.run(run())


def test_local_bot_api_manager_healthcheck_timeout_stops_and_returns_none(tmp_path):
    """Если сервер запущен, но не ответил на healthcheck за 10 секунд, происходит откат."""
    async def run():
        manager = bot.LocalBotAPIManager(
            api_id="123",
            api_hash="abc",
            port=8081,
            data_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch.object(manager, "check_health", AsyncMock(return_value=False)), \
             patch.object(manager, "find_binary", return_value=tmp_path / "bin.exe"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)), \
             patch.object(manager, "_wait_until_ready", AsyncMock(return_value=False)), \
             patch.object(manager, "stop", AsyncMock()) as mock_stop:

            url = await manager.start()
            assert url is None
            mock_stop.assert_awaited_once()

    asyncio.run(run())


def test_local_bot_api_manager_premature_crash_detected(tmp_path):
    """Если процесс сразу упал с ошибкой, _wait_until_ready мгновенно возвращает False."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # процесс завершился с кодом ошибки
        manager.process = mock_proc

        with patch.object(manager, "check_health", AsyncMock(return_value=False)):
            ready = await manager._wait_until_ready("http://127.0.0.1:8081", timeout=10.0)
            assert ready is False

    asyncio.run(run())


def test_local_bot_api_manager_stop_kills_hung_process():
    """Если процесс завис и не завершается по terminate(), вызывается kill()."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        # Первый wait (после terminate) таймаутится, второй (после kill) завершается
        mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
        manager.process = mock_proc

        await manager.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_find_binary_candidates(tmp_path):
    """Тестирует приоритеты поиска бинарного файла telegram-bot-api."""
    # 1. Пользовательский бинарник
    custom_bin = tmp_path / "my_custom_bot_api.exe"
    custom_bin.write_text("dummy")

    mgr1 = bot.LocalBotAPIManager(bin_path=custom_bin)
    assert mgr1.find_binary() == custom_bin.resolve()

    # 2. Поиск в PATH
    mgr2 = bot.LocalBotAPIManager(bin_path=None)
    with patch("shutil.which", return_value="C:\\tools\\telegram-bot-api.exe"):
        found = mgr2.find_binary()
        assert found == Path("C:\\tools\\telegram-bot-api.exe").resolve()

    # 3. Ничего не найдено
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.is_file", return_value=False):
        assert mgr2.find_binary() is None


def test_local_bot_api_manager_check_health_logic():
    """Тестирует логику HTTP-проверки работоспособности check_health."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)

        # 1. Сервер отвечает статусом 404 (нормальный ответ telegram-bot-api) -> True
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get)
        mock_session_cls = MagicMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", mock_session_cls):
            assert await manager.check_health("http://127.0.0.1:8081") is True

        # 2. Сетевая ошибка (ConnectionRefused) -> False
        mock_session_fail = MagicMock()
        mock_session_fail.get = MagicMock(side_effect=Exception("Connection refused"))
        mock_session_fail_cls = MagicMock()
        mock_session_fail_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session_fail)
        mock_session_fail_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", mock_session_fail_cls):
            assert await manager.check_health("http://127.0.0.1:8081") is False

    asyncio.run(run())


def test_main_integration_with_local_bot_api():
    """Тестирует интеграцию жизненного цикла LocalBotAPIManager в bot.main()."""
    async def run():
        mock_manager = MagicMock()
        mock_manager.start = AsyncMock(return_value="http://127.0.0.1:8081")
        mock_manager.stop = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.session = MagicMock()
        mock_bot.session.close = AsyncMock()

        mock_dp = MagicMock()
        mock_dp.start_polling = AsyncMock(return_value=None)
        mock_dp.message = MagicMock()
        mock_dp.callback_query = MagicMock()

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch.object(config, "BOT_TOKEN", "123:ABC"), \
             patch.object(config, "is_local_bot_api_enabled", return_value=True), \
             patch("bot.SingleInstanceLock", return_value=mock_lock), \
             patch("bot.LocalBotAPIManager", return_value=mock_manager), \
             patch("bot.create_bot", return_value=mock_bot) as mock_create_bot, \
             patch("bot.Dispatcher", return_value=mock_dp):

            await bot.main()

            mock_manager.start.assert_awaited_once()
            mock_create_bot.assert_called_once_with(token="123:ABC", api_server_url="http://127.0.0.1:8081")
            mock_dp.start_polling.assert_awaited_once_with(mock_bot)
            mock_bot.session.close.assert_awaited_once()
            mock_manager.stop.assert_awaited_once()

    asyncio.run(run())


def test_main_with_local_bot_api_disabled():
    """Если локальный API выключен, LocalBotAPIManager не запускается."""
    async def run():
        mock_bot = MagicMock()
        mock_bot.session = MagicMock()
        mock_bot.session.close = AsyncMock()

        mock_dp = MagicMock()
        mock_dp.start_polling = AsyncMock(return_value=None)
        mock_dp.message = MagicMock()
        mock_dp.callback_query = MagicMock()

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch.object(config, "BOT_TOKEN", "123:ABC"), \
             patch.object(config, "BOT_API_SERVER_URL", ""), \
             patch.object(config, "is_local_bot_api_enabled", return_value=False), \
             patch("bot.SingleInstanceLock", return_value=mock_lock), \
             patch("bot.LocalBotAPIManager") as mock_manager_cls, \
             patch("bot.create_bot", return_value=mock_bot) as mock_create_bot, \
             patch("bot.Dispatcher", return_value=mock_dp):

            await bot.main()

            mock_manager_cls.assert_not_called()
            mock_create_bot.assert_called_once_with(token="123:ABC", api_server_url=None)
            mock_bot.session.close.assert_awaited_once()

    asyncio.run(run())


def test_local_bot_api_manager_docker_run_fails_with_error(tmp_path):
    """Если docker run завершился с ненулевым кодом ошибки, происходит откат на Cloud API."""
    async def run():
        manager = bot.LocalBotAPIManager(
            api_id="123",
            api_hash="abc",
            port=8081,
            data_dir=tmp_path,
        )

        mock_rm = MagicMock()
        mock_rm.wait = AsyncMock(return_value=0)

        mock_run = MagicMock()
        mock_run.returncode = 125
        mock_run.communicate = AsyncMock(return_value=(b"", b"Docker daemon error"))

        mock_exec = AsyncMock(side_effect=[mock_rm, mock_run])

        with patch.object(manager, "check_health", AsyncMock(return_value=False)), \
             patch.object(manager, "find_binary", return_value=None), \
             patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("asyncio.create_subprocess_exec", mock_exec):

            url = await manager.start()
            assert url is None
            assert manager.started_docker is False
            assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_stop_exception_safety():
    """Проверяет безопасность метода stop() при возникновении ошибок остановки."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)

        # Ошибка при terminate() процесса
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock(side_effect=RuntimeError("Process terminate failure"))
        manager.process = mock_proc

        await manager.stop()
        assert manager.process is None

        # Ошибка при остановке Docker
        manager.started_docker = True
        with patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("Docker stop failed")):
            await manager.stop()
            assert manager.started_docker is False

    asyncio.run(run())


def test_run_download_local_api_exceeds_2gb_limit():
    """При превышении лимита 2 ГБ для локального Bot API выводится понятное сообщение об ошибке."""
    async def run():
        mock_stat = MagicMock()
        mock_stat.st_size = 2050 * 1024 * 1024  # 2050 MB

        dummy_file = MagicMock()
        dummy_file.exists.return_value = True
        dummy_file.stat.return_value = mock_stat
        dummy_file.suffix = ".flac"

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock()
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=dummy_file), \
             patch.object(config, "BOT_API_SERVER_URL", "http://localhost:8081"):

            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="test_query",
                target_quality="FLAC",
                meta={"artist": "Artist", "title": "Title"},
                status_msg=mock_status
            )

            mock_bot.send_audio.assert_not_awaited()
            mock_status.edit_text.assert_awaited()
            text = mock_status.edit_text.call_args[0][0]
            assert "превышает лимит сервера в 2 ГБ" in text

    asyncio.run(run())


def test_run_download_local_api_sends_large_file_under_2gb():
    """Файл размером 150 МБ (больше 50 МБ) успешно отправляется при подключенном локальном Bot API."""
    async def run():
        mock_stat = MagicMock()
        mock_stat.st_size = 150 * 1024 * 1024  # 150 MB

        dummy_large_file = MagicMock()
        dummy_large_file.exists.return_value = True
        dummy_large_file.stat.return_value = mock_stat
        dummy_large_file.suffix = ".flac"

        mock_bot = MagicMock(spec=bot.Bot)
        mock_bot.send_audio = AsyncMock()
        mock_status = MagicMock(spec=Message)
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()

        with patch("bot.core.download_track_by_link", return_value=dummy_large_file), \
             patch.object(config, "BOT_API_SERVER_URL", "http://127.0.0.1:8081"), \
             patch("bot.prepare_thumbnail", return_value=None):

            await bot.run_download_and_send(
                bot=mock_bot,
                chat_id=111,
                query_or_url="large_test",
                target_quality="FLAC",
                meta={"artist": "Band", "title": "Epic Long Track", "duration": 600},
                status_msg=mock_status,
            )

            mock_bot.send_audio.assert_awaited_once()
            mock_status.delete.assert_awaited_once()

    asyncio.run(run())


def test_local_bot_api_manager_stop_already_exited_process():
    """Если процесс уже завершился и terminate() выбрасывает ProcessLookupError, метод stop() завершается без ошибок."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock(side_effect=ProcessLookupError())
        mock_proc.wait = AsyncMock(return_value=0)
        manager.process = mock_proc

        await manager.stop()

        mock_proc.terminate.assert_called_once()
        assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_stop_handles_permission_error_on_windows():
    """На Windows terminate() может выбросить PermissionError (WinError 5), метод stop() не должен падать."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081)
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock(side_effect=PermissionError("Access is denied"))
        mock_proc.wait = AsyncMock(return_value=0)
        manager.process = mock_proc

        await manager.stop()
        assert manager.process is None

    asyncio.run(run())


def test_local_bot_api_manager_stop_docker_timeout_force_removes():
    """Если docker stop зависает и таймаутится, вызывается docker rm -f для принудительного удаления."""
    async def run():
        manager = bot.LocalBotAPIManager(port=8081, container_name="test-cont")
        manager.started_docker = True

        mock_stop = MagicMock()
        mock_stop.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        mock_rm_force = MagicMock()
        mock_rm_force.wait = AsyncMock(return_value=0)

        mock_exec = AsyncMock(side_effect=[mock_stop, mock_rm_force])

        with patch("asyncio.create_subprocess_exec", mock_exec):
            await manager.stop()

            assert manager.started_docker is False
            assert mock_exec.await_count == 2
            stop_args = mock_exec.await_args_list[0][0]
            assert stop_args[:3] == ("docker", "stop", "-t")
            rm_args = mock_exec.await_args_list[1][0]
            assert rm_args == ("docker", "rm", "-f", "test-cont")

    asyncio.run(run())


def test_main_local_bot_api_start_failure_falls_back_to_cloud():
    """Если запуск локального API завершился неудачей (None), bot.main() сбрасывает api_url на None и использует Cloud API."""
    async def run():
        mock_manager = MagicMock()
        mock_manager.start = AsyncMock(return_value=None)
        mock_manager.stop = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.session = MagicMock()
        mock_bot.session.close = AsyncMock()

        mock_dp = MagicMock()
        mock_dp.start_polling = AsyncMock(return_value=None)
        mock_dp.message = MagicMock()
        mock_dp.callback_query = MagicMock()

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch.object(config, "BOT_TOKEN", "123:ABC"), \
             patch.object(config, "BOT_API_SERVER_URL", "http://stale-old-url:8081"), \
             patch.object(config, "is_local_bot_api_enabled", return_value=True), \
             patch("bot.SingleInstanceLock", return_value=mock_lock), \
             patch("bot.LocalBotAPIManager", return_value=mock_manager), \
             patch("bot.create_bot", return_value=mock_bot) as mock_create_bot, \
             patch("bot.Dispatcher", return_value=mock_dp):

            await bot.main()

            mock_manager.start.assert_awaited_once()
            # api_server_url сброшен в None (Cloud API)
            mock_create_bot.assert_called_once_with(token="123:ABC", api_server_url=None)
            assert config.BOT_API_SERVER_URL == ""
            mock_manager.stop.assert_awaited_once()

    asyncio.run(run())


def test_main_stop_called_even_if_bot_creation_fails():
    """Если create_bot выбрасывает исключение, менеджер все равно гарантированно останавливается в finally."""
    async def run():
        mock_manager = MagicMock()
        mock_manager.start = AsyncMock(return_value="http://127.0.0.1:8081")
        mock_manager.stop = AsyncMock()

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch.object(config, "BOT_TOKEN", "123:ABC"), \
             patch.object(config, "is_local_bot_api_enabled", return_value=True), \
             patch("bot.SingleInstanceLock", return_value=mock_lock), \
             patch("bot.LocalBotAPIManager", return_value=mock_manager), \
             patch("bot.create_bot", side_effect=RuntimeError("Bot creation failed")):

            with pytest.raises(RuntimeError, match="Bot creation failed"):
                await bot.main()

            mock_manager.start.assert_awaited_once()
            mock_manager.stop.assert_awaited_once()

    asyncio.run(run())


# =====================================================================
# 10. Single Instance Lock (Anti-conflict protection)
# =====================================================================

def test_single_instance_lock_acquire_and_release(tmp_path):
    """Тестирует базовый захват и освобождение блокировки экземпляра."""
    lock_file = tmp_path / "test.lock"
    pid_file = tmp_path / "test.pid"

    lock = bot.SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
    assert lock.acquire() is True
    assert lock_file.exists()
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    lock.release()
    assert not pid_file.exists()


def test_single_instance_lock_blocks_second_instance(tmp_path):
    """Второй экземпляр не может захватить блокировку, пока первый активен."""
    lock_file = tmp_path / "test.lock"
    pid_file = tmp_path / "test.pid"

    lock1 = bot.SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
    assert lock1.acquire() is True

    lock2 = bot.SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
    assert lock2.acquire(auto_terminate_stale=False) is False

    lock1.release()
    assert lock2.acquire(auto_terminate_stale=False) is True
    lock2.release()


def test_single_instance_lock_auto_terminates_stale(tmp_path):
    """При обнаружении занятой блокировки вызывается завершение старого PID."""
    lock_file = tmp_path / "test.lock"
    pid_file = tmp_path / "test.pid"

    lock1 = bot.SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
    assert lock1.acquire() is True
    pid_file.write_text("987654", encoding="utf-8")

    lock2 = bot.SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)

    with patch.object(lock2, "_terminate_pid") as mock_term:
        # Вызываем с auto_terminate_stale=True
        # В тесте lock1 всё ещё удерживает файл, но мы проверяем вызов _terminate_pid
        res = lock2.acquire(auto_terminate_stale=True)
        assert res is False
        mock_term.assert_called_once_with(987654)

    lock1.release()


def test_main_exits_if_single_instance_lock_fails():
    """Если блокировка не может быть захвачена, bot.main() завершает процесс с кодом 1."""
    async def run():
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False

        with patch.object(config, "BOT_TOKEN", "123:ABC"), \
             patch("bot.SingleInstanceLock", return_value=mock_lock), \
             pytest.raises(SystemExit) as exc_info:
            await bot.main()

        assert exc_info.value.code == 1
        mock_lock.release.assert_not_called()

    asyncio.run(run())







