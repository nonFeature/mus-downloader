"""
Tests for the local audio file cache.

Disabled suite-wide via ``FILE_CACHE_ENABLED=0`` in conftest, so these tests
build their own :class:`AudioFileCache` instances on tmp paths.
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from core import filecache
from core.filecache import (
    AudioFileCache,
    build_keys,
    canonical_name,
    sanitize_filename,
)

META = {"artist": "Daft Punk", "title": "Get Lucky", "isrc": "USRC17607839"}


@pytest.fixture
def cache(tmp_path):
    instance = AudioFileCache(root=tmp_path / "cache_audio", enabled=True)
    yield instance
    instance.close()


def make_track(directory, name="Artist - Title.mp3", size=2048):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def test_sanitize_strips_unsafe_characters():
    assert sanitize_filename('AC/DC: Back? <in> "Black"') == "AC_DC_ Back_ _in_ _Black_"


def test_sanitize_strips_control_characters():
    assert "\x00" not in sanitize_filename("bad\x00name")


def test_sanitize_handles_empty():
    assert sanitize_filename("") == "Unknown"
    assert sanitize_filename(None) == "Unknown"


def test_sanitize_truncates_long_names():
    assert len(sanitize_filename("a" * 500)) <= 120


def test_canonical_name_uses_artist_and_title():
    assert canonical_name(META, ".flac") == "Daft Punk - Get Lucky.flac"


def test_canonical_name_strips_version_noise():
    """Пометки версий не различают трек, но засоряют имя файла."""
    meta = {"artist": "Daft Punk", "title": "Get Lucky (feat. Pharrell) (Remastered 2011)"}
    name = canonical_name(meta, ".mp3")
    assert "feat" not in name
    assert "Remastered" not in name
    assert name.endswith("- Get Lucky.mp3")


def test_canonical_name_normalises_extension():
    assert canonical_name(META, "FLAC").endswith(".flac")
    assert canonical_name(META, "mp3").endswith(".mp3")


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def test_build_keys_includes_quality():
    keys = build_keys(META, "FLAC")
    assert all(k.endswith(":FLAC") for k in keys)
    assert set(keys) != set(build_keys(META, "MP3"))


def test_build_keys_empty_meta():
    assert build_keys({}, "MP3") == []


# --------------------------------------------------------------------------
# Store / lookup
# --------------------------------------------------------------------------

def test_store_then_lookup(cache, tmp_path):
    src = make_track(tmp_path / "dl", "Artist - Title.flac")
    cache.store(src, META, "FLAC")
    hit = cache.lookup(META, "FLAC")
    assert hit is not None and hit.is_file()
    assert hit.suffix == ".flac"
    assert "Daft Punk - Get Lucky" in hit.name


def test_stored_extension_reflects_the_real_file(cache, tmp_path):
    """Ключ отражает запрос, расширение - реальность: FLAC-запрос мог
    откатиться на MP3, и тогда файл честно лежит как .mp3."""
    src = make_track(tmp_path / "dl", "Artist - Title.mp3")
    cache.store(src, META, "FLAC")
    hit = cache.lookup(META, "FLAC")
    assert hit is not None and hit.suffix == ".mp3"
    # MP3-запрос это не тот файл, поэтому промах.
    assert cache.lookup(META, "MP3") is None


def test_store_copies_and_keeps_original(cache, tmp_path):
    src = make_track(tmp_path / "dl")
    cache.store(src, META, "MP3")
    assert src.exists(), "исходный файл пользователя должен остаться на месте"
    assert cache.lookup(META, "MP3").exists()


def test_store_missing_or_empty_source(cache, tmp_path):
    assert cache.store(tmp_path / "nope.mp3", META, "MP3") is None
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    assert cache.store(empty, META, "MP3") is None


def test_lookup_is_quality_specific(cache, tmp_path):
    src = make_track(tmp_path / "dl", "a.flac")
    cache.store(src, META, "FLAC")
    assert cache.lookup(META, "MP3") is None
    assert cache.lookup(META, "FLAC") is not None


def test_lookup_matched_by_isrc_from_other_meta(cache, tmp_path):
    cache.store(make_track(tmp_path / "dl", "a.flac"), META, "FLAC")
    other = {"artist": "Daft Punk", "title": "Get Lucky (Remastered)", "isrc": META["isrc"]}
    assert cache.lookup(other, "FLAC") is not None


def test_lookup_matched_by_url(cache, tmp_path):
    url = "https://open.spotify.com/track/xyz"
    cache.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3", url=url)
    assert cache.lookup({}, "MP3", url=url) is not None


def test_lookup_miss_returns_none(cache):
    assert cache.lookup(META, "MP3") is None


def test_missing_file_is_forgotten_not_returned(cache, tmp_path):
    src = make_track(tmp_path / "dl", "a.mp3")
    cache.store(src, META, "MP3")
    hit = cache.lookup(META, "MP3")
    assert hit is not None
    hit.unlink()
    # Файл исчез -> это промах, а не «отдаём битый путь».
    assert cache.lookup(META, "MP3") is None
    assert cache.stats()["entries"] == 0


def test_truncated_file_is_rejected(cache, tmp_path):
    """Обрезанный файл после прерванной закачки отдавать нельзя."""
    src = make_track(tmp_path / "dl", "a.mp3", size=2048)
    cache.store(src, META, "MP3")
    hit = cache.lookup(META, "MP3")
    assert hit is not None
    hit.write_bytes(b"x" * 10)
    assert cache.lookup(META, "MP3") is None


def test_storing_file_already_inside_cache_is_a_noop(cache, tmp_path):
    first = cache.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3")
    again = cache.store(first, META, "MP3")
    assert again == first
    assert cache.stats()["entries"] >= 1


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------

def test_clear_removes_files_and_index(cache, tmp_path):
    cache.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3")
    assert cache.stats()["entries"] >= 1
    cache.clear()
    assert cache.stats()["entries"] == 0
    # Файлы в кэше тоже удалены.
    leftovers = [p for p in cache.root.iterdir() if p.name != "index.db" and not p.name.startswith("index.db-")]
    assert leftovers == []


def test_clear_keep_files_only_drops_index(cache, tmp_path):
    cache.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3")
    cache.clear(remove_files=False)
    assert cache.stats()["entries"] == 0
    assert cache.lookup(META, "MP3") is None


def test_total_size(cache, tmp_path):
    cache.store(make_track(tmp_path / "dl", "a.mp3", size=5000), META, "MP3")
    assert cache.total_size_bytes() >= 5000


# --------------------------------------------------------------------------
# Очистка по возрасту
# --------------------------------------------------------------------------

def test_prune_expired_removes_untouched_files(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "7")
    src = tmp_path / "dl"
    stored = cache.store(make_track(src, "a.mp3", size=4096), META, "MP3")
    assert stored.exists()

    # Отматываем used_at на 10 дней назад.
    conn = cache._connect()
    conn.execute(
        "UPDATE audio_files SET used_at = ?", (time.time() - 10 * 86400,)
    )

    assert cache.prune_expired() >= 1
    assert not stored.exists()
    assert cache.lookup(META, "MP3") is None
    assert cache.stats()["entries"] == 0


def test_prune_keeps_recently_used_files(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "30")
    stored = cache.store(make_track(tmp_path / "dl", "a.mp3", size=4096), META, "MP3")
    assert cache.prune_expired() == 0
    assert stored.exists()
    assert cache.lookup(META, "MP3") is not None


def test_age_zero_disables_pruning(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "0")
    stored = cache.store(make_track(tmp_path / "dl", "a.mp3", size=4096), META, "MP3")
    conn = cache._connect()
    conn.execute("UPDATE audio_files SET used_at = ?", (time.time() - 999 * 86400,))
    assert cache.prune_expired() == 0
    assert stored.exists()


def test_bad_age_env_falls_back_to_default(cache, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "не-число")
    assert cache.max_age_days() == 30


def test_lookup_refreshes_age(cache, tmp_path, monkeypatch):
    """Активно используемый трек не должен вылететь по возрасту."""
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "7")
    cache.store(make_track(tmp_path / "dl", "a.mp3", size=4096), META, "MP3")
    conn = cache._connect()
    conn.execute("UPDATE audio_files SET used_at = ?", (time.time() - 6 * 86400,))

    assert cache.lookup(META, "MP3") is not None
    # used_at обновился -> при следующей чистке файл выживет.
    assert cache.prune_expired() == 0


def test_prune_is_throttled(cache, tmp_path, monkeypatch):
    """Чистка по возрасту не должна гоняться на каждом сохранении."""
    monkeypatch.setenv("FILE_CACHE_MAX_AGE_DAYS", "7")
    calls = []
    monkeypatch.setattr(cache, "prune_expired", lambda *a, **k: calls.append(1) or 0)

    cache._last_prune_at = time.time()
    for i in range(5):
        cache.store(make_track(tmp_path / "dl", f"t{i}.mp3", size=1024), {"artist": f"A{i}", "title": "T"}, "MP3")
    assert calls == [], "в пределах часа чистка запускаться не должна"

    # Истёк час - чистка пошла.
    cache._last_prune_at = time.time() - 4000
    cache.store(make_track(tmp_path / "dl", "t9.mp3", size=1024), {"artist": "A9", "title": "T"}, "MP3")
    assert calls, "после часа чистка должна запуститься"


# --------------------------------------------------------------------------
# Лимит места
# --------------------------------------------------------------------------

def test_limit_evicts_least_recently_used(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_MB", "0.01")  # ~10 KB
    assert cache.max_bytes() < 50_000

    old = tmp_path / "dl_old"
    cache.store(make_track(old, "old.mp3", size=6000), {"artist": "Old", "title": "T"}, "MP3")
    time.sleep(0.01)
    cache.store(make_track(old, "new.mp3", size=6000), {"artist": "New", "title": "T"}, "MP3")

    # Оба файла не влезут -> вытеснен самый старый.
    assert cache.lookup({"artist": "Old", "title": "T"}, "MP3") is None
    assert cache.lookup({"artist": "New", "title": "T"}, "MP3") is not None


def test_limit_never_evicts_just_stored_file(cache, tmp_path, monkeypatch):
    """Пользователь только что скачал трек - выгонять его сразу нельзя."""
    monkeypatch.setenv("FILE_CACHE_MAX_MB", "0.001")  # ~1 KB, меньше файла
    meta = {"artist": "Huge", "title": "Track"}
    src = make_track(tmp_path / "dl", "huge.mp3", size=50_000)
    cache.store(src, meta, "MP3")
    assert cache.lookup(meta, "MP3") is not None


def test_limit_zero_means_unlimited(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_MB", "0")
    assert cache.max_bytes() == 0
    cache.store(make_track(tmp_path / "dl", "a.mp3", size=100_000), META, "MP3")
    assert cache.enforce_limit() == 0
    assert cache.lookup(META, "MP3") is not None


def test_limit_handles_bad_env(cache, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_MB", "не-число")
    assert cache.max_bytes() == 2048 * 1024 * 1024


def test_evicted_file_is_removed_from_disk(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_CACHE_MAX_MB", "0.01")
    old = tmp_path / "dl_old"
    stored = cache.store(
        make_track(old, "old.mp3", size=6000), {"artist": "Old", "title": "T"}, "MP3"
    )
    assert stored.exists()
    cache.store(make_track(old, "new.mp3", size=6000), {"artist": "New", "title": "T"}, "MP3")
    assert not stored.exists()


# --------------------------------------------------------------------------
# Версионирование формата
# --------------------------------------------------------------------------

def test_stale_version_entry_is_discarded(cache, tmp_path):
    """
    Меняешь правила тегирования / подбора источника -> файлы, собранные по
    старым правилам, не должны жить в кэше вечно.
    """
    cache.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3")
    conn = cache._connect()
    conn.execute("UPDATE audio_files SET version = ?", (filecache.CACHE_VERSION - 1,))

    assert cache.lookup(META, "MP3") is None
    assert cache.stats()["entries"] == 0


def test_old_schema_database_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS не добавляет колонки - нужна явная миграция."""
    root = tmp_path / "oldcache"
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "index.db"))
    conn.executescript(
        """
        CREATE TABLE audio_files (
            key        TEXT PRIMARY KEY,
            path       TEXT NOT NULL,
            quality    TEXT NOT NULL,
            size       INTEGER,
            ext        TEXT,
            created_at REAL NOT NULL,
            used_at    REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    instance = AudioFileCache(root=root, enabled=True)
    assert instance.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3") is not None
    assert instance.lookup(META, "MP3") is not None
    instance.close()


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------

def test_disabled_cache_is_a_noop(tmp_path):
    instance = AudioFileCache(root=tmp_path / "c", enabled=False)
    assert instance.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3") is None
    assert instance.lookup(META, "MP3") is None
    assert not (tmp_path / "c").exists()
    instance.close()


def test_unwritable_root_disables_cache(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not dir")
    instance = AudioFileCache(root=blocker / "nested", enabled=True)
    assert instance.store(make_track(tmp_path / "dl", "a.mp3"), META, "MP3") is None
    assert instance.lookup(META, "MP3") is None
    instance.close()


def test_concurrent_stores(cache, tmp_path):
    def worker(index: int):
        src = make_track(tmp_path / f"dl{index}", f"t{index}.mp3", size=1024 + index)
        return cache.store(src, {"artist": f"A{index}", "title": f"T{index}"}, "MP3")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(25)))

    assert all(r is not None for r in results)
    for index in range(25):
        assert cache.lookup({"artist": f"A{index}", "title": f"T{index}"}, "MP3") is not None


# --------------------------------------------------------------------------
# Integration with download_track_by_link
# --------------------------------------------------------------------------

def test_second_download_hits_the_file_cache(tmp_path, monkeypatch):
    from core import filecache
    from core import download_track_by_link

    instance = AudioFileCache(root=tmp_path / "cache_audio", enabled=True)
    monkeypatch.setattr(filecache, "_cache", instance)

    dest = tmp_path / "out"
    meta = {"artist": "Daft Punk", "title": "Get Lucky", "deezer_id": "42", "duration": 248}

    def fake_download(*args, **kwargs):
        out = dest / "Daft Punk - Get Lucky.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp3data" * 200)
        return out

    with (
        patch("core.search_deezer_track", return_value="42"),
        patch("core.download_deezer_track", side_effect=fake_download) as mock_dl,
        patch("core.tagger.apply_metadata"),
    ):
        first = download_track_by_link("Daft Punk - Get Lucky", dest_dir=dest, track_meta=meta)
        assert first is not None
        assert mock_dl.call_count == 1

        second = download_track_by_link("Daft Punk - Get Lucky", dest_dir=dest, track_meta=meta)
        assert second is not None
        # Второй раз источники не трогали вообще.
        assert mock_dl.call_count == 1
        assert second.parent == instance.root

    instance.close()


def test_force_bypasses_the_file_cache(tmp_path, monkeypatch):
    from core import filecache
    from core import download_track_by_link

    instance = AudioFileCache(root=tmp_path / "cache_audio", enabled=True)
    monkeypatch.setattr(filecache, "_cache", instance)

    dest = tmp_path / "out"
    meta = {"artist": "Daft Punk", "title": "Get Lucky", "deezer_id": "42"}

    def fake_download(*args, **kwargs):
        out = dest / "Daft Punk - Get Lucky.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp3data" * 200)
        return out

    with (
        patch("core.search_deezer_track", return_value="42"),
        patch("core.download_deezer_track", side_effect=fake_download) as mock_dl,
        patch("core.tagger.apply_metadata"),
    ):
        download_track_by_link("Daft Punk - Get Lucky", dest_dir=dest, track_meta=meta)
        download_track_by_link(
            "Daft Punk - Get Lucky",
            dest_dir=dest,
            track_meta=meta,
            reuse_cached_file=False,
        )
        assert mock_dl.call_count == 2

    instance.close()


def test_bot_disables_the_file_cache():
    """Бот всё равно удаляет файл сразу после отправки - кэш ему только мешает."""
    import inspect

    # bot/__init__.py подменяет пакет в sys.modules на модуль bot.bot,
    # поэтому импортируем ровно так же, как остальные тесты.
    import bot

    assert "reuse_cached_file=False" in inspect.getsource(bot.run_download_and_send)
