"""
Tests for the ISRC-based matching path.

An ISRC identifies one specific sound recording, so looking a track up by it
is exact - no candidate scoring, no guessing. This is the cheapest possible
upgrade over searching by name, but it only works when we actually have an
ISRC, and the ISRC arrives from elsewhere, so it needs validation.
"""

from unittest.mock import MagicMock, patch

import pytest

from core import metadata
from core.cache import MetadataCache
from core.sources import deezer

ISRC = "GBDUW0000059"
DEEZER_TRACK = {
    "id": 3135556,
    "title": "Harder, Better, Faster, Stronger",
    "isrc": ISRC,
    "duration": 226,
    "release_date": "2001-03-12",
    "artist": {"name": "Daft Punk"},
    "album": {
        "id": 302127,
        "title": "Discovery",
        "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000.jpg",
    },
}


@pytest.fixture
def cache(tmp_path):
    instance = MetadataCache(path=tmp_path / "c.db", enabled=True)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# Нормализация ISRC
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["GBDUW0000059", "gbduw0000059", "GBDUW-0000-059", " GBDUW 0000059 "],
)
def test_isrc_normalisation_accepts_common_formats(raw):
    assert deezer._normalize_isrc(raw) == ISRC


def test_short_garbage_is_rejected_without_request():
    for junk in ("", "abc", "GBDUW", None, "123"):
        assert deezer.search_deezer_by_isrc(junk) is None


# --------------------------------------------------------------------------
# search_deezer_by_isrc
# --------------------------------------------------------------------------

def test_search_by_isrc_builds_correct_url():
    with patch("core.sources.deezer.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, **{"json.return_value": DEEZER_TRACK})
        deezer.search_deezer_by_isrc("gbduw-0000-059")
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://api.deezer.com/track/isrc:GBDUW0000059"


def test_search_by_isrc_returns_track():
    with patch("core.sources.deezer.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, **{"json.return_value": DEEZER_TRACK})
        assert deezer.search_deezer_by_isrc(ISRC)["id"] == 3135556


def test_search_by_isrc_returns_none_on_http_error():
    with patch("core.sources.deezer.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=404)
        assert deezer.search_deezer_by_isrc(ISRC) is None


def test_search_by_isrc_returns_none_on_api_error():
    with patch("core.sources.deezer.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200, **{"json.return_value": {"error": {"code": 800}}}
        )
        assert deezer.search_deezer_by_isrc(ISRC) is None


def test_search_by_isrc_survives_network_exception():
    with patch("core.sources.deezer.httpx.get", side_effect=Exception("timeout")):
        assert deezer.search_deezer_by_isrc(ISRC) is None


# --------------------------------------------------------------------------
# resolve_deezer_by_isrc: валидация
# --------------------------------------------------------------------------

def test_resolve_by_isrc_without_expectations_skips_validation():
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=DEEZER_TRACK):
        res = metadata.resolve_deezer_by_isrc(ISRC)
    assert res["deezer_id"] == "3135556"
    assert res["album"] == "Discovery"
    assert res["album_art"].endswith("1000x1000.jpg")


def test_resolve_by_isrc_validates_against_expectations():
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=DEEZER_TRACK), \
         patch("core.metadata.validate_deezer_track", return_value={"title": "x"}) as mock_val:
        res = metadata.resolve_deezer_by_isrc(
            ISRC, expected_artist="Daft Punk", expected_title="Harder, Better, Faster, Stronger"
        )
    assert mock_val.called
    assert res["deezer_id"] == "3135556"


def test_resolve_by_isrc_rejects_when_validation_fails():
    """ISRC пришёл извне - верить ему без проверки нельзя."""
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=DEEZER_TRACK), \
         patch("core.metadata.validate_deezer_track", return_value=None):
        assert metadata.resolve_deezer_by_isrc(
            ISRC, expected_artist="Some Other Band", expected_title="Other Song"
        ) is None


def test_resolve_by_isrc_rejects_mismatched_response():
    """Deezer вернул другую запись - не доверяем."""
    other = dict(DEEZER_TRACK, isrc="USRC17607839")
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=other):
        assert metadata.resolve_deezer_by_isrc(ISRC) is None


def test_resolve_by_isrc_allows_case_differences():
    other = dict(DEEZER_TRACK, isrc=ISRC.lower())
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=other):
        assert metadata.resolve_deezer_by_isrc(ISRC) is not None


def test_resolve_by_isrc_returns_none_when_not_found():
    with patch("core.sources.deezer.search_deezer_by_isrc", return_value=None):
        assert metadata.resolve_deezer_by_isrc(ISRC) is None


# --------------------------------------------------------------------------
# Кэш
# --------------------------------------------------------------------------

def test_cache_roundtrip_by_isrc(cache):
    cache.set_deezer_id_by_isrc(ISRC, "3135556")
    assert cache.get_deezer_id_by_isrc("gbduw-0000-059") == "3135556"


def test_cache_miss_returns_none(cache):
    assert cache.get_deezer_id_by_isrc(ISRC) is None


def test_negative_result_is_cached(cache):
    """
    Пустой ответ тоже запоминаем: иначе каждый повторный запрос
    заново ломится в Deezer за одним и тем же 'нет такого ISRC'.
    """
    cache.set_deezer_id_by_isrc(ISRC, "")
    assert cache.get_deezer_id_by_isrc(ISRC) == ""
    # Ключ существует - второй раз не пойдём в сеть.
    assert cache.stats()["entries"] >= 1


def test_isrc_cache_is_separate_from_name_cache(cache):
    cache.set_deezer_id("A", "B", "C", "111")
    cache.set_deezer_id_by_isrc(ISRC, "222")
    assert cache.get_deezer_id("A", "B", "C") == "111"
    assert cache.get_deezer_id_by_isrc(ISRC) == "222"


def test_isrc_cache_ignores_empty_isrc(cache):
    cache.set_deezer_id_by_isrc("", "3135556")
    assert cache.stats()["entries"] == 0


# --------------------------------------------------------------------------
# Оркестратор
# --------------------------------------------------------------------------

META = {"artist": "Daft Punk", "title": "Harder, Better, Faster, Stronger", "isrc": ISRC, "duration": 226}


def _run_twice(meta, isrc_meta, produced):
    """Прогоняет download_track_by_link дважды, возвращает счётчики вызовов."""
    from core import cache as cache_mod
    from core import download_track_by_link

    instance = MetadataCache(path=produced.parent / "c.db", enabled=True)
    cache_calls = []
    original = cache_mod.get_cache
    cache_mod.get_cache = lambda: instance

    def make_file(*a, **kw):
        out = produced.parent / "out.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp3" * 200)
        return out

    try:
        with (
            patch("core.search_deezer_track", side_effect=lambda *a, **kw: cache_calls.append("name") or None),
            patch("core.metadata.resolve_deezer_by_isrc", return_value=isrc_meta) as mock_isrc,
            patch("core.download_deezer_track", side_effect=make_file),
            patch("core.tagger.apply_metadata"),
        ):
            dest = produced.parent / "d1"
            download_track_by_link("Daft Punk - Harder", dest_dir=dest, track_meta=meta)
            dest2 = produced.parent / "d2"
            download_track_by_link("Daft Punk - Harder", dest_dir=dest2, track_meta=meta)
        return mock_isrc, cache_calls
    finally:
        cache_mod.get_cache = original
        instance.close()


def test_isrc_path_skips_name_search(tmp_path):
    isrc_meta = {"deezer_id": "3135556", "album": "Discovery", "year": "2001",
                 "album_art": "https://cdn-images.dzcdn.net/x/1000x1000.jpg"}
    mock_isrc, name_calls = _run_twice(META, isrc_meta, tmp_path / "t.mp3")
    # Поиск по названию ни разу не понадобился.
    assert name_calls == []


def test_isrc_is_only_resolved_once(tmp_path):
    isrc_meta = {"deezer_id": "3135556"}
    mock_isrc, _ = _run_twice(META, isrc_meta, tmp_path / "t.mp3")
    # Второй запрос того же трека взят из кэша.
    assert mock_isrc.call_count == 1


def test_negative_isrc_result_also_cached(tmp_path):
    """
    ISRC-отказ кэшируется: второй запрос того же трека не должен снова
    ломиться в Deezer за тем же «такого ISRC нет».

    Поиск по названию при этом повторяется - у него ключ размытый
    (художественное написание), и отрицательный результат там кэшировать
    опасно: «Daft Punk - X» и «daft punk — X» это один ключ, а вот
    «Daft Punk - Get Lucky» и «Daft Punk - Get Lucky (Radio Edit)» -
    уже нет.
    """
    mock_isrc, name_calls = _run_twice(META, None, tmp_path / "t.mp3")
    # ISRC-путь спросил ровно один раз, второй раз взял кэш.
    assert mock_isrc.call_count == 1


def test_falls_back_to_name_search_when_no_isrc(tmp_path):
    from core import cache as cache_mod
    from core import download_track_by_link

    meta = dict(META)
    meta.pop("isrc")
    instance = MetadataCache(path=tmp_path / "c.db", enabled=True)
    original = cache_mod.get_cache
    cache_mod.get_cache = lambda: instance

    def make_file(*a, **kw):
        out = tmp_path / "out.mp3"
        out.write_bytes(b"mp3" * 200)
        return out

    try:
        with (
            patch("core.search_deezer_track", return_value="999") as mock_name,
            patch("core.download_deezer_track", side_effect=make_file),
            patch("core.tagger.apply_metadata"),
        ):
            download_track_by_link("x", dest_dir=tmp_path / "d1", track_meta=meta)
            download_track_by_link("x", dest_dir=tmp_path / "d2", track_meta=meta)
        # Первый раз спросили, дальше кэш по названию.
        assert mock_name.call_count == 1
    finally:
        cache_mod.get_cache = original
        instance.close()
