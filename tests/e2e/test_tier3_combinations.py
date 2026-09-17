"""Tier 3: Pairwise Combinatorial Cross-Feature Tests.

Covers cross-cutting interactions between:
- Language override (RU/EN) × Custom Emoji toggle (Enabled/Disabled) × Keyboard Builders
- Language override × Custom Emoji toggle × Metadata Card HTML Formatting
- Process locking mutex × Local Bot API lifecycle × Storage persistence
- Concurrency semaphore throttling × StatusNotifier rate limits × English localization
- Default quality setting (FLAC/MP3) × QueryStore caching × File size threshold routing
- Interactive settings modification × Dynamic keyboard checkmark updates
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from tests.e2e.conftest import (
    ID_AUDIO,
    ID_CHECK,
    ID_VINYL,
    E2EDownloadConcurrencyManager,
    E2EEmoji,
    E2EI18nEngine,
    E2EKeyboards,
    E2ELocalBotAPIManager,
    E2EQueryStore,
    E2ESingleInstanceLock,
    E2EStatusNotifier,
    E2EUserSettings,
    build_metadata_card,
    resolve_effective_language,
)


class TestTier3LanguageEmojiKeyboardCombinations:
    """Combinations of Language, Custom Emoji Mode, and Keyboards."""

    def teardown_method(self):
        E2EEmoji.set_custom_emoji_enabled(False)

    def test_en_lang_and_custom_emoji_enabled_on_quality_keyboard(self):
        """Combination 1: EN language + Custom Emoji Enabled -> icon_custom_emoji_id set, English text."""
        E2EEmoji.set_custom_emoji_enabled(True)
        kb = E2EKeyboards.build_quality_keyboard(query_id="comb_1", lang="en")

        btn_mp3 = kb.inline_keyboard[0][0]
        btn_flac = kb.inline_keyboard[0][1]

        assert btn_mp3.text == "MP3 320k"
        assert btn_mp3.icon_custom_emoji_id == ID_AUDIO
        assert btn_mp3.callback_data == "quality:mp3:comb_1"

        assert btn_flac.text == "FLAC Lossless"
        assert btn_flac.icon_custom_emoji_id == ID_VINYL
        assert btn_flac.callback_data == "quality:flac:comb_1"

    def test_ru_lang_and_custom_emoji_disabled_on_quality_keyboard(self):
        """Combination 2: RU language + Custom Emoji Disabled -> fallback emoji in text, icon is None."""
        E2EEmoji.set_custom_emoji_enabled(False)
        kb = E2EKeyboards.build_quality_keyboard(query_id="comb_2", lang="ru")

        btn_mp3 = kb.inline_keyboard[0][0]
        btn_flac = kb.inline_keyboard[0][1]

        assert "🎵 MP3 320k" in btn_mp3.text
        assert btn_mp3.icon_custom_emoji_id is None
        assert btn_mp3.callback_data == "quality:mp3:comb_2"

        assert "💿 FLAC Lossless" in btn_flac.text
        assert btn_flac.icon_custom_emoji_id is None
        assert btn_flac.callback_data == "quality:flac:comb_2"

    def test_ru_lang_and_custom_emoji_enabled_on_metadata_card(self):
        """Combination 3: RU language + Custom Emoji Enabled -> <tg-emoji> tags with Russian card labels."""
        E2EEmoji.set_custom_emoji_enabled(True)
        data = {
            "artist": "Кино",
            "title": "Группа крови",
            "album": "Группа крови",
            "year": 1988,
            "duration_formatted": "04:45",
        }
        card = build_metadata_card(data, lang="ru")

        # Custom emoji tags present
        assert f'<tg-emoji emoji-id="{ID_AUDIO}">🎵</tg-emoji>' in card
        assert f'<tg-emoji emoji-id="{ID_VINYL}">💿</tg-emoji>' in card
        # Russian labels present
        assert "Альбом:" in card
        assert "Год:" in card
        assert "Длительность:" in card

    def test_en_lang_and_custom_emoji_disabled_on_metadata_card(self):
        """Combination 4: EN language + Custom Emoji Disabled -> pure Unicode with English card labels."""
        E2EEmoji.set_custom_emoji_enabled(False)
        data = {
            "artist": "Queen",
            "title": "Bohemian Rhapsody",
            "album": "A Night at the Opera",
            "year": 1975,
            "duration_formatted": "05:55",
        }
        card = build_metadata_card(data, lang="en")

        # No <tg-emoji> tags
        assert "<tg-emoji" not in card
        assert "🎵" in card
        assert "💿" in card
        # English labels present
        assert "Album:" in card
        assert "Year:" in card
        assert "Duration:" in card


class TestTier3ProcessLockLocalServerAndStorage:
    """Combinations of Process Locking, Local Bot API Lifecycle, and Storage."""

    def test_lock_contention_with_running_local_server_and_storage(self, tmp_path: Path):
        """Combination 5: Lock holder runs local server & saves to storage; second instance is rejected."""
        async def run():
            lock_file = tmp_path / "bot_comb.lock"
            settings_file = tmp_path / "settings_comb.json"

            # Primary instance setup
            primary_lock = E2ESingleInstanceLock(lock_file=lock_file)
            assert primary_lock.acquire() is True

            primary_server = E2ELocalBotAPIManager(api_url="http://127.0.0.1:8085")
            server_url = await primary_server.start()
            assert server_url == "http://127.0.0.1:8085"

            primary_storage = E2EUserSettings(file_path=settings_file)
            primary_storage.set_quality(555, "FLAC")
            primary_storage.set_language(555, "ru")

            # Secondary instance attempts to acquire same lock
            secondary_lock = E2ESingleInstanceLock(lock_file=lock_file)
            assert secondary_lock.acquire() is False

            # Primary instance cleans up and shuts down
            await primary_server.stop()
            primary_lock.release()

            # Now secondary instance can acquire lock and access persisted storage
            assert secondary_lock.acquire() is True
            secondary_storage = E2EUserSettings(file_path=settings_file)
            assert secondary_storage.get_quality(555) == "FLAC"
            assert secondary_storage.get_language(555) == "ru"
            secondary_lock.release()

        asyncio.run(run())


class TestTier3ConcurrencyNotifierAndLanguage:
    """Combinations of Concurrency Throttling, Notifier Rate Limits, and Localization."""

    def test_concurrency_throttling_with_queued_en_notification(self, mock_bot: AsyncMock):
        """Combination 6: Semaphore saturation triggers English queued status, then proceeds to dl."""
        async def run():
            manager = E2EDownloadConcurrencyManager(max_concurrent=1)
            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=1, message_id=10, lang="en")

            # Slot 1 acquired by first job
            slot1 = await manager.acquire()
            assert manager.active_count == 1
            assert manager.is_queued is True

            # Second job detects queue and sends localized EN queued status
            await notifier.notify(
                "status.queued",
                artist="Pink Floyd",
                title="Time",
                quality="FLAC",
            )
            assert "Download queue: waiting for available slot..." in notifier.last_text
            assert "Pink Floyd" in notifier.last_text

            # Job 1 finishes
            slot1.release()
            assert manager.active_count == 0

            # Job 2 now proceeds to active download
            slot2 = await manager.acquire()
            await notifier.notify(
                "status.searching_sources",
                artist="Pink Floyd",
                title="Time",
                quality="FLAC",
            )
            assert "Searching sources..." in notifier.last_text
            slot2.release()

        asyncio.run(run())

    def test_notifier_rate_limit_during_multi_step_progress(self, mock_bot: AsyncMock):
        """Combination 7: Sequence of progress updates with 429 RetryAfter under Russian locale."""
        async def run():
            retry_err = TelegramRetryAfter(method=MagicMock(), message="Rate limit", retry_after=0.01)
            # Fail once on 2nd step, succeed on retry
            mock_bot.edit_message_text.side_effect = [
                MagicMock(),            # Step 1 succeeds
                retry_err, MagicMock(), # Step 2 retries and succeeds
                MagicMock(),            # Step 3 succeeds
            ]

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=12, message_id=34, lang="ru")

            # 1. Searching metadata
            await notifier.notify("status.searching_meta")
            assert "Поиск метаданных" in notifier.last_text

            # 2. Searching sources
            await notifier.notify("status.searching_sources", artist="Nirvana", title="In Bloom", quality="MP3")
            assert "Поиск источников" in notifier.last_text

            # 3. Uploading
            await notifier.notify("status.uploading")
            assert "Отправка аудио" in notifier.last_text

            assert mock_bot.edit_message_text.await_count == 4

        asyncio.run(run())


class TestTier3QualitySettingsCacheAndCloudLimits:
    """Combinations of User Quality Settings, QueryStore, and File Size Cloud Routing."""

    def test_cached_query_flac_pref_exceeding_cloud_size_shows_warning(self, tmp_path: Path):
        """Combination 8: User wants FLAC, cached query is 85 MB, cloud mode prompts MP3 fallback."""
        settings = E2EUserSettings(file_path=tmp_path / "settings_limit.json")
        settings.set_quality(9001, "FLAC")
        settings.set_language(9001, "en")

        store = E2EQueryStore(ttl=3600)
        query_data = {
            "artist": "Led Zeppelin",
            "title": "Stairway to Heaven",
            "flac_size_mb": 85.5,
            "mp3_size_mb": 18.2,
        }
        store.save("query_zep", query_data)

        # Retrieve and evaluate
        cached = store.get("query_zep")
        assert cached is not None

        user_pref = settings.get_quality(9001)
        assert user_pref == "FLAC"

        is_cloud_api = True  # Cloud 50MB limit
        if is_cloud_api and cached["flac_size_mb"] > 50.0:
            engine = E2EI18nEngine()
            err_msg = engine.get("error.file_too_large_cloud", lang="en", size_mb=cached["flac_size_mb"])
            assert "85.5 MB" in err_msg
            assert "50 MB" in err_msg
            assert "MP3 320k" in err_msg

    def test_interactive_language_change_updates_keyboard_active_checkmark(self, tmp_path: Path):
        """Combination 9: Changing language setting from RU to EN updates active checkmark."""
        settings = E2EUserSettings(file_path=tmp_path / "settings_lang_switch.json")
        user_id = 4444

        # Initial default: None -> Auto selected
        kb_initial = E2EKeyboards.build_language_keyboard(current_pref=None, lang="ru")
        btn_auto_init = kb_initial.inline_keyboard[1][0]
        assert "✅" in btn_auto_init.text

        # User selects English
        settings.set_language(user_id, "en")
        kb_after = E2EKeyboards.build_language_keyboard(current_pref="en", lang="en")
        btn_ru_after = kb_after.inline_keyboard[0][0]
        btn_en_after = kb_after.inline_keyboard[0][1]
        btn_auto_after = kb_after.inline_keyboard[1][0]

        assert "✅" in btn_en_after.text
        assert "✅" not in btn_ru_after.text
        assert "✅" not in btn_auto_after.text
        assert "English" in btn_en_after.text
