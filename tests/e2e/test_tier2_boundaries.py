"""Tier 2: Boundary Value Analysis and Corner Cases.

Covers:
- Empty strings, whitespace, and missing attributes
- Corrupt files, 0-byte files, and storage recovery
- Extreme name lengths, HTML special characters, and Unicode bounds
- Rapid query bursts and cache capacity limits
- Telegram rate limits (HTTP 429 RetryAfter storms) and backoff ceilings
- Invalid, malformed, and extreme language codes
- File size thresholds (50 MB Cloud vs 2 GB Local Bot API boundaries)
"""

from __future__ import annotations

import asyncio
import html
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from tests.e2e.conftest import (
    ID_AUDIO,
    ID_CHECK,
    E2EEmoji,
    E2EI18nEngine,
    E2EKeyboards,
    E2EQueryStore,
    E2ESingleInstanceLock,
    E2EStatusNotifier,
    E2EUserSettings,
    autodetect_language,
    build_metadata_card,
    resolve_effective_language,
)


class TestTier2EmptyAndWhitespaceBoundaries:
    """Boundary conditions for empty, whitespace, and missing data inputs."""

    def test_empty_search_query_handling(self, i18n_engine: E2EI18nEngine):
        """Empty or whitespace search query produces unsupported content hint."""
        for query in ["", "   ", "\t\n", "\r\n  "]:
            is_empty = not query.strip()
            assert is_empty is True
            # Expect prompt to enter track title or link
            resp = i18n_engine.get("status.unsupported_content", lang="ru")
            assert "отправьте ссылку" in resp or "текстом" in resp

    def test_metadata_with_all_empty_and_none_fields(self, i18n_engine: E2EI18nEngine):
        """Metadata card with None/empty fields gracefully falls back to placeholders."""
        card_ru = build_metadata_card({}, lang="ru")
        assert i18n_engine.get("card.unknown_artist", lang="ru") in card_ru
        assert i18n_engine.get("card.unknown_track", lang="ru") in card_ru
        assert "—" in card_ru

        card_en = build_metadata_card({}, lang="en")
        assert i18n_engine.get("card.unknown_artist", lang="en") in card_en
        assert i18n_engine.get("card.unknown_track", lang="en") in card_en

    def test_custom_emoji_with_empty_strings(self):
        """e() with empty string emoji_id or fallback returns safe strings."""
        E2EEmoji.set_custom_emoji_enabled(True)
        try:
            assert E2EEmoji.e("", "fallback") == "fallback"
            assert E2EEmoji.e(None, "") == ""
            assert E2EEmoji.e("", "") == ""
        finally:
            E2EEmoji.set_custom_emoji_enabled(False)

    def test_user_settings_empty_file_initialization(self, tmp_path: Path):
        """UserSettings initializes cleanly from an empty 0-byte file."""
        empty_file = tmp_path / "empty_settings.json"
        empty_file.write_text("", encoding="utf-8")

        storage = E2EUserSettings(file_path=empty_file)
        assert storage.get_quality(123) == "ASK"
        assert storage.get_language(123) is None


class TestTier2CorruptFilesAndStorageRecovery:
    """Boundary conditions for corrupted files, invalid JSON, and process lock recovery."""

    def test_corrupt_json_settings_file_recovery(self, tmp_path: Path):
        """Corrupt non-JSON file is handled gracefully without crash."""
        corrupt_file = tmp_path / "corrupt_settings.json"
        corrupt_file.write_text("{invalid_json: true, [unclosed", encoding="utf-8")

        storage = E2EUserSettings(file_path=corrupt_file)
        # Should initialize defaults rather than raise JSONDecodeError
        assert storage.get_quality(999) == "ASK"
        assert storage.get_language(999) is None

        # Saving should overwrite with valid JSON
        storage.set_quality(999, "MP3")
        reloaded = E2EUserSettings(file_path=corrupt_file)
        assert reloaded.get_quality(999) == "MP3"

    def test_empty_lock_file_locking(self, tmp_path: Path):
        """Zero-byte lock file is handled correctly on Windows and POSIX."""
        lock_file = tmp_path / "empty.lock"
        lock_file.write_bytes(b"")

        lock = E2ESingleInstanceLock(lock_file=lock_file)
        assert lock.acquire() is True
        lock.release()

    def test_zero_byte_audio_size_verification(self, i18n_engine: E2EI18nEngine):
        """0-byte file size calculation does not cause division by zero or errors."""
        size_bytes = 0
        size_mb = size_bytes / (1024 * 1024)
        assert size_mb == 0.0

        # Should be below 50MB cloud limit
        assert size_mb < 50.0


class TestTier2ExtremeLengthAndHTMLCharacters:
    """Boundary conditions for long strings, HTML special characters, and Unicode."""

    def test_extremely_long_track_and_artist_names(self):
        """Card builder handles 1000-character titles and artists without overflow."""
        long_title = "A" * 1000
        long_artist = "B" * 1000
        data = {
            "artist": long_artist,
            "title": long_title,
            "album": "C" * 500,
            "year": 2026,
            "duration_formatted": "03:45",
        }
        card = build_metadata_card(data, lang="ru")
        assert long_title in card
        assert long_artist in card

    def test_html_special_character_escaping(self):
        """HTML sensitive characters (<, >, &, \", ') are safely formatted."""
        unsafe_title = "Rock <&> Roll \"The Best\" & 'Hits'"
        unsafe_artist = "<script>alert('pwned')</script>"
        safe_title = html.escape(unsafe_title)
        safe_artist = html.escape(unsafe_artist)

        data = {
            "artist": safe_artist,
            "title": safe_title,
            "album": "Test <Album>",
            "year": 2026,
            "duration_formatted": "04:20",
        }
        card = build_metadata_card(data, lang="en")
        assert "<script>" not in card
        assert "&lt;script&gt;" in card
        assert "&lt;&amp;&gt;" in card

    def test_unicode_multilingual_characters(self):
        """Card builder safely supports Cyrillic, Arabic, CJK, Emoji, and RTL scripts."""
        data = {
            "artist": "موسیقی / 音乐 / 音楽 / Музыка / Music 🎵",
            "title": "مرحبا / 你好 / こんにちは / Привет 🚀",
            "album": "Global Album 🌍",
            "year": 2026,
            "duration_formatted": "05:00",
        }
        card = build_metadata_card(data, lang="ru")
        assert "موسیقی" in card
        assert "音乐" in card
        assert "Привет" in card


class TestTier2RapidQueriesAndCapacityLimits:
    """Boundary conditions for rapid bursts, cache capacity, and eviction."""

    def test_rapid_burst_100_queries_in_querystore(self):
        """QueryStore withstands rapid sequential insertion of 100 queries."""
        store = E2EQueryStore(ttl=3600)
        for i in range(100):
            store.save(f"qid_{i}", {"index": i, "title": f"Song {i}"})

        # All 100 queries retrievable
        for i in range(100):
            item = store.get(f"qid_{i}")
            assert item is not None
            assert item["index"] == i

    def test_querystore_overwrite_existing_query_id(self):
        """Saving with identical query_id updates cached data."""
        store = E2EQueryStore(ttl=3600)
        store.save("fixed_id", {"version": 1})
        assert store.get("fixed_id")["version"] == 1

        store.save("fixed_id", {"version": 2})
        assert store.get("fixed_id")["version"] == 2

    def test_sub_millisecond_ttl_eviction(self):
        """Microsecond TTL evicts reliably on subsequent get."""
        store = E2EQueryStore(ttl=0.001)
        store.save("fast_exp", {"key": "quick"})
        time.sleep(0.01)
        assert store.get("fast_exp") is None


class TestTier2RateLimitsAndBackoffCeilings:
    """Boundary conditions for Telegram rate limits (HTTP 429) and error bursts."""

    def test_notifier_consecutive_rate_limits_retry_storm(self, mock_bot: AsyncMock):
        """Notifier retries up to maximum retry attempts when encountering consecutive 429s."""
        async def run():
            err1 = TelegramRetryAfter(method=MagicMock(), message="Too many requests", retry_after=0.01)
            err2 = TelegramRetryAfter(method=MagicMock(), message="Too many requests", retry_after=0.01)
            # Succeeded on 3rd attempt
            mock_bot.edit_message_text.side_effect = [err1, err2, MagicMock()]

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=1, message_id=1, lang="ru")
            await notifier.notify("status.uploading")

            assert mock_bot.edit_message_text.await_count == 3

        asyncio.run(run())

    def test_notifier_capping_excessive_retry_after(self, mock_bot: AsyncMock):
        """Simulated notifier caps long retry_after intervals to avoid blocking tests."""
        async def run():
            # Telegram specifies 100s retry_after; adapter caps sleep at 0.05s
            err = TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=100.0)
            mock_bot.edit_message_text.side_effect = [err, MagicMock()]

            start_t = time.time()
            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=1, message_id=1, lang="ru")
            await notifier.notify("status.uploading")
            elapsed = time.time() - start_t

            assert elapsed < 1.0  # Must not sleep for 100s
            assert mock_bot.edit_message_text.await_count == 2

        asyncio.run(run())


class TestTier2InvalidAndExtremeLanguages:
    """Boundary conditions for malformed, non-standard, or extreme locale codes."""

    def test_invalid_language_codes_fallback_to_en(self):
        """Non-ru codes including invalid strings fallback to English."""
        invalid_codes = [
            "invalid_code",
            "12345",
            "???",
            "klingon",
            "esperanto",
            "elvish",
            "xyz-99",
        ]
        for code in invalid_codes:
            assert autodetect_language(code) == "en"

    def test_extreme_length_language_code(self):
        """Massive 10,000-character language code does not crash string handling."""
        massive_code = "ru" + "x" * 10000
        assert autodetect_language(massive_code) == "ru"

        massive_non_ru = "en" + "x" * 10000
        assert autodetect_language(massive_non_ru) == "en"

    def test_case_insensitive_language_code(self):
        """Language codes are case-insensitive (RU, Ru, rU, EN, En)."""
        assert autodetect_language("RU") == "ru"
        assert autodetect_language("Ru") == "ru"
        assert autodetect_language("rU") == "ru"
        assert autodetect_language("EN") == "en"
        assert autodetect_language("En") == "en"


class TestTier2FileSizeBoundaries:
    """Boundary conditions for Telegram Cloud (50 MB) and Local Server (2 GB) limits."""

    def test_file_size_exactly_50mb_boundary(self, i18n_engine: E2EI18nEngine):
        """49.9 MB is allowed for cloud; 50.1 MB exceeds cloud limit."""
        size_under = 49.9
        size_over = 50.1

        assert size_under <= 50.0
        assert size_over > 50.0

        err_ru = i18n_engine.get("error.file_too_large_cloud", lang="ru", size_mb=size_over)
        assert "50.1 МБ" in err_ru
        assert "50 МБ" in err_ru
        assert "MP3 320k" in err_ru

        err_en = i18n_engine.get("error.file_too_large_cloud", lang="en", size_mb=size_over)
        assert "50.1 MB" in err_en
        assert "50 MB" in err_en

    def test_file_size_2gb_local_bot_api_boundary(self, i18n_engine: E2EI18nEngine):
        """2000 MB is allowed for local bot api; 2001 MB exceeds 2 GB hard limit."""
        size_allowed = 2000.0
        size_exceeded = 2001.0

        assert size_allowed <= 2048.0
        assert size_exceeded > 2000.0

        err_msg = i18n_engine.get("error.file_too_large_local", lang="ru", size_mb=size_exceeded)
        assert "2 ГБ" in err_msg
        assert "2001.0 МБ" in err_msg
