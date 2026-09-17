"""Tier 1: Category-Partition Feature Coverage Tests.

Covers all 9 core features with >=5 dedicated tests per feature:
1. /id command removal (R1)
2. i18n auto-detection (R4)
3. Manual language override (R4)
4. Custom Telegram emojis (R3)
5. Modular keyboards (R2, R3, R4)
6. Progress notifier (R2, R5)
7. Single-instance process locking (R2)
8. Local Bot API server lifecycle (R2)
9. Storage services (QueryStore & UserSettings) (R2, R4)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tests.e2e.conftest import (
    ID_AUDIO,
    ID_CHECK,
    ID_CROSS,
    ID_DOWNLOAD,
    ID_QUALITY,
    ID_SETTINGS,
    ID_TRACK,
    ID_VINYL,
    ID_WARNING,
    E2EEmoji,
    E2EI18nEngine,
    E2EKeyboards,
    E2ELocalBotAPIManager,
    E2EQueryStore,
    E2ESingleInstanceLock,
    E2EStatusNotifier,
    E2EUserSettings,
    autodetect_language,
    build_metadata_card,
    resolve_effective_language,
)


# ===========================================================================
# FEATURE 1: /id Command Removal (R1)
# ===========================================================================
class TestFeature1IdRemoval:
    """Requirement R1: Complete removal of /id command, router, help text, assertions."""

    def test_id_command_absent_from_help_ru(self, i18n_engine: E2EI18nEngine):
        """1. /help text in Russian must not mention /id."""
        help_text = i18n_engine.get("cmd.help", lang="ru")
        assert "/id" not in help_text
        assert "узнать ваш Telegram ID" not in help_text

    def test_id_command_absent_from_help_en(self, i18n_engine: E2EI18nEngine):
        """2. /help text in English must not mention /id."""
        help_text = i18n_engine.get("cmd.help", lang="en")
        assert "/id" not in help_text
        assert "Telegram ID" not in help_text

    def test_id_command_absent_from_start_welcome_ru(self, i18n_engine: E2EI18nEngine):
        """3. /start welcome text in Russian must not mention /id."""
        start_text = i18n_engine.get("cmd.start", lang="ru")
        assert "/id" not in start_text
        assert "узнать ваш Telegram ID" not in start_text

    def test_id_command_absent_from_start_welcome_en(self, i18n_engine: E2EI18nEngine):
        """4. /start welcome text in English must not mention /id."""
        start_text = i18n_engine.get("cmd.start", lang="en")
        assert "/id" not in start_text
        assert "Telegram ID" not in start_text

    def test_id_command_invocation_yields_unknown_or_unhandled(self, message_factory, i18n_engine):
        """5. Invoking /id produces unknown command message or no id exposure."""
        async def run():
            msg = message_factory(text="/id", user_id=987654321, language_code="ru")

            # Check if command handlers module is extracted
            try:
                from handlers import commands as cmd_handlers
                has_commands_module = True
            except ImportError:
                has_commands_module = False

            if has_commands_module:
                assert not hasattr(cmd_handlers, "handle_id")

            # In localized engine, unknown command does not reveal ID
            unknown_resp = i18n_engine.get("status.unknown_cmd", lang="ru")
            assert "987654321" not in unknown_resp
            assert "Неизвестная команда" in unknown_resp or "Unknown command" in unknown_resp

        asyncio.run(run())

    def test_id_search_query_not_hijacked_as_command(self):
        """6. Song queries with 'id' (e.g. 'Radiohead Kid A') must be treated as queries."""
        query = "Radiohead Kid A"
        is_command = query.startswith("/")
        assert not is_command
        cmd_candidate = query.split()[0].lstrip("/")
        assert cmd_candidate.lower() != "id"


# ===========================================================================
# FEATURE 2: i18n Auto-Detection (R4)
# ===========================================================================
class TestFeature2I18nAutoDetection:
    """Requirement R4: Language code inspection (starts with ru -> ru; else -> en)."""

    def test_autodetect_exact_ru(self):
        """1. Exact 'ru' language code selects Russian."""
        assert autodetect_language("ru") == "ru"

    def test_autodetect_regional_ru(self):
        """2. Regional Russian codes (ru-RU, ru-BY, ru-KZ, RU) select Russian."""
        assert autodetect_language("ru-RU") == "ru"
        assert autodetect_language("ru-BY") == "ru"
        assert autodetect_language("ru-KZ") == "ru"
        assert autodetect_language("RU") == "ru"

    def test_autodetect_english(self):
        """3. English codes (en, en-US, en-GB) select English."""
        assert autodetect_language("en") == "en"
        assert autodetect_language("en-US") == "en"
        assert autodetect_language("en-GB") == "en"

    def test_autodetect_non_ru_fallbacks(self):
        """4. Other non-Russian languages (es, fr, de, zh, ja) fallback to English."""
        for code in ["es", "fr", "de", "zh", "ja", "it", "pt"]:
            assert autodetect_language(code) == "en"

    def test_autodetect_missing_or_none_defaults_to_ru(self):
        """5. Missing, None, or empty language codes select Russian system default."""
        assert autodetect_language(None) == "ru"
        assert autodetect_language("") == "ru"
        assert autodetect_language("   ") == "ru"

    def test_autodetect_via_user_object(self, user_factory):
        """6. aiogram User object with language_code correctly resolves."""
        user_ru = user_factory(user_id=1, language_code="ru")
        user_en = user_factory(user_id=2, language_code="en-US")
        user_none = user_factory(user_id=3, language_code=None)

        assert autodetect_language(user_ru.language_code) == "ru"
        assert autodetect_language(user_en.language_code) == "en"
        assert autodetect_language(user_none.language_code) == "ru"


# ===========================================================================
# FEATURE 3: Manual Language Override (R4)
# ===========================================================================
class TestFeature3ManualLanguageOverride:
    """Requirement R4: Persistent user setting overriding client language."""

    def test_manual_override_set_and_get_en(self, user_settings: E2EUserSettings):
        """1. User can explicitly set English preference."""
        user_settings.set_language(1001, "en")
        assert user_settings.get_language(1001) == "en"

    def test_manual_override_set_and_get_ru(self, user_settings: E2EUserSettings):
        """2. User can explicitly set Russian preference."""
        user_settings.set_language(1002, "ru")
        assert user_settings.get_language(1002) == "ru"

    def test_manual_override_disk_persistence(self, temp_settings_path: Path):
        """3. Language preference persists across storage reloads."""
        storage1 = E2EUserSettings(file_path=temp_settings_path)
        storage1.set_language(2001, "en")
        storage1.set_quality(2001, "FLAC")

        # Reload from same file
        storage2 = E2EUserSettings(file_path=temp_settings_path)
        assert storage2.get_language(2001) == "en"
        assert storage2.get_quality(2001) == "FLAC"

    def test_manual_override_precedence_over_telegram_client(self, user_settings: E2EUserSettings):
        """4. Manual override strictly takes precedence over telegram language_code."""
        user_id = 3001
        user_settings.set_language(user_id, "en")

        # User has ru telegram client, but overridden to en
        resolved = resolve_effective_language(
            user_id=user_id,
            telegram_language_code="ru",
            user_settings=user_settings,
        )
        assert resolved == "en"

        # User with en telegram client, overridden to ru
        user_settings.set_language(3002, "ru")
        resolved_ru = resolve_effective_language(
            user_id=3002,
            telegram_language_code="en-US",
            user_settings=user_settings,
        )
        assert resolved_ru == "ru"

    def test_manual_override_reset_to_auto(self, user_settings: E2EUserSettings):
        """5. Resetting override to None/auto restores client auto-detection."""
        user_id = 4001
        user_settings.set_language(user_id, "en")
        assert user_settings.get_language(user_id) == "en"

        # Reset to auto
        user_settings.set_language(user_id, None)
        assert user_settings.get_language(user_id) is None

        # Reverts to Telegram client detection
        resolved = resolve_effective_language(
            user_id=user_id,
            telegram_language_code="ru",
            user_settings=user_settings,
        )
        assert resolved == "ru"

    def test_manual_override_distinct_per_user(self, user_settings: E2EUserSettings):
        """6. Language settings are isolated per user ID."""
        user_settings.set_language(5001, "ru")
        user_settings.set_language(5002, "en")

        assert user_settings.get_language(5001) == "ru"
        assert user_settings.get_language(5002) == "en"


# ===========================================================================
# FEATURE 4: Custom Telegram Emoji System (R3)
# ===========================================================================
class TestFeature4CustomTelegramEmojis:
    """Requirement R3: <tg-emoji> tags, button icons, fallbacks, music mappings."""

    def setup_method(self):
        E2EEmoji.set_custom_emoji_enabled(False)

    def teardown_method(self):
        E2EEmoji.set_custom_emoji_enabled(False)

    def test_custom_emoji_enabled_formats_tag(self):
        """1. When enabled, e() wraps fallback in <tg-emoji> tag."""
        E2EEmoji.set_custom_emoji_enabled(True)
        formatted = E2EEmoji.e(ID_AUDIO, "🎵")
        assert formatted == f'<tg-emoji emoji-id="{ID_AUDIO}">🎵</tg-emoji>'

    def test_custom_emoji_disabled_returns_raw_fallback(self):
        """2. When disabled, e() returns pure Unicode fallback."""
        E2EEmoji.set_custom_emoji_enabled(False)
        formatted = E2EEmoji.e(ID_AUDIO, "🎵")
        assert formatted == "🎵"

    def test_make_inline_button_custom_emoji_enabled(self):
        """3. When enabled, make_inline_button sets icon_custom_emoji_id without prepending text."""
        E2EEmoji.set_custom_emoji_enabled(True)
        btn = E2EEmoji.make_inline_button(
            text="MP3 320k",
            callback_data="quality:mp3:123",
            emoji_id=ID_AUDIO,
            fallback_emoji="🎵",
        )
        assert btn.text == "MP3 320k"
        assert btn.icon_custom_emoji_id == ID_AUDIO
        assert btn.callback_data == "quality:mp3:123"

    def test_make_inline_button_custom_emoji_disabled(self):
        """4. When disabled, make_inline_button prepends fallback emoji into button text."""
        E2EEmoji.set_custom_emoji_enabled(False)
        btn = E2EEmoji.make_inline_button(
            text="MP3 320k",
            callback_data="quality:mp3:123",
            emoji_id=ID_AUDIO,
            fallback_emoji="🎵",
        )
        assert btn.text == "🎵 MP3 320k"
        assert btn.icon_custom_emoji_id is None
        assert btn.callback_data == "quality:mp3:123"

    def test_music_domain_emoji_constants_exist(self):
        """5. All required music domain emoji IDs exist and are valid digits."""
        ids = [
            ID_AUDIO, ID_VINYL, ID_TRACK, ID_DOWNLOAD,
            ID_CHECK, ID_CROSS, ID_WARNING, ID_SETTINGS, ID_QUALITY
        ]
        for eid in ids:
            assert isinstance(eid, str)
            assert eid.isdigit()
            assert len(eid) > 10

    def test_custom_emoji_with_none_or_empty_id(self):
        """6. e() with None or empty emoji_id returns fallback even when enabled."""
        E2EEmoji.set_custom_emoji_enabled(True)
        assert E2EEmoji.e(None, "⚡") == "⚡"
        assert E2EEmoji.e("", "⚡") == "⚡"


# ===========================================================================
# FEATURE 5: Modular Keyboards (R2, R3, R4)
# ===========================================================================
class TestFeature5ModularKeyboards:
    """Requirement R2, R3, R4: Quality, settings, and language keyboard builders."""

    def test_quality_keyboard_structure_and_callbacks(self):
        """1. Quality keyboard has MP3 and FLAC buttons with correct callbacks."""
        kb = E2EKeyboards.build_quality_keyboard(query_id="query_abc", lang="ru")
        assert isinstance(kb, InlineKeyboardMarkup)
        assert len(kb.inline_keyboard) == 1
        row = kb.inline_keyboard[0]
        assert len(row) == 2
        assert row[0].callback_data == "quality:mp3:query_abc"
        assert row[1].callback_data == "quality:flac:query_abc"

    def test_quality_keyboard_bilingual_labels(self):
        """2. Quality keyboard text adapts according to language."""
        kb_ru = E2EKeyboards.build_quality_keyboard("q1", lang="ru")
        kb_en = E2EKeyboards.build_quality_keyboard("q1", lang="en")

        ru_row = kb_ru.inline_keyboard[0]
        en_row = kb_en.inline_keyboard[0]

        assert "MP3 320k" in ru_row[0].text
        assert "FLAC Lossless" in ru_row[1].text
        assert "MP3 320k" in en_row[0].text
        assert "FLAC Lossless" in en_row[1].text

    def test_settings_keyboard_structure(self):
        """3. Settings keyboard has Quality and Language selection rows."""
        kb = E2EKeyboards.build_settings_keyboard(lang="ru")
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[0][0].callback_data == "settings:quality"
        assert kb.inline_keyboard[1][0].callback_data == "settings:language"

    def test_language_keyboard_options_and_active_mark(self):
        """4. Language keyboard provides RU, EN, Auto, Back with active mark."""
        kb_en_active = E2EKeyboards.build_language_keyboard(current_pref="en", lang="en")
        assert len(kb_en_active.inline_keyboard) == 3

        row0 = kb_en_active.inline_keyboard[0]
        btn_ru = row0[0]
        btn_en = row0[1]
        btn_auto = kb_en_active.inline_keyboard[1][0]
        btn_back = kb_en_active.inline_keyboard[2][0]

        assert btn_ru.callback_data == "set_lang:ru"
        assert btn_en.callback_data == "set_lang:en"
        assert btn_auto.callback_data == "set_lang:auto"
        assert btn_back.callback_data == "settings:main"

        # English should have checkmark
        assert "✅" in btn_en.text
        assert "✅" not in btn_ru.text

    def test_quality_settings_keyboard_active_mark(self):
        """5. Quality settings keyboard displays checkmark on current preference."""
        kb_flac = E2EKeyboards.build_quality_settings_keyboard(current_pref="FLAC", lang="ru")
        assert len(kb_flac.inline_keyboard) == 4

        btn_ask = kb_flac.inline_keyboard[0][0]
        btn_mp3 = kb_flac.inline_keyboard[1][0]
        btn_flac = kb_flac.inline_keyboard[2][0]

        assert "✅" in btn_flac.text
        assert "✅" not in btn_mp3.text
        assert "✅" not in btn_ask.text


# ===========================================================================
# FEATURE 6: Progress Notifier Service (R2, R5)
# ===========================================================================
class TestFeature6ProgressNotifier:
    """Requirement R2, R5: Rate limits (TelegramRetryAfter), deleted msg, clean close."""

    def test_notifier_successful_status_edit(self, mock_bot: AsyncMock):
        """1. Normal status notify edits message with HTML parse mode."""
        async def run():
            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=123, message_id=456, lang="ru")
            await notifier.notify("status.searching_sources", artist="Queen", title="Bohemian", quality="MP3")

            mock_bot.edit_message_text.assert_awaited_once()
            call_kwargs = mock_bot.edit_message_text.call_args.kwargs
            assert call_kwargs["chat_id"] == 123
            assert call_kwargs["message_id"] == 456
            assert "Queen" in call_kwargs["text"]
            assert call_kwargs["parse_mode"] == ParseMode.HTML

        asyncio.run(run())

    def test_notifier_rate_limit_retry_after(self, mock_bot: AsyncMock):
        """2. When TelegramRetryAfter occurs, notifier backs off and retries."""
        async def run():
            retry_err = TelegramRetryAfter(
                method=MagicMock(),
                message="Too many requests",
                retry_after=0.01,
            )
            mock_bot.edit_message_text.side_effect = [retry_err, MagicMock()]

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=123, message_id=456, lang="ru")
            await notifier.notify("status.uploading")

            assert mock_bot.edit_message_text.await_count == 2

        asyncio.run(run())

    def test_notifier_suppresses_deleted_message(self, mock_bot: AsyncMock):
        """3. TelegramBadRequest('message to edit not found') is safely suppressed."""
        async def run():
            deleted_err = TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: message to edit not found",
            )
            mock_bot.edit_message_text.side_effect = deleted_err

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=123, message_id=456, lang="ru")
            await notifier.notify("status.uploading")
            assert mock_bot.edit_message_text.await_count == 1

        asyncio.run(run())

    def test_notifier_suppresses_message_not_modified(self, mock_bot: AsyncMock):
        """4. TelegramBadRequest('message is not modified') is safely ignored."""
        async def run():
            not_mod_err = TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
            )
            mock_bot.edit_message_text.side_effect = not_mod_err

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=123, message_id=456, lang="ru")
            await notifier.notify("status.uploading")
            assert mock_bot.edit_message_text.await_count == 1

        asyncio.run(run())

    def test_notifier_close_prevents_further_edits(self, mock_bot: AsyncMock):
        """5. Closed notifier ignores subsequent notifications without making API calls."""
        async def run():
            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=123, message_id=456, lang="ru")
            await notifier.close()
            assert notifier.closed is True

            await notifier.notify("status.uploading")
            mock_bot.edit_message_text.assert_not_awaited()

        asyncio.run(run())

    def test_notifier_bilingual_output(self, mock_bot: AsyncMock):
        """6. Notifier produces Russian text for 'ru' and English text for 'en'."""
        async def run():
            notifier_ru = E2EStatusNotifier(bot=mock_bot, chat_id=1, message_id=1, lang="ru")
            await notifier_ru.notify("status.uploading")
            assert "Отправка аудио" in notifier_ru.last_text

            notifier_en = E2EStatusNotifier(bot=mock_bot, chat_id=1, message_id=1, lang="en")
            await notifier_en.notify("status.uploading")
            assert "Uploading audio" in notifier_en.last_text

        asyncio.run(run())


# ===========================================================================
# FEATURE 7: Single-Instance Process Locking (R2)
# ===========================================================================
class TestFeature7SingleInstanceLock:
    """Requirement R2: Mutex lock preventing concurrent bot pollers."""

    def test_single_instance_lock_acquire_success(self, tmp_path: Path):
        """1. First instance acquires lock successfully."""
        lock_file = tmp_path / "bot_test.lock"
        lock = E2ESingleInstanceLock(lock_file=lock_file)
        acquired = lock.acquire()
        try:
            assert acquired is True
        finally:
            lock.release()

    def test_single_instance_lock_mutual_exclusion(self, tmp_path: Path):
        """2. Second instance fails to acquire same lock file."""
        lock_file = tmp_path / "bot_test.lock"
        lock1 = E2ESingleInstanceLock(lock_file=lock_file)
        lock2 = E2ESingleInstanceLock(lock_file=lock_file)

        assert lock1.acquire() is True
        try:
            assert lock2.acquire() is False
        finally:
            lock1.release()

    def test_single_instance_lock_release_and_reacquire(self, tmp_path: Path):
        """3. Releasing lock allows subsequent acquisition."""
        lock_file = tmp_path / "bot_test.lock"
        lock = E2ESingleInstanceLock(lock_file=lock_file)

        assert lock.acquire() is True
        lock.release()

        # Re-acquire
        assert lock.acquire() is True
        lock.release()

    def test_single_instance_lock_context_manager(self, tmp_path: Path):
        """4. Context manager protocol (__enter__ / __exit__) acquires and releases."""
        lock_file = tmp_path / "bot_test.lock"
        with E2ESingleInstanceLock(lock_file=lock_file) as lk:
            assert lk._acquired is True
            second_lk = E2ESingleInstanceLock(lock_file=lock_file)
            assert second_lk.acquire() is False

        third_lk = E2ESingleInstanceLock(lock_file=lock_file)
        assert third_lk.acquire() is True
        third_lk.release()

    def test_single_instance_lock_stale_file_recovery(self, tmp_path: Path):
        """5. Handles non-existent directory creation and clean state."""
        nested_dir = tmp_path / "sub" / "nested"
        lock_file = nested_dir / "bot.lock"
        lock = E2ESingleInstanceLock(lock_file=lock_file)

        assert lock.acquire() is True
        assert lock_file.exists()
        lock.release()


# ===========================================================================
# FEATURE 8: Local Bot API Server Lifecycle (R2)
# ===========================================================================
class TestFeature8LocalBotAPI:
    """Requirement R2: Local Telegram Bot API manager and lifecycle."""

    def test_local_bot_api_resolve_local_filepath(self):
        """1. Local path resolution converts telegram paths to local paths."""
        mgr = E2ELocalBotAPIManager(api_url="http://127.0.0.1:8081", local_mode=True)
        raw_path = "/var/lib/telegram-bot-api/data/file.mp3"
        resolved = mgr.resolve_local_filepath(raw_path)
        assert isinstance(resolved, Path)
        assert "file.mp3" in str(resolved)

    def test_local_bot_api_health_check_healthy(self):
        """2. Health check returns True when server is running."""
        async def run():
            mgr = E2ELocalBotAPIManager()
            await mgr.start()
            is_healthy = await mgr.check_health()
            assert is_healthy is True
            await mgr.stop()

        asyncio.run(run())

    def test_local_bot_api_health_check_unhealthy(self):
        """3. Health check returns False when server is stopped."""
        async def run():
            mgr = E2ELocalBotAPIManager()
            await mgr.stop()
            is_healthy = await mgr.check_health()
            assert is_healthy is False

        asyncio.run(run())

    def test_local_bot_api_lifecycle_start_and_stop(self):
        """4. Manager starts and stops cleanly."""
        async def run():
            mgr = E2ELocalBotAPIManager(api_url="http://127.0.0.1:8081")
            url = await mgr.start()
            assert url == "http://127.0.0.1:8081"
            assert mgr._is_running is True

            await mgr.stop()
            assert mgr._is_running is False

        asyncio.run(run())

    def test_local_bot_api_cloud_fallback_config(self):
        """5. When local_mode=False, server path is optional and defaults gracefully."""
        mgr = E2ELocalBotAPIManager(local_mode=False)
        assert mgr.local_mode is False


# ===========================================================================
# FEATURE 9: Storage Services (R2, R4)
# ===========================================================================
class TestFeature9StorageServices:
    """Requirement R2, R4: QueryStore TTL cache & UserSettings dual schema."""

    def test_query_store_save_and_get(self, query_store: E2EQueryStore):
        """1. QueryStore caches metadata and retrieves it intact."""
        data = {"artist": "Daft Punk", "title": "Get Lucky", "year": 2013}
        query_store.save("query_123", data)

        retrieved = query_store.get("query_123")
        assert retrieved is not None
        assert retrieved["artist"] == "Daft Punk"
        assert retrieved["title"] == "Get Lucky"

    def test_query_store_ttl_expiration(self):
        """2. QueryStore evicts keys when TTL expires."""
        short_store = E2EQueryStore(ttl=0)
        short_store.save("exp_1", {"key": "value"})
        time.sleep(0.01)

        assert short_store.get("exp_1") is None

    def test_user_settings_default_quality_ask(self, user_settings: E2EUserSettings):
        """3. Unconfigured users default to quality='ASK'."""
        assert user_settings.get_quality(99999) == "ASK"

    def test_user_settings_dual_schema_persistence(self, temp_settings_path: Path):
        """4. Persists both quality and language preference independently."""
        storage = E2EUserSettings(file_path=temp_settings_path)
        storage.set_quality(12345, "MP3")
        storage.set_language(12345, "en")

        # Reload
        reloaded = E2EUserSettings(file_path=temp_settings_path)
        assert reloaded.get_quality(12345) == "MP3"
        assert reloaded.get_language(12345) == "en"

    def test_user_settings_independent_updates(self, user_settings: E2EUserSettings):
        """5. Updating quality does not overwrite existing language preference."""
        user_id = 7777
        user_settings.set_language(user_id, "ru")
        user_settings.set_quality(user_id, "FLAC")

        # Update quality only
        user_settings.set_quality(user_id, "MP3")
        assert user_settings.get_quality(user_id) == "MP3"
        assert user_settings.get_language(user_id) == "ru"

        # Update language only
        user_settings.set_language(user_id, "en")
        assert user_settings.get_quality(user_id) == "MP3"
        assert user_settings.get_language(user_id) == "en"

    def test_query_store_missing_key_returns_none(self, query_store: E2EQueryStore):
        """6. Querying non-existent key returns None without error."""
        assert query_store.get("non_existent_query_id") is None
