"""
Tests for the SQLite metadata cache.

The suite disables the global cache via ``CACHE_ENABLED=0`` in conftest, so
these tests build their own :class:`MetadataCache` instances against tmp paths.
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from core.cache import MetadataCache, normalize_isrc, normalize_query, url_key


@pytest.fixture
def cache(tmp_path):
    instance = MetadataCache(path=tmp_path / "cache.db", enabled=True)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# Key normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "Daft Punk - Get Lucky",
        "daft  punk - get lucky",
        "Daft Punk — Get Lucky",
        "Get Lucky by Daft Punk",
        "GET LUCKY DAFT PUNK",
        "Daft Punk - Get Lucky (feat. Pharrell Williams)",
    ],
)
def test_normalize_query_collapses_equivalent_forms(raw):
    assert normalize_query(raw) == "get lucky daft punk"


def test_normalize_query_keeps_genuinely_different_requests_apart():
    """'Get Lucky - Daft Punk' is a different track, so it must not collide."""
    assert normalize_query("Get Lucky - Daft Punk") != normalize_query("Daft Punk - Get Lucky")


def test_normalize_query_handles_empty():
    assert normalize_query("") == ""
    assert normalize_query(None) == ""


def test_normalize_isrc_strips_dashes_and_case():
    assert normalize_isrc("us-rc1-99-00001") == "USRC19900001"
    assert normalize_isrc("USRC19900001") == normalize_isrc("us rc1 99 00001")


def test_url_key_is_stable_and_case_insensitive():
    assert url_key("https://a.example/x") == url_key("HTTPS://A.EXAMPLE/X")
    assert url_key("https://a.example/x") != url_key("https://a.example/y")


# --------------------------------------------------------------------------
# Basic operations
# --------------------------------------------------------------------------

def test_set_then_get(cache):
    assert cache.set("query", "k", {"artist": "A", "title": "B"}) is True
    assert cache.get("query", "k") == {"artist": "A", "title": "B"}


def test_get_missing_returns_none(cache):
    assert cache.get("query", "nope") is None


def test_set_overwrites_existing_key(cache):
    cache.set("query", "k", {"v": 1})
    cache.set("query", "k", {"v": 2})
    assert cache.get("query", "k") == {"v": 2}


def test_kinds_are_namespaced(cache):
    cache.set("query", "shared", {"kind": "query"})
    cache.set("url", "shared", {"kind": "url"})
    assert cache.get("query", "shared") == {"kind": "query"}
    assert cache.get("url", "shared") == {"kind": "url"}


def test_empty_key_is_not_stored(cache):
    assert cache.set("query", "", {"v": 1}) is False
    assert cache.get("query", "") is None


def test_none_value_is_not_stored(cache):
    assert cache.set("query", "k", None) is False


def test_delete(cache):
    cache.set("query", "k", {"v": 1})
    cache.delete("query", "k")
    assert cache.get("query", "k") is None


def test_unserialisable_payload_is_rejected_without_raising(cache):
    class Weird:
        pass

    assert cache.set("query", "k", {"obj": Weird()}) is False
    assert cache.get("query", "k") is None


# --------------------------------------------------------------------------
# TTL
# --------------------------------------------------------------------------

def test_expired_entry_is_treated_as_missing(cache):
    cache.set("query", "k", {"v": 1}, ttl_days=1.0 / 86400.0)  # 1 second
    time.sleep(1.1)
    assert cache.get("query", "k") is None


def test_non_expiring_entry_survives(cache):
    cache.set("query", "k", {"v": 1}, ttl_days=0)
    time.sleep(0.1)
    assert cache.get("query", "k") == {"v": 1}


def test_prune_removes_only_expired(cache):
    cache.set("query", "fresh", {"v": 1}, ttl_days=30)
    cache.set("query", "stale", {"v": 1}, ttl_days=1.0 / 86400.0)
    time.sleep(1.1)
    removed = cache.prune()
    assert removed == 1
    assert cache.get("query", "fresh") == {"v": 1}
    assert cache.get("query", "stale") is None


# --------------------------------------------------------------------------
# Track metadata helpers
# --------------------------------------------------------------------------

def test_set_track_writes_all_three_keys(cache):
    meta = {"artist": "A", "title": "B", "isrc": "USRC19900001"}
    cache.set_track(meta, query="A - B", url="https://x.example/1")
    assert cache.get_track(query="A - B") == meta
    assert cache.get_track(url="https://x.example/1") == meta
    assert cache.get_track(isrc="us-rc1-99-00001") == meta


def test_get_track_prefers_url_over_query(cache):
    """A URL is the most specific key and must win."""
    by_url = {"artist": "FromURL", "isrc": "AAA00000000"}
    by_query = {"artist": "FromQuery", "isrc": "BBB00000000"}
    cache.set("url", url_key("https://x.example/1"), by_url)
    cache.set("query", normalize_query("A - B"), by_query)
    assert cache.get_track(query="A - B", url="https://x.example/1") == by_url


def test_get_track_falls_through_to_weaker_keys(cache):
    cache.set("query", normalize_query("A - B"), {"artist": "FromQuery"})
    assert cache.get_track(query="a - b") == {"artist": "FromQuery"}


def test_get_track_returns_none_when_nothing_matches(cache):
    assert cache.get_track(query="nothing", url="https://nope", isrc="XXX00000000") is None


def test_set_track_ignores_empty_meta(cache):
    cache.set_track({}, query="A - B")
    assert cache.get_track(query="A - B") is None


# --------------------------------------------------------------------------
# Release art: album key and release_id key must not shadow each other
# --------------------------------------------------------------------------

def test_release_art_album_and_release_id_are_separate_entries(cache):
    cache.set_release_art("Death Grips", "Exmilitary", "https://album/art.jpg")
    cache.set_release_art("Death Grips", "Exmilitary", "https://release/front.jpg", release_id="rel-123")

    assert cache.get_release_art("Death Grips", "Exmilitary") == "https://album/art.jpg"
    assert cache.get_release_art("Death Grips", "Exmilitary", release_id="rel-123") == (
        "https://release/front.jpg"
    )


def test_release_art_lookup_without_release_id_does_not_hit_release_entry(cache):
    cache.set_release_art("A", "B", "https://rel/art.jpg", release_id="rel-1")
    assert cache.get_release_art("A", "B") is None


def test_release_art_needs_artist_and_album_or_release(cache):
    assert cache.get_release_art("", "") is None
    assert cache.get_release_art("A", "") is None
    assert cache.get_release_art("", "B") is None


# --------------------------------------------------------------------------
# Deezer id helpers
# --------------------------------------------------------------------------

def test_deezer_id_roundtrip(cache):
    cache.set_deezer_id("Daft Punk", "Get Lucky", "Discovery", "12345")
    assert cache.get_deezer_id("daft  punk", "get lucky", "discovery") == "12345"


def test_deezer_id_distinguishes_albums(cache):
    cache.set_deezer_id("A", "B", "Album1", "111")
    assert cache.get_deezer_id("A", "B", "Album2") is None


def test_deezer_id_rejects_empty_id(cache):
    cache.set_deezer_id("A", "B", "C", "")
    assert cache.get_deezer_id("A", "B", "C") is None


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------

def test_clear_empties_cache(cache):
    cache.set("query", "k", {"v": 1})
    cache.set("url", "k2", {"v": 2})
    cache.clear()
    assert cache.stats()["entries"] == 0


def test_stats_reports_counts_by_kind(cache):
    cache.set("query", "a", {"v": 1})
    cache.set("url", "b", {"v": 1})
    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["by_kind"] == {"query": 1, "url": 1}
    assert stats["healthy"] is True


# --------------------------------------------------------------------------
# Robustness: a broken cache must never break downloading
# --------------------------------------------------------------------------

def test_unwritable_path_degrades_to_disabled(tmp_path):
    """A path we cannot create must disable the cache, not raise."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    instance = MetadataCache(path=blocker / "nested" / "cache.db", enabled=True)
    assert instance.set("query", "k", {"v": 1}) is False
    assert instance.get("query", "k") is None
    assert instance.stats()["healthy"] is False
    instance.close()


def test_corrupt_database_file_does_not_crash(tmp_path):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is definitely not a sqlite database" * 100)

    instance = MetadataCache(path=db_path, enabled=True)
    # Whatever happens, the API must stay safe to call.
    assert instance.get("query", "k") is None
    instance.set("query", "k", {"v": 1})
    assert instance.get("query", "k") is None
    instance.close()


def test_disabled_cache_is_a_noop(tmp_path):
    instance = MetadataCache(path=tmp_path / "cache.db", enabled=False)
    assert instance.set("query", "k", {"v": 1}) is False
    assert instance.get("query", "k") is None
    assert instance.stats()["enabled"] is False
    # The file must not even be created.
    assert not (tmp_path / "cache.db").exists()
    instance.close()


def test_corrupt_json_payload_is_dropped(cache):
    """A hand-edited row with broken JSON must be discarded, not returned."""
    conn = sqlite3.connect(str(cache.path))
    conn.execute(
        "INSERT INTO entries (key, kind, payload, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        ("query:broken", "query", "{not valid json", time.time(), time.time() + 999),
    )
    conn.commit()
    conn.close()

    assert cache.get("query", "broken") is None
    # The broken row is cleaned up.
    assert cache.stats()["entries"] == 0


# --------------------------------------------------------------------------
# Thread safety
# --------------------------------------------------------------------------

def test_concurrent_writes_from_many_threads(cache):
    def writer(index: int) -> bool:
        return cache.set("query", f"key-{index}", {"index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(writer, range(50)))

    assert all(results)
    assert cache.stats()["entries"] == 50
    for index in range(50):
        assert cache.get("query", f"key-{index}") == {"index": index}


def test_concurrent_reads_and_writes(cache):
    cache.set("query", "shared", {"v": 0})

    def worker(index: int):
        if index % 2 == 0:
            return cache.set("query", f"w{index}", {"v": index})
        return cache.get("query", "shared") is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(40)))

    assert cache.get("query", "shared") == {"v": 0}


# --------------------------------------------------------------------------
# Integration with the download orchestrator
# --------------------------------------------------------------------------

def test_second_download_reuses_cached_metadata(tmp_path, monkeypatch):
    """The whole point: a repeat request must not re-hit the metadata APIs."""
    from core import cache as cache_mod
    from core import download_track_by_link

    instance = MetadataCache(path=tmp_path / "cache.db", enabled=True)
    monkeypatch.setattr(cache_mod, "_cache", instance)

    produced = tmp_path / "Artist - Track.mp3"
    meta = {"artist": "Artist", "title": "Track", "duration": 200, "deezer_id": "42"}
    dest = tmp_path / "out"

    def fake_download(*args, **kwargs):
        produced.write_bytes(b"mp3data" * 50)
        return produced

    with (
        patch("core.metadata.resolve_query_metadata", return_value=meta) as mock_resolve,
        patch("core.search_deezer_track", return_value="42"),
        patch("core.download_deezer_track", side_effect=fake_download),
        patch("core.tagger.apply_metadata"),
    ):
        first = download_track_by_link("Artist - Track", dest_dir=dest)
        # Другая капитализация того же запроса -> должен попасть в кэш.
        second = download_track_by_link("artist - track", dest_dir=dest)

    assert first and second
    # Resolver called exactly once: the second request was served from cache.
    assert mock_resolve.call_count == 1
    instance.close()


def test_cache_hit_is_reported_to_the_user(tmp_path, monkeypatch, capsys):
    from core import cache as cache_mod
    from core import download_track_by_link

    instance = MetadataCache(path=tmp_path / "cache.db", enabled=True)
    monkeypatch.setattr(cache_mod, "_cache", instance)

    produced = tmp_path / "Artist - Track.mp3"
    meta = {"artist": "Artist", "title": "Track", "duration": 200, "deezer_id": "42"}

    with (
        patch("core.metadata.resolve_query_metadata", return_value=meta),
        patch("core.search_deezer_track", return_value="42"),
        patch(
            "core.download_deezer_track",
            side_effect=lambda *a, **k: (produced.write_bytes(b"mp3" * 50), produced)[1],
        ),
        patch("core.tagger.apply_metadata"),
    ):
        download_track_by_link("Artist - Track", dest_dir=tmp_path / "out")
        download_track_by_link("Artist - Track", dest_dir=tmp_path / "out")

    assert "из кэша" in capsys.readouterr().out
    instance.close()
