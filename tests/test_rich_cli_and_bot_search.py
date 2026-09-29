from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import pytest
import asyncio

from cli.ui import (
    print_banner,
    print_search_table,
    print_track_panel,
    print_success_panel,
    print_error,
    print_info,
)
from cli.main import select_candidate_interactive, cli_search_and_download
from bot.storage import UserSettings, QueryStore
from bot.keyboards import build_settings_keyboard, build_search_results_keyboard
import bot as bot_module


# =====================================================================
# 1. Rich UI CLI Tests
# =====================================================================

def test_print_banner_runs_without_errors():
    """Проверка отрисовки баннера в интерактивном и неинтерактивном режимах."""
    with patch("cli.ui.is_interactive", return_value=True):
        print_banner()

    with patch("cli.ui.is_interactive", return_value=False):
        print_banner()


def test_print_search_table_renders_candidates():
    """Проверка отрисовки таблицы кандидатов поиска."""
    candidates = [
        {
            "artist": "Daft Punk",
            "title": "Get Lucky",
            "album": "Random Access Memories",
            "year": "2013",
            "duration": 248,
            "source_quality": "Deezer (FLAC / 320k)",
            "explicit": False,
        },
        {
            "artist": "Queen",
            "title": "Bohemian Rhapsody",
            "album": "A Night at the Opera",
            "year": "1975",
            "duration": 354,
            "source_quality": "Apple Music (256k AAC)",
            "explicit": True,
        }
    ]
    with patch("cli.ui.is_interactive", return_value=True):
        print_search_table(candidates, "hits")

    with patch("cli.ui.is_interactive", return_value=False):
        print_search_table(candidates, "hits")


def test_print_panels_render():
    """Проверка карточек трека и успешного сохранения."""
    with patch("cli.ui.is_interactive", return_value=True):
        print_track_panel("Artist", "Track", "MP3", album="Alb", year="2020", duration=180)
        print_success_panel(Path("downloads/test.mp3"))
        print_error("Test error")
        print_info("Test info")


def test_select_candidate_interactive_rich():
    """Интерактивный выбор трека в CLI."""
    candidates = [{"artist": "A1", "title": "T1"}, {"artist": "A2", "title": "T2"}]
    with patch("builtins.input", return_value="2"):
        res = select_candidate_interactive(candidates, query="test")
        assert res == candidates[1]


# =====================================================================
# 2. UserSettings: Search Mode Tests
# =====================================================================

def test_user_settings_search_mode(tmp_path):
    """Проверка сохранения и чтения режима поиска (BEST vs LIST)."""
    settings_file = tmp_path / "user_settings.json"
    us = UserSettings(filepath=settings_file)

    # По умолчанию для нового пользователя — BEST
    assert us.get_search_mode(12345) == "BEST"

    # Переключение на LIST
    us.set_search_mode(12345, "LIST")
    assert us.get_search_mode(12345) == "LIST"

    # Проверка персистентности (перезагрузка из файла)
    us_reloaded = UserSettings(filepath=settings_file)
    assert us_reloaded.get_search_mode(12345) == "LIST"

    # Возврат на BEST
    us_reloaded.set_search_mode(12345, "BEST")
    assert us_reloaded.get_search_mode(12345) == "BEST"

    # Игнорирование неподдерживаемых режимов
    us_reloaded.set_search_mode(12345, "INVALID_MODE")
    assert us_reloaded.get_search_mode(12345) == "BEST"


# =====================================================================
# 3. Keyboards Tests
# =====================================================================

def test_build_settings_keyboard():
    """Проверка структуры единой клавиатуры /settings."""
    kb = build_settings_keyboard(current_quality="MP3", current_search_mode="LIST", lang="ru")
    assert len(kb.inline_keyboard) == 3
    # Строка 1: MP3 и FLAC
    row1 = [btn.callback_data for btn in kb.inline_keyboard[0]]
    assert "pref:MP3" in row1
    assert "pref:FLAC" in row1
    # Строка 2: ASK
    row2 = [btn.callback_data for btn in kb.inline_keyboard[1]]
    assert "pref:ASK" in row2
    # Строка 3: Режимы поиска
    row3 = [btn.callback_data for btn in kb.inline_keyboard[2]]
    assert "set_search:BEST" in row3
    assert "set_search:LIST" in row3


def test_build_search_results_keyboard():
    """Проверка кнопок выбора трека в поиске."""
    kb = build_search_results_keyboard(qid="test1234", count=5, lang="ru")
    assert len(kb.inline_keyboard) == 2
    # 5 кнопок выбора в первом ряду
    assert len(kb.inline_keyboard[0]) == 5
    assert kb.inline_keyboard[0][0].callback_data == "srch:test1234:1"
    assert kb.inline_keyboard[0][4].callback_data == "srch:test1234:5"
    # Кнопка отмены во втором ряду
    assert kb.inline_keyboard[1][0].callback_data == "srch:test1234:cancel"


# =====================================================================
# 4. Telegram Bot Search & Settings Handlers Tests
# =====================================================================

def test_handle_settings_command():
    """Проверка обработки команды /settings."""
    async def run():
        msg = MagicMock()
        msg.from_user.id = 99999
        msg.from_user.language_code = "ru"
        msg.answer = AsyncMock()

        await bot_module.handle_settings_command(msg)
        msg.answer.assert_called_once()
        args, kwargs = msg.answer.call_args
        assert "Настройки бота" in args[0]
        assert kwargs.get("reply_markup") is not None

    asyncio.run(run())


def test_handle_set_search_callback():
    """Проверка переключения режима поиска через инлайн-кнопку."""
    async def run():
        callback = MagicMock()
        callback.from_user.id = 88888
        callback.from_user.language_code = "ru"
        callback.data = "set_search:LIST"
        callback.answer = AsyncMock()
        callback.message.edit_text = AsyncMock()

        await bot_module.handle_set_search_callback(callback)
        callback.answer.assert_called_once()
        assert bot_module.user_settings.get_search_mode(88888) == "LIST"
        callback.message.edit_text.assert_called_once()

    asyncio.run(run())


def test_handle_search_command_empty_prompt():
    """Проверка подсказки при пустом вызове /search без аргументов."""
    async def run():
        msg = MagicMock()
        msg.from_user.id = 77777
        msg.from_user.language_code = "ru"
        msg.text = "/search"
        msg.answer = AsyncMock()
        bot = MagicMock()

        await bot_module.handle_search_command(msg, bot)
        msg.answer.assert_called_once()
        args, _ = msg.answer.call_args
        assert "Напиши поисковый запрос" in args[0]

    asyncio.run(run())


def test_handle_search_command_with_query():
    """Проверка работы /search <запрос>."""
    async def run():
        msg = MagicMock()
        msg.chat.id = 1234
        msg.from_user.id = 77777
        msg.from_user.language_code = "ru"
        msg.text = "/search Queen"
        searching_msg = MagicMock()
        searching_msg.edit_text = AsyncMock()
        msg.answer = AsyncMock(return_value=searching_msg)
        bot = MagicMock()

        mock_candidates = [
            {"artist": "Queen", "title": "Bohemian Rhapsody", "album": "A Night at the Opera", "year": "1975", "duration": 354, "source_quality": "Deezer", "explicit": False},
            {"artist": "Queen", "title": "Radio Ga Ga", "album": "The Works", "year": "1984", "duration": 348, "source_quality": "Deezer", "explicit": False}
        ]

        with patch("core.search_tracks", return_value=mock_candidates):
            await bot_module.handle_search_command(msg, bot)
            msg.answer.assert_called_once()
            searching_msg.edit_text.assert_called_once()
            args, kwargs = searching_msg.edit_text.call_args
            assert "Результаты поиска для:" in args[0]
            assert "Bohemian Rhapsody" in args[0]
            assert kwargs.get("reply_markup") is not None

    asyncio.run(run())


def test_handle_search_callback_cancel():
    """Проверка отмены поиска по кнопке 'Отмена'."""
    async def run():
        callback = MagicMock()
        callback.from_user.id = 77777
        callback.from_user.language_code = "ru"
        callback.data = "srch:fakeqid:cancel"
        callback.answer = AsyncMock()
        callback.message.edit_text = AsyncMock()
        bot = MagicMock()

        await bot_module.query_store.put("fakeqid", {"candidates": []})
        await bot_module.handle_search_callback(callback, bot)
        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()
        args, _ = callback.message.edit_text.call_args
        assert "Поиск отменен" in args[0]

    asyncio.run(run())


def test_handle_search_callback_select_with_auto_quality():
    """Проверка выбора трека из поиска при настроенном авто-качестве (MP3)."""
    async def run():
        callback = MagicMock()
        callback.from_user.id = 77777
        callback.from_user.language_code = "ru"
        callback.data = "srch:sampleqid:1"
        callback.message.chat.id = 5555
        callback.message.edit_reply_markup = AsyncMock()
        callback.answer = AsyncMock()

        bot = MagicMock()
        status_msg = MagicMock()
        bot.send_message = AsyncMock(return_value=status_msg)

        # У пользователя настроено качество MP3
        bot_module.user_settings.set_quality(77777, "MP3")

        candidate = {
            "artist": "Daft Punk",
            "title": "Get Lucky",
            "album": "RAM",
            "year": "2013",
            "duration": 248,
            "url": "https://www.deezer.com/track/67238735"
        }

        await bot_module.query_store.put("sampleqid", {"candidates": [candidate]})
        with patch("bot.run_download_and_send", new_callable=AsyncMock) as mock_run:
            await bot_module.handle_search_callback(callback, bot)
            callback.answer.assert_called()
            bot.send_message.assert_called_once()
            # Запущена задача скачивания
            await asyncio.sleep(0.01)
            mock_run.assert_called_once()

    asyncio.run(run())
