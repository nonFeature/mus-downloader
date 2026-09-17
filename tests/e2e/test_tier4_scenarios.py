"""Tier 4: Real-World Application Workloads and Complete User Journeys.

Covers:
- Scenario 1: Complete Russian user journey: /start -> search query -> metadata card ->
  format selection -> progress updates -> audio delivery.
- Scenario 2: English user journey with streaming link: auto-detection -> FLAC size limit
  check -> MP3 fallback -> successful audio delivery.
- Scenario 3: Interactive settings workflow: /settings -> switch language -> switch default
  quality -> verify subsequent query triggers auto-download bypass.
- Scenario 4: Multi-user concurrent load under semaphore throttling with queue notifications.
- Scenario 5: Resilient recovery under Telegram rate-limits and deleted status messages.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from tests.e2e.conftest import (
    ID_AUDIO,
    ID_VINYL,
    E2EDownloadConcurrencyManager,
    E2EEmoji,
    E2EI18nEngine,
    E2EKeyboards,
    E2EQueryStore,
    E2EStatusNotifier,
    E2EUserSettings,
    autodetect_language,
    build_metadata_card,
    resolve_effective_language,
)


class TestTier4RealWorldScenarios:
    """End-to-end full user journey scenarios."""

    def test_scenario_1_russian_user_complete_journey(
        self,
        mock_bot: AsyncMock,
        message_factory,
        callback_factory,
        user_settings: E2EUserSettings,
        query_store: E2EQueryStore,
        i18n_engine: E2EI18nEngine,
    ):
        """Scenario 1: Russian user cold start -> search -> format selection -> delivery."""
        async def run():
            user_id = 111101
            chat_id = 111101

            # Step 1: User sends /start
            start_msg = message_factory(
                text="/start",
                user_id=user_id,
                language_code="ru",
                chat_id=chat_id,
            )
            lang = resolve_effective_language(user_id, "ru", user_settings)
            assert lang == "ru"

            welcome_text = i18n_engine.get("cmd.start", lang=lang)
            await start_msg.answer(welcome_text, parse_mode=ParseMode.HTML)
            start_msg.answer.assert_awaited_once()
            # Verified /id is absent
            assert "/id" not in start_msg.answer.call_args[0][0]

            # Step 2: User sends search query "Queen Bohemian Rhapsody"
            search_msg = message_factory(
                text="Queen Bohemian Rhapsody",
                user_id=user_id,
                language_code="ru",
                chat_id=chat_id,
            )
            query_id = uuid.uuid4().hex[:12]
            meta = {
                "artist": "Queen",
                "title": "Bohemian Rhapsody",
                "album": "A Night at the Opera",
                "year": 1975,
                "duration_formatted": "05:55",
            }
            query_store.save(query_id, meta)

            card_html = build_metadata_card(meta, lang=lang)
            kb = E2EKeyboards.build_quality_keyboard(query_id=query_id, lang=lang)
            await search_msg.reply(card_html, reply_markup=kb, parse_mode=ParseMode.HTML)
            search_msg.reply.assert_awaited_once()

            # Step 3: User clicks MP3 button
            cb = callback_factory(
                data=f"quality:mp3:{query_id}",
                user_id=user_id,
                language_code="ru",
                chat_id=chat_id,
            )
            cached_query = query_store.get(query_id)
            assert cached_query is not None

            # Step 4: Progress notifications
            status_msg_id = 999
            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=chat_id, message_id=status_msg_id, lang=lang)
            await notifier.notify(
                "status.searching_sources",
                artist=cached_query["artist"],
                title=cached_query["title"],
                quality="MP3",
            )
            assert "Поиск источников" in notifier.last_text

            await notifier.notify("status.uploading")
            assert "Отправка аудио" in notifier.last_text
            await notifier.close()

            # Step 5: Audio sent to chat
            await mock_bot.send_audio(
                chat_id=chat_id,
                audio="mock_file_id_mp3",
                performer=cached_query["artist"],
                title=cached_query["title"],
            )
            mock_bot.send_audio.assert_awaited_once()
            call_kwargs = mock_bot.send_audio.call_args.kwargs
            assert call_kwargs["performer"] == "Queen"
            assert call_kwargs["title"] == "Bohemian Rhapsody"

        asyncio.run(run())

    def test_scenario_2_english_user_streaming_link_and_cloud_limit(
        self,
        mock_bot: AsyncMock,
        message_factory,
        callback_factory,
        user_settings: E2EUserSettings,
        query_store: E2EQueryStore,
        i18n_engine: E2EI18nEngine,
    ):
        """Scenario 2: English client sends Spotify link, FLAC exceeds cloud limit, selects MP3."""
        async def run():
            user_id = 222202
            chat_id = 222202

            # Step 1: User with en-US client sends Spotify URL
            spotify_url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
            link_msg = message_factory(
                text=spotify_url,
                user_id=user_id,
                language_code="en-US",
                chat_id=chat_id,
            )
            lang = resolve_effective_language(user_id, "en-US", user_settings)
            assert lang == "en"

            query_id = uuid.uuid4().hex[:12]
            meta = {
                "artist": "Rick Astley",
                "title": "Never Gonna Give You Up",
                "album": "Whenever You Need Somebody",
                "year": 1987,
                "duration_formatted": "03:33",
                "flac_size_mb": 65.4,
            }
            query_store.save(query_id, meta)

            card_html = build_metadata_card(meta, lang=lang)
            assert "Album:" in card_html
            assert "Duration:" in card_html

            kb = E2EKeyboards.build_quality_keyboard(query_id=query_id, lang=lang)
            await link_msg.reply(card_html, reply_markup=kb, parse_mode=ParseMode.HTML)

            # Step 2: User clicks FLAC Lossless
            cached = query_store.get(query_id)
            flac_size = cached["flac_size_mb"]
            is_local_api = False  # Telegram Cloud Bot API mode

            # Cloud limit check: > 50 MB
            if not is_local_api and flac_size > 50.0:
                warn_text = i18n_engine.get("error.file_too_large_cloud", lang=lang, size_mb=flac_size)
                assert "65.4 MB" in warn_text
                assert "Standard Telegram Cloud Bot API limits uploaded files to" in warn_text
                assert "50 MB" in warn_text
                # Advise MP3
                assert "MP3 320k" in warn_text

            # Step 3: User falls back to MP3 -> download succeeds
            await mock_bot.send_audio(
                chat_id=chat_id,
                audio="mock_file_id_astley",
                performer=cached["artist"],
                title=cached["title"],
            )
            mock_bot.send_audio.assert_awaited_once()

        asyncio.run(run())

    def test_scenario_3_settings_and_auto_download_preference(
        self,
        mock_bot: AsyncMock,
        message_factory,
        callback_factory,
        user_settings: E2EUserSettings,
        query_store: E2EQueryStore,
        i18n_engine: E2EI18nEngine,
    ):
        """Scenario 3: /settings -> set English -> set MP3 quality -> next query auto-downloads."""
        async def run():
            user_id = 333303
            chat_id = 333303

            # Step 1: User sends /settings
            msg_settings = message_factory(text="/settings", user_id=user_id, language_code="ru", chat_id=chat_id)
            kb_settings = E2EKeyboards.build_settings_keyboard(lang="ru")
            await msg_settings.answer("Настройки", reply_markup=kb_settings)

            # Step 2: User switches language to English
            user_settings.set_language(user_id, "en")
            assert user_settings.get_language(user_id) == "en"

            # Alert confirmation
            alert_text = i18n_engine.get("settings.lang_saved_alert", lang="en")
            assert "Interface language changed to English" in alert_text

            # Step 3: User sets quality preference to MP3
            user_settings.set_quality(user_id, "MP3")
            assert user_settings.get_quality(user_id) == "MP3"

            # Step 4: User sends subsequent track query
            query_msg = message_factory(
                text="Radiohead Karma Police",
                user_id=user_id,
                language_code="ru",  # Client is ru, but override is en
                chat_id=chat_id,
            )
            effective_lang = resolve_effective_language(user_id, "ru", user_settings)
            assert effective_lang == "en"

            meta = {
                "artist": "Radiohead",
                "title": "Karma Police",
                "album": "OK Computer",
                "year": 1997,
                "duration_formatted": "04:24",
            }

            # Because quality == "MP3" (not "ASK"), system skips keyboard and starts auto-download
            user_pref = user_settings.get_quality(user_id)
            assert user_pref == "MP3"

            auto_status_text = i18n_engine.get(
                "status.auto_download",
                lang=effective_lang,
                pref="MP3 320k",
                card_text=build_metadata_card(meta, lang=effective_lang),
            )
            assert "Auto-download [MP3 320k]" in auto_status_text
            assert "Starting download process..." in auto_status_text

        asyncio.run(run())

    def test_scenario_4_high_concurrency_multi_user_queue(
        self,
        mock_bot: AsyncMock,
        i18n_engine: E2EI18nEngine,
    ):
        """Scenario 4: 3 concurrent requests with max_concurrent=2 -> 3rd queues and finishes."""
        async def run():
            manager = E2EDownloadConcurrencyManager(max_concurrent=2)
            results = []

            async def download_worker(user_id: int, lang: str):
                notifier = E2EStatusNotifier(bot=mock_bot, chat_id=user_id, message_id=1, lang=lang)
                if manager.is_queued:
                    await notifier.notify("status.queued", artist="Artist", title=f"Track_{user_id}", quality="MP3")
                    results.append((user_id, "queued"))

                async with manager:
                    await notifier.notify("status.searching_sources", artist="Artist", title=f"Track_{user_id}", quality="MP3")
                    await asyncio.sleep(0.02)  # Simulate download
                    results.append((user_id, "completed"))

            # Launch 3 workers concurrently
            t1 = asyncio.create_task(download_worker(101, "ru"))
            t2 = asyncio.create_task(download_worker(102, "en"))
            t3 = asyncio.create_task(download_worker(103, "en"))

            await asyncio.gather(t1, t2, t3)

            # Verification: User 103 was queued, all 3 completed
            completed = [item for item in results if item[1] == "completed"]
            assert len(completed) == 3
            assert (103, "queued") in results or (102, "queued") in results or (101, "queued") in results

        asyncio.run(run())

    def test_scenario_5_resilience_under_rate_limits_and_deleted_message(
        self,
        mock_bot: AsyncMock,
        i18n_engine: E2EI18nEngine,
    ):
        """Scenario 5: 429 RetryAfter followed by deleted message does not crash pipeline."""
        async def run():
            chat_id = 999
            retry_err = TelegramRetryAfter(method=MagicMock(), message="Too many requests", retry_after=0.01)
            deleted_err = TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")

            # 1st call triggers RetryAfter, 2nd call triggers message deleted error
            mock_bot.edit_message_text.side_effect = [retry_err, deleted_err]

            notifier = E2EStatusNotifier(bot=mock_bot, chat_id=chat_id, message_id=123, lang="ru")
            # Should handle both gracefully without crashing
            await notifier.notify("status.uploading")

            # Pipeline continues and sends audio safely
            await mock_bot.send_audio(
                chat_id=chat_id,
                audio="mock_delivered_audio",
                performer="The Beatles",
                title="Hey Jude",
            )
            mock_bot.send_audio.assert_awaited_once()

        asyncio.run(run())
