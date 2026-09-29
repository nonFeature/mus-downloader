"""
Tests for the Telegram file_id cache.

The suite disables it via ``FILEID_CACHE_ENABLED=0`` in conftest, so these
tests build their own :class:`FileIdCache` instances on tmp paths.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

import bot as bot_module
import config
from bot.fileid_cache import (
    FileIdCache,
    build_keys,
    is_invalid_file_id_error,
    reset_file_id_cache,
)

META = {"artist": "Daft Punk", "title": "Get Lucky", "isrc": "USRC17607839", "duration": 248}


@pytest.fixture
def cache(tmp_path):
    instance = FileIdCache(path=tmp_path / "fileid.db", enabled=True)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# build_keys
# --------------------------------------------------------------------------

def test_build_keys_orders_strongest_first():
    keys = build_keys(META, "FLAC", url="https://x.example/track")
    assert keys[0].startswith("isrc:")
    assert any(k.startswith("track:") for k in keys)
    assert any(k.startswith("url:") for k in keys)


def test_build_keys_always_includes_quality():
    """FLAC и MP3 - разные файлы, отдавать одно вместо другого нельзя."""
    flac = build_keys(META, "FLAC")
    mp3 = build_keys(META, "MP3")
    assert all(k.endswith(":FLAC") for k in flac)
    assert all(k.endswith(":MP3") for k in mp3)
    assert not set(flac) & set(mp3)


def test_build_keys_isrc_is_normalised():
    a = build_keys({"artist": "A", "title": "B", "isrc": "us-rc1-99-00001"}, "MP3")
    b = build_keys({"artist": "X", "title": "Y", "isrc": "USRC19900001"}, "MP3")
    assert a[0] == b[0]


def test_build_keys_empty_meta_yields_nothing_usable():
    assert build_keys({}, "MP3") == []


def test_build_keys_uses_query_when_meta_has_no_artist_title():
    keys = build_keys({}, "MP3", query="Artist - Title")
    assert any(k.startswith("track:") for k in keys)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_save_then_lookup(cache):
    assert cache.save("FILEID123", META, "FLAC") >= 1
    hit = cache.lookup(META, "FLAC")
    assert hit is not None
    assert hit["file_id"] == "FILEID123"
    assert hit["performer"] == "Daft Punk"
    assert hit["title"] == "Get Lucky"
    assert hit["duration"] == 248


def test_lookup_misses_for_other_quality(cache):
    cache.save("FILEID123", META, "FLAC")
    assert cache.lookup(META, "MP3") is None


def test_lookup_by_isrc_from_different_meta(cache):
    """Тот же релиз, но метаданные пришли из другого источника."""
    cache.save("FILEID123", META, "FLAC")
    other = {"artist": "Daft Punk", "title": "Get Lucky (Remastered)", "isrc": META["isrc"]}
    hit = cache.lookup(other, "FLAC")
    assert hit is not None and hit["file_id"] == "FILEID123"


def test_lookup_by_normalised_title_ignores_casing(cache):
    cache.save("FILEID123", META, "MP3")
    hit = cache.lookup({"artist": "DAFT PUNK", "title": "get lucky"}, "MP3")
    assert hit is not None


def test_lookup_by_url(cache):
    url = "https://open.spotify.com/track/abc123"
    cache.save("FILEID999", META, "MP3", url=url)
    hit = cache.lookup({}, "MP3", url=url)
    assert hit is not None and hit["file_id"] == "FILEID999"


def test_lookup_refreshes_used_at(cache):
    cache.save("FILEID123", META, "MP3")
    assert cache.lookup(META, "MP3") is not None
    assert cache.lookup(META, "MP3") is not None
    assert cache.stats()["entries"] >= 1


def test_save_writes_all_keys(cache):
    written = cache.save("FILEID123", META, "FLAC", url="https://x.example/t")
    # isrc + track + url
    assert written == 3


def test_save_empty_file_id_is_rejected(cache):
    assert cache.save("", META, "MP3") == 0
    assert cache.lookup(META, "MP3") is None


# --------------------------------------------------------------------------
# Invalidation
# --------------------------------------------------------------------------

def test_invalidate_removes_every_key(cache):
    cache.save("FILEID123", META, "FLAC", url="https://x.example/t")
    cache.invalidate(META, "FLAC", url="https://x.example/t")
    assert cache.lookup(META, "FLAC", url="https://x.example/t") is None


def test_invalidate_key_removes_single_entry(cache):
    """Удаление одного ключа не обязано чистить остальные."""
    cache.save("FILEID123", META, "FLAC")
    before = cache.stats()["entries"]
    entry = cache.lookup(META, "FLAC")
    cache.invalidate_key(entry["key"])
    assert cache.stats()["entries"] == before - 1
    # Запись всё ещё находится по другому ключу того же трека.
    assert cache.lookup(META, "FLAC") is not None


def test_invalidate_does_not_touch_other_quality(cache):
    cache.save("FLACID", META, "FLAC")
    cache.save("MP3ID", META, "MP3")
    cache.invalidate(META, "FLAC")
    assert cache.lookup(META, "FLAC") is None
    assert cache.lookup(META, "MP3")["file_id"] == "MP3ID"


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------

def test_prune_removes_stale_entries(cache):
    cache.save("OLD", META, "MP3")
    conn = cache._connect()
    # Перематываем used_at далеко в прошлое.
    conn.execute("UPDATE file_ids SET used_at = ?", (time.time() - 400 * 86400,))
    assert cache.prune() >= 1
    assert cache.lookup(META, "MP3") is None


def test_stats_groups_by_quality(cache):
    cache.save("A", META, "MP3")
    cache.save("B", META, "FLAC")
    stats = cache.stats()
    assert stats["healthy"] is True
    # Одна запись трека пишется по нескольким ключам (isrc + track),
    # поэтому считаем ключи, а не уникальные треки.
    assert stats["by_quality"].get("MP3", 0) >= 1
    assert stats["by_quality"].get("FLAC", 0) >= 1
    assert stats["by_quality"]["MP3"] == stats["by_quality"]["FLAC"]


def test_clear(cache):
    cache.save("A", META, "MP3")
    cache.clear()
    assert cache.stats()["entries"] == 0


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------

def test_disabled_cache_is_a_noop(tmp_path):
    instance = FileIdCache(path=tmp_path / "f.db", enabled=False)
    assert instance.save("FILEID", META, "MP3") == 0
    assert instance.lookup(META, "MP3") is None
    assert not (tmp_path / "f.db").exists()
    instance.close()


def test_unwritable_path_disables_cache(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not a dir")
    instance = FileIdCache(path=blocker / "nested" / "f.db", enabled=True)
    assert instance.save("FILEID", META, "MP3") == 0
    assert instance.lookup(META, "MP3") is None
    instance.close()


def test_corrupt_db_does_not_crash(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a database at all" * 200)
    instance = FileIdCache(path=path, enabled=True)
    assert instance.lookup(META, "MP3") is None
    instance.save("FILEID", META, "MP3")
    assert instance.lookup(META, "MP3") is None
    instance.close()


def test_concurrent_saves(cache):
    def worker(index: int):
        meta = {"artist": f"A{index}", "title": f"T{index}"}
        return cache.save(f"FILEID{index}", meta, "MP3")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(40)))

    assert all(results)
    assert cache.stats()["entries"] == 40


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "Telegram server says: Bad Request: file_id is invalid",
        "wrong file identifier",
        "Bad Request: file reference is no longer available",
        "Bad Request: type of file mismatch",
        "FILE_ID_INVALID",
    ],
)
def test_detects_invalid_file_id(message):
    assert is_invalid_file_id_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "Flood control exceeded",
        "chat not found",
        "Bad Request: message is not modified",
        "Peer id is invalid",
    ],
)
def test_does_not_mistake_other_errors_for_stale_file_id(message):
    assert is_invalid_file_id_error(Exception(message)) is False


def test_empty_error_is_not_stale_file_id():
    assert is_invalid_file_id_error(Exception("")) is False


# --------------------------------------------------------------------------
# Bot integration
# --------------------------------------------------------------------------

def _status_msg():
    status = MagicMock(spec=Message)
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    return status


def test_cache_hit_skips_download_entirely(tmp_path, monkeypatch):
    """Повторный запрос не должен ни качать, ни запускать ffmpeg."""
    instance = FileIdCache(path=tmp_path / "f.db", enabled=True)
    monkeypatch.setattr(bot_module, "get_file_id_cache", lambda: instance)
    instance.save("CACHED_FILE_ID", META, "FLAC")

    async def run():
        mock_bot = MagicMock(spec=bot_module.Bot)
        mock_bot.send_audio = AsyncMock()
        status = _status_msg()

        with patch("bot.core.download_track_by_link") as mock_dl:
            await bot_module.run_download_and_send(
                bot=mock_bot,
                chat_id=1,
                query_or_url="Daft Punk - Get Lucky",
                target_quality="FLAC",
                meta=META,
                status_msg=status,
            )

        mock_dl.assert_not_called()
        assert mock_bot.send_audio.await_count == 1
        assert mock_bot.send_audio.await_args.kwargs["audio"] == "CACHED_FILE_ID"

    asyncio.run(run())
    instance.close()


def test_cache_miss_falls_through_to_download(tmp_path, monkeypatch):
    instance = FileIdCache(path=tmp_path / "f.db", enabled=True)
    monkeypatch.setattr(bot_module, "get_file_id_cache", lambda: instance)

    track = tmp_path / "Daft Punk - Get Lucky.mp3"
    track.write_bytes(b"mp3" * 100)

    async def run():
        mock_bot = MagicMock(spec=bot_module.Bot)
        mock_bot.send_audio = AsyncMock()
        status = _status_msg()

        with patch("bot.core.download_track_by_link", return_value=track) as mock_dl, \
             patch("bot.prepare_thumbnail", return_value=None):
            await bot_module.run_download_and_send(
                bot=mock_bot,
                chat_id=1,
                query_or_url="Daft Punk - Get Lucky",
                target_quality="MP3",
                meta=META,
                status_msg=status,
            )

        assert mock_dl.called

    asyncio.run(run())
    instance.close()


def test_stale_file_id_is_invalidated_and_track_redownloaded(tmp_path, monkeypatch):
    """Telegram отверг file_id -> запись удаляется, трек качается заново."""
    instance = FileIdCache(path=tmp_path / "f.db", enabled=True)
    monkeypatch.setattr(bot_module, "get_file_id_cache", lambda: instance)
    instance.save("DEAD_FILE_ID", META, "FLAC")
    assert instance.lookup(META, "FLAC") is not None

    track = tmp_path / "Daft Punk - Get Lucky.flac"
    track.write_bytes(b"flac" * 100)

    async def run():
        mock_bot = MagicMock(spec=bot_module.Bot)
        # Первая отправка (из кэша) падает, вторая (после скачивания) успешна.
        mock_bot.send_audio = AsyncMock(
            side_effect=[
                Exception("Bad Request: file_id is invalid"),
                MagicMock(audio=MagicMock(file_id="FRESH_FILE_ID")),
            ]
        )
        status = _status_msg()

        with patch("bot.core.download_track_by_link", return_value=track) as mock_dl, \
             patch("bot.prepare_thumbnail", return_value=None):
            await bot_module.run_download_and_send(
                bot=mock_bot,
                chat_id=1,
                query_or_url="Daft Punk - Get Lucky",
                target_quality="FLAC",
                meta=META,
                status_msg=status,
            )

        # Упало -> пошло скачивание.
        assert mock_dl.called

    asyncio.run(run())

    # Протухшая запись заменена свежей, а не осталась жить рядом.
    hit = instance.lookup(META, "FLAC")
    assert hit is not None and hit["file_id"] == "FRESH_FILE_ID"
    instance.close()


def test_file_id_is_saved_after_successful_send(tmp_path, monkeypatch):
    instance = FileIdCache(path=tmp_path / "f.db", enabled=True)
    monkeypatch.setattr(bot_module, "get_file_id_cache", lambda: instance)

    track = tmp_path / "Daft Punk - Get Lucky.mp3"
    track.write_bytes(b"mp3" * 100)

    async def run():
        mock_bot = MagicMock(spec=bot_module.Bot)
        mock_bot.send_audio = AsyncMock(
            return_value=MagicMock(audio=MagicMock(file_id="SAVED_ID"))
        )
        status = _status_msg()

        with patch("bot.core.download_track_by_link", return_value=track), \
             patch("bot.prepare_thumbnail", return_value=None), \
             patch.object(config, "BOT_API_SERVER_URL", ""):
            await bot_module.run_download_and_send(
                bot=mock_bot,
                chat_id=1,
                query_or_url="Daft Punk - Get Lucky",
                target_quality="MP3",
                meta=META,
                status_msg=status,
            )

    asyncio.run(run())

    hit = instance.lookup(META, "MP3")
    assert hit is not None and hit["file_id"] == "SAVED_ID"
    instance.close()


def test_unrelated_send_error_does_not_wipe_the_cache(tmp_path, monkeypatch):
    """Флуд-контрол не должен считаться протухшим file_id."""
    instance = FileIdCache(path=tmp_path / "f.db", enabled=True)
    monkeypatch.setattr(bot_module, "get_file_id_cache", lambda: instance)
    instance.save("GOOD_ID", META, "MP3")

    track = tmp_path / "Daft Punk - Get Lucky.mp3"
    track.write_bytes(b"mp3" * 100)

    async def run():
        mock_bot = MagicMock(spec=bot_module.Bot)
        # Первая отправка (из кэша) падает на флуд-контроле, вторая
        # (после скачивания) проходит.
        mock_bot.send_audio = AsyncMock(
            side_effect=[Exception("Flood control exceeded"), MagicMock(audio=MagicMock(file_id="NEW_ID"))]
        )
        status = _status_msg()

        with patch("bot.core.download_track_by_link", return_value=track) as mock_dl, \
             patch("bot.prepare_thumbnail", return_value=None):
            await bot_module.run_download_and_send(
                bot=mock_bot,
                chat_id=1,
                query_or_url="Daft Punk - Get Lucky",
                target_quality="MP3",
                meta=META,
                status_msg=status,
            )

        # Флуд-контрол - повод скачать заново, но не повод забывать старый file_id.
        assert mock_dl.called

    asyncio.run(run())

    assert instance.lookup(META, "MP3") is not None
    instance.close()
