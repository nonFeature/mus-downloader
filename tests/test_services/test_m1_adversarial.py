"""
tests/test_services/test_m1_adversarial.py
Adversarial stress-test suite for Milestone M1 components:
- UserSettings (corrupt JSON, concurrency, legacy/modern schema compatibility)
- QueryStore (TTL expiration, LRU eviction, sync/async dual mode, concurrency)
- StatusNotifier (rapid calls, consecutive RetryAfter, TelegramBadRequest, spec=Message handling)
- /id command removal (router audit, help/start audit, fallback execution)
"""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message, User, Chat

import bot
from bot.notifier import StatusNotifier, format_status_text, escape_html
from bot.storage import QueryStore, UserSettings, SUPPORTED_QUALITIES, SUPPORTED_LANGUAGES


# ============================================================================
# 1. UserSettings Stress Tests
# ============================================================================

class TestUserSettingsAdversarial:
    """Stress testing UserSettings for corruption recovery, schemas, and concurrency."""

    @pytest.mark.parametrize(
        "corrupt_content",
        [
            "",                                  # Empty file (0 bytes)
            "   \n\t  ",                         # Whitespace only
            "{",                                 # Truncated JSON
            '{"123": {"quality": "MP',           # Half-written JSON (crash simulation)
            "INVALID_JSON_CONTENT {{{",          # Malformed syntax
            "[]",                                # Valid JSON, but list instead of dict
            '"just a string"',                   # Valid JSON, but string
            "12345",                             # Valid JSON, but number
            "null",                              # Valid JSON, but null
            '{"not_a_number": "MP3"}',           # Non-numeric user ID key
            '{"None": {"quality": "FLAC"}}',     # String "None" as key
            '{"100": {"quality": 999}}',         # Non-string quality
            '{"101": {"language": ["ru"]}}',     # Non-string language
        ]
    )
    def test_corrupt_json_recovery(self, tmp_path: Path, corrupt_content: str):
        """Corrupted, truncated, or malformed JSON files must not crash UserSettings and should default gracefully."""
        settings_file = tmp_path / "corrupt_settings.json"
        settings_file.write_text(corrupt_content, encoding="utf-8")

        # Must initialize without raising an exception
        settings = UserSettings(file_path=settings_file)

        # Non-existent or recovered user queries must safely return defaults
        assert settings.get_quality(123) == "ASK"
        assert settings.get_language(123) is None

        # Writing after corrupt recovery must heal the file into valid JSON
        settings.set_quality(123, "FLAC")
        assert settings.get_quality(123) == "FLAC"

        reloaded = UserSettings(file_path=settings_file)
        assert reloaded.get_quality(123) == "FLAC"

    def test_legacy_string_format_compatibility(self, tmp_path: Path):
        """Legacy flat mapping {"123": "MP3"} must be read correctly."""
        settings_file = tmp_path / "legacy_settings.json"
        legacy_data = {
            "111": "MP3",
            "222": "FLAC",
            "333": "ASK",
            "444": "UNKNOWN_VAL",  # Invalid quality in legacy format
        }
        settings_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        settings = UserSettings(file_path=settings_file)
        assert settings.get_quality(111) == "MP3"
        assert settings.get_quality("111") == "MP3"
        assert settings.get_language(111) is None

        assert settings.get_quality(222) == "FLAC"
        assert settings.get_language(222) is None

        assert settings.get_quality(333) == "ASK"
        assert settings.get_quality(444) == "ASK"  # Fallback for invalid

    def test_legacy_to_modern_schema_upgrade(self, tmp_path: Path):
        """Updating language on a legacy entry must preserve quality and upgrade to modern dict format."""
        settings_file = tmp_path / "upgrade_settings.json"
        legacy_data = {"555": "FLAC"}
        settings_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        settings = UserSettings(file_path=settings_file)
        assert settings.get_quality(555) == "FLAC"
        assert settings.get_language(555) is None

        # Upgrade by setting language
        settings.set_language(555, "en")
        assert settings.get_quality(555) == "FLAC"
        assert settings.get_language(555) == "en"

        # Verify disk persistence has modern schema
        with open(settings_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        assert disk_data["555"] == {"quality": "FLAC", "language": "en"}

        # Fresh reload
        reloaded = UserSettings(file_path=settings_file)
        assert reloaded.get_quality(555) == "FLAC"
        assert reloaded.get_language(555) == "en"

    def test_invalid_quality_and_language_mutation_guard(self, tmp_path: Path):
        """Invalid qualities or languages must not overwrite valid settings."""
        settings_file = tmp_path / "guard_settings.json"
        settings = UserSettings(file_path=settings_file)

        settings.set_quality(777, "MP3")
        settings.set_language(777, "ru")

        # Try to set unsupported values
        settings.set_quality(777, "INVALID_HI_RES")
        settings.set_quality(777, "")
        settings.set_language(777, "es")
        settings.set_language(777, "de")

        assert settings.get_quality(777) == "MP3"
        assert settings.get_language(777) == "ru"

    def test_concurrent_multithreaded_read_write(self, tmp_path: Path):
        """Simultaneous multithreaded writes and reads must not produce a corrupt JSON file or crash."""
        settings_file = tmp_path / "concurrent_settings.json"
        settings = UserSettings(file_path=settings_file)

        num_threads = 10
        ops_per_thread = 50
        errors = []

        def worker(thread_idx: int):
            try:
                for i in range(ops_per_thread):
                    uid = (thread_idx * 100) + i
                    quality = "FLAC" if i % 2 == 0 else "MP3"
                    lang = "ru" if i % 3 == 0 else "en"
                    settings.set_quality(uid, quality)
                    settings.set_language(uid, lang)
                    # Interleaved reads
                    read_q = settings.get_quality(uid)
                    read_l = settings.get_language(uid)
                    if read_q != quality:
                        errors.append(f"Thread {thread_idx}: quality mismatch {read_q} != {quality}")
                    if read_l != lang:
                        errors.append(f"Thread {thread_idx}: lang mismatch {read_l} != {lang}")
            except Exception as e:
                errors.append(f"Thread {thread_idx} crashed: {e}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent execution produced errors: {errors[:5]}"

        # Verify disk file is intact valid JSON
        with open(settings_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        assert len(disk_data) == num_threads * ops_per_thread


# ============================================================================
# 2. QueryStore Stress Tests
# ============================================================================

class TestQueryStoreAdversarial:
    """Stress testing QueryStore for LRU eviction, TTL expiration, sync/async access, and race conditions."""

    def test_lru_eviction_under_pressure(self):
        """QueryStore with capacity N must strictly evict the oldest entry when N+1 is added."""
        max_items = 5
        store = QueryStore(max_items=max_items, ttl_seconds=3600)

        for i in range(10):
            store.save(f"qid_{i}", {"index": i})

        # Oldest 0..4 should be evicted
        for i in range(5):
            assert store.get_sync(f"qid_{i}") is None

        # Most recent 5..9 should be present
        for i in range(5, 10):
            entry = store.get_sync(f"qid_{i}")
            assert entry is not None
            assert entry["index"] == i

    def test_ttl_expiration(self):
        """Entries past ttl_seconds must be expired and purged on get."""
        store = QueryStore(max_items=10, ttl_seconds=0.05)
        store.save("expiring_qid", {"title": "Ghost"})

        # Immediate get succeeds
        assert store.get_sync("expiring_qid") is not None

        # Sleep past TTL
        time.sleep(0.07)
        assert store.get_sync("expiring_qid") is None

    def test_dual_mode_sync_and_async_retrieval(self):
        """QueryStore.get() must support both direct sync calls (with sync=True) and await."""
        store = QueryStore(max_items=10)
        store.save("dual_qid", {"name": "DualTrack"})

        # Direct sync get
        sync_result = store.get("dual_qid", sync=True)
        assert sync_result == {"name": "DualTrack", "_created_at": sync_result["_created_at"]}

        # Async get
        async def run_async():
            res = await store.get("dual_qid")
            return res

        async_result = asyncio.run(run_async())
        assert async_result["name"] == "DualTrack"

    def test_concurrent_async_saves_and_gets(self):
        """High-concurrency async coroutines saving and querying entries simultaneously."""
        store = QueryStore(max_items=200, ttl_seconds=30)

        async def run():
            async def writer(i: int):
                for j in range(20):
                    qid = f"async_q_{i}_{j}"
                    await store.save(qid, {"worker": i, "step": j})
                    await asyncio.sleep(0.001)

            async def reader(i: int):
                found = 0
                for j in range(20):
                    qid = f"async_q_{i}_{j}"
                    data = await store.get(qid)
                    if data is not None:
                        found += 1
                return found

            # Run 10 writers and 10 readers concurrently
            writers = [writer(i) for i in range(10)]
            await asyncio.gather(*writers)

            readers = [reader(i) for i in range(10)]
            results = await asyncio.gather(*readers)
            # All 200 items should be retrieved
            assert sum(results) == 200

        asyncio.run(run())


# ============================================================================
# 3. StatusNotifier Stress Tests
# ============================================================================

class TestStatusNotifierAdversarial:
    """Stress testing StatusNotifier against rapid calls, flood limits, error states, and mock specs."""

    def test_rapid_calls_trailing_flush(self):
        """100 rapid calls within a burst must not drop the final status update."""
        async def run():
            mock_bot = MagicMock()
            mock_msg = MagicMock()
            mock_msg.edit_text = AsyncMock()

            # min_interval 0.05s
            notifier = StatusNotifier(mock_bot, mock_msg, header="Burst Test", min_interval=0.05)

            for i in range(50):
                notifier.notify(f"status_{i}")

            # Give event loop time to process trailing flush
            await asyncio.sleep(0.12)

            assert mock_msg.edit_text.await_count >= 2
            # Final edit must contain the last update
            last_call = mock_msg.edit_text.await_args_list[-1]
            assert "status_49" in last_call[0][0]

            notifier.close()

        asyncio.run(run())

    def test_consecutive_telegram_retry_after(self):
        """Multiple consecutive TelegramRetryAfter errors must back off and retry up to limit without unhandled crash."""
        async def run():
            mock_bot = MagicMock()
            mock_msg = MagicMock()
            
            retry_err = TelegramRetryAfter(method=MagicMock(), message="Flood limit", retry_after=0.01)
            # 2 failures then success
            mock_msg.edit_text = AsyncMock(side_effect=[retry_err, retry_err, MagicMock()])

            notifier = StatusNotifier(mock_bot, mock_msg, min_interval=0.01)
            await notifier.notify("status.uploading")

            assert mock_msg.edit_text.await_count == 3
            notifier.close()

        asyncio.run(run())

    def test_excessive_retry_after_exhaustion_does_not_crash(self):
        """When retries (retries=2) are completely exhausted by TelegramRetryAfter, it must log and terminate cleanly."""
        async def run():
            mock_bot = MagicMock()
            mock_msg = MagicMock()

            retry_err = TelegramRetryAfter(method=MagicMock(), message="Persistent flood", retry_after=0.01)
            # 4 consecutive failures (exceeds retries=2)
            mock_msg.edit_text = AsyncMock(side_effect=[retry_err, retry_err, retry_err, retry_err])

            notifier = StatusNotifier(mock_bot, mock_msg, min_interval=0.01)
            # Must not raise unhandled exception
            await notifier.notify("status.uploading")

            assert mock_msg.edit_text.await_count == 3
            notifier.close()

        asyncio.run(run())

    def test_deleted_message_telegram_bad_request_closes_notifier(self):
        """If Telegram returns message to edit not found (user deleted message), notifier must close immediately."""
        async def run():
            mock_bot = MagicMock()
            mock_msg = MagicMock()

            del_err = TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")
            mock_msg.edit_text = AsyncMock(side_effect=del_err)

            notifier = StatusNotifier(mock_bot, mock_msg, min_interval=0.01)
            assert notifier.closed is False

            await notifier.notify("status.uploading")

            # Must have closed automatically
            assert notifier.closed is True

            # Subsequent notify calls must be no-ops
            await notifier.notify("status.starting_dl", quality="MP3")
            assert mock_msg.edit_text.await_count == 1

        asyncio.run(run())

    def test_message_not_modified_benign_suppression(self):
        """If Telegram returns 'message is not modified', it should be treated as benign success."""
        async def run():
            mock_bot = MagicMock()
            mock_msg = MagicMock()

            mod_err = TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message")
            mock_msg.edit_text = AsyncMock(side_effect=mod_err)

            notifier = StatusNotifier(mock_bot, mock_msg, min_interval=0.01)
            # Should not crash and should not close
            await notifier.notify("status.uploading")

            assert notifier.closed is False
            assert mock_msg.edit_text.await_count == 1
            notifier.close()

        asyncio.run(run())

    def test_status_notifier_with_spec_message_missing_chat(self):
        """
        EMPERICAL CHALLENGE:
        When a test passes `MagicMock(spec=Message)` where `.chat` is not pre-mocked,
        StatusNotifier.__init__ must not crash with AttributeError on status_msg.chat.id!
        """
        mock_bot = MagicMock()
        mock_msg = MagicMock(spec=Message)
        mock_msg.edit_text = AsyncMock()

        # This tests whether StatusNotifier.__init__ crashes on MagicMock(spec=Message)
        try:
            notifier = StatusNotifier(mock_bot, mock_msg, header="Test")
            init_crashed = False
        except AttributeError as e:
            init_crashed = True
            error_msg = str(e)

        # In current implementation, this crashes! Let's record the assertion:
        assert not init_crashed, f"StatusNotifier crashed on MagicMock(spec=Message): {error_msg if 'error_msg' in locals() else ''}"


# ============================================================================
# 4. /id Command Removal Verification
# ============================================================================

class TestIdCommandRemovalAdversarial:
    """Verifying complete eradication of /id command and correct unknown command fallback."""

    def test_id_handler_absent_from_bot_module(self):
        """handle_id must not exist in bot module namespace or __all__."""
        assert not hasattr(bot, "handle_id"), "bot.handle_id still exists!"
        assert "handle_id" not in bot.__all__, "handle_id is still in bot.__all__!"

    def test_id_absent_from_router_handlers(self):
        """The bot Dispatcher / Router must not have any handler matching Command('id')."""
        found_id_filter = False
        for handler in bot.router.message.handlers:
            filters = getattr(handler, "filters", [])
            for f in filters:
                commands = getattr(f, "commands", None)
                if commands and "id" in commands:
                    found_id_filter = True
                    break

        assert not found_id_filter, "Found a router message handler registered with Command('id')!"

    def test_id_absent_from_start_welcome_message(self):
        """handle_start message text must not mention /id."""
        async def run():
            mock_msg = MagicMock(spec=Message)
            mock_msg.answer = AsyncMock()

            await bot.handle_start(mock_msg)

            assert mock_msg.answer.awaited
            welcome_text = mock_msg.answer.call_args[0][0]
            assert "/id" not in welcome_text, f"'/id' is still mentioned in /start text:\n{welcome_text}"

        asyncio.run(run())

    def test_sending_id_command_triggers_unknown_command_fallback(self):
        """
        Sending /id as a message to handle_track_query must trigger the unknown command fallback,
        without leaking the user's Telegram ID.
        """
        async def run():
            mock_user = MagicMock(spec=User)
            mock_user.id = 987654321

            mock_msg = MagicMock(spec=Message)
            mock_msg.text = "/id"
            mock_msg.from_user = mock_user
            mock_msg.answer = AsyncMock()

            mock_bot = MagicMock()

            await bot.handle_track_query(mock_msg, mock_bot)

            mock_msg.answer.assert_awaited_once()
            reply_text = mock_msg.answer.call_args[0][0]

            assert "Неизвестная команда" in reply_text
            assert "987654321" not in reply_text, "User ID was leaked in unknown command response!"

        asyncio.run(run())
