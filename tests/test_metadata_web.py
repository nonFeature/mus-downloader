"""Тесты сбора «мелких» метаданных из TIDAL и Apple Music.

Сеть не используется: протокол подменяется на уровне ``_tidal_get`` /
``_apple_get``, поэтому тесты живут под общим запретом httpx из conftest.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

import core  # noqa: F401 — регистрирует legacy-алиасы sources.*
from core import metadata_web
from core.tagger import apply_extended_metadata, apply_metadata


def _jwt(header: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(b'{"exp":1799948708}').decode().rstrip("=")
    return f"{encoded}.{payload}.c2lnbmF0dXJl"


# ───────────────────────────── Apple token ─────────────────────────────


def test_extract_apple_token_from_devtoken_attribute():
    token = _jwt({"alg": "ES256", "kid": "WebPlayKid"})
    html = f'<html><script>window.__x = {{"devToken={token}"}}</script></html>'
    assert metadata_web.extract_apple_token(html) == token


def test_extract_apple_token_prefers_webplaykid_in_bundles():
    other = _jwt({"alg": "ES256", "kid": "LT2ZDZSXNQ"})
    webplay = _jwt({"alg": "ES256", "kid": "WebPlayKid"})
    html = f'<html><script src="/assets/index.js"></script><p>{other}</p></html>'
    bundle = f'var a="{other}";var b="{webplay}";'
    assert metadata_web.extract_apple_token(html, [bundle]) == webplay


def test_extract_apple_token_returns_empty_when_nothing_found():
    assert metadata_web.extract_apple_token("<html>no tokens here</html>", []) == ""


# ─────────────────────────── TIDAL candidates ───────────────────────────


TIDAL_SEARCH = {
    "items": [
        {"id": 1, "title": "Get Lucky (Cover)", "artist": {"name": "Random Cover Band"}, "duration": 370},
        {"id": 2, "title": "Get Lucky", "artist": {"name": "Daft Punk"}, "duration": 248},
        {"id": 3, "title": "Get Lucky", "artist": {"name": "Daft Punk"}, "duration": 370},
        {"id": 4, "title": "Get Lucky (Karaoke)", "artist": {"name": "Karaoke Hits"}, "duration": 369},
    ]
}


def test_tidal_search_picks_matching_artist_and_duration(monkeypatch):
    monkeypatch.setattr(metadata_web, "_tidal_get", lambda path, params=None: TIDAL_SEARCH)
    best = metadata_web.tidal_search_track("Daft Punk", "Get Lucky", duration=369)
    assert best is not None and best["id"] == 3


def test_tidal_search_rejects_wrong_artist(monkeypatch):
    payload = {"items": [TIDAL_SEARCH["items"][0]]}
    monkeypatch.setattr(metadata_web, "_tidal_get", lambda path, params=None: payload)
    assert metadata_web.tidal_search_track("Daft Punk", "Get Lucky", duration=370) is None


def test_tidal_search_requires_title_overlap(monkeypatch):
    payload = {"items": [{"id": 9, "title": "One More Time", "artist": {"name": "Daft Punk"}, "duration": 320}]}
    monkeypatch.setattr(metadata_web, "_tidal_get", lambda path, params=None: payload)
    assert metadata_web.tidal_search_track("Daft Punk", "Get Lucky", duration=320) is None


TIDAL_TRACK = {
    "id": 3,
    "title": "Get Lucky",
    "duration": 370,
    "isrc": "USQX91300108",
    "copyright": "(P) 2013 Daft Life",
    "trackNumber": 8,
    "volumeNumber": 1,
    "bpm": 116,
    "key": "B",
    "audioQuality": "LOSSLESS",
    "explicit": False,
    "replayGain": -7.55,
    "peak": 0.999,
    "album": {"id": 55, "title": "Random Access Memories", "cover": "aa-bb-cc"},
}
TIDAL_ALBUM = {
    "id": 55,
    "title": "Random Access Memories",
    "numberOfVolumes": 2,
    "numberOfTracks": 13,
    "releaseDate": "2013-05-20",
    "upc": "886443919266",
    "copyright": "(P) 2013 Daft Life",
}


def test_tidal_track_extras_maps_album_fields(monkeypatch):
    def fake_get(path, params=None):
        if path.startswith("/tracks/"):
            return TIDAL_TRACK
        if path.startswith("/albums/"):
            return TIDAL_ALBUM
        return None

    monkeypatch.setattr(metadata_web, "_tidal_get", fake_get)
    extras = metadata_web.tidal_track_extras(3)

    assert extras["isrc"] == "USQX91300108"
    assert extras["disc_number"] == 1 and extras["disc_total"] == 2
    assert extras["track_number"] == 8 and extras["track_total"] == 13
    assert extras["upc"] == "886443919266"
    assert extras["bpm"] == 116 and extras["initial_key"] == "B"
    assert extras["replaygain_track_gain"] == pytest.approx(-7.55)
    assert extras["cover_url"] == "https://resources.tidal.com/images/aa/bb/cc/1280x1280.jpg"


# ─────────────────────────────── merge ───────────────────────────────


def test_collect_extras_merges_apple_and_tidal(monkeypatch):
    monkeypatch.setattr(
        metadata_web,
        "apple_search_song",
        lambda *a, **kw: {"id": 7, "attributes": {}},
    )
    monkeypatch.setattr(
        metadata_web,
        "apple_song_extras",
        lambda song: {"composer": "Nile Rodgers", "label": "Columbia", "isrc": "FROM-APPLE", "genre": "Pop"},
    )
    monkeypatch.setattr(
        metadata_web,
        "tidal_search_track",
        lambda *a, **kw: {"id": 3},
    )
    monkeypatch.setattr(
        metadata_web,
        "tidal_track_extras",
        lambda track_id: {"isrc": "FROM-TIDAL", "bpm": 116, "initial_key": "B"},
    )

    extras = metadata_web.collect_extras("Daft Punk", "Get Lucky", duration=369)

    # Apple идёт первым — его ISRC побеждает, TIDAL добивает пустые поля.
    assert extras["isrc"] == "FROM-APPLE"
    assert extras["composer"] == "Nile Rodgers"
    assert extras["bpm"] == 116
    assert extras["initial_key"] == "B"


def test_collect_extras_survives_broken_source(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(metadata_web, "apple_search_song", boom)
    monkeypatch.setattr(metadata_web, "tidal_search_track", lambda *a, **kw: {"id": 3})
    monkeypatch.setattr(metadata_web, "tidal_track_extras", lambda track_id: {"isrc": "FROM-TIDAL"})

    extras = metadata_web.collect_extras("Daft Punk", "Get Lucky", duration=369)
    assert extras == {"isrc": "FROM-TIDAL"}


def test_collect_extras_returns_empty_without_query():
    assert metadata_web.collect_extras() == {}


# ────────────────────── запись тегов в файлы ──────────────────────

EXTRAS = {
    "isrc": "USQX91300108",
    "composer": "Pharrell Williams, Nile Rodgers",
    "label": "Columbia",
    "copyright": "℗ 2013 Daft Life Limited",
    "disc_number": 1,
    "disc_total": 2,
    "track_number": 8,
    "track_total": 13,
    "release_date": "2013-04-19",
    "genre": "Pop",
    "bpm": 116,
    "initial_key": "B",
    "upc": "886443919266",
    "replaygain_track_gain": -7.55,
    "replaygain_track_peak": 0.999,
}

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg не установлен")


def _make_audio(path: Path, codec_args: list[str]) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            *codec_args,
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


@needs_ffmpeg
def test_apply_extended_metadata_flac(tmp_path):
    track = _make_audio(tmp_path / "t.flac", ["-c:a", "flac"])
    apply_metadata(track, artist="Daft Punk", title="Get Lucky")
    assert apply_extended_metadata(track, EXTRAS) is True

    tags = FLAC(str(track))
    assert tags["ISRC"] == ["USQX91300108"]
    assert tags["COMPOSER"] == ["Pharrell Williams, Nile Rodgers"]
    assert tags["LABEL"] == ["Columbia"]
    assert tags["DISCNUMBER"] == ["1"] and tags["DISCTOTAL"] == ["2"]
    assert tags["TRACKTOTAL"] == ["13"]
    assert tags["BPM"] == ["116"] and tags["INITIALKEY"] == ["B"]
    assert tags["REPLAYGAIN_TRACK_GAIN"] == ["-7.55 dB"]


@needs_ffmpeg
def test_apply_extended_metadata_mp3(tmp_path):
    track = _make_audio(tmp_path / "t.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"])
    apply_metadata(track, artist="Daft Punk", title="Get Lucky")
    assert apply_extended_metadata(track, EXTRAS) is True

    tags = ID3(str(track))
    assert str(tags["TSRC"]) == "USQX91300108"
    assert str(tags["TCOM"]) == "Pharrell Williams, Nile Rodgers"
    assert str(tags["TPUB"]) == "Columbia"
    assert str(tags["TPOS"]) == "1/2" and str(tags["TRCK"]) == "8/13"
    assert str(tags["TBPM"]) == "116" and str(tags["TKEY"]) == "B"
    assert str(tags["TXXX:REPLAYGAIN_TRACK_GAIN"]) == "-7.55 dB"


@needs_ffmpeg
def test_apply_extended_metadata_m4a(tmp_path):
    track = _make_audio(tmp_path / "t.m4a", ["-c:a", "aac", "-b:a", "128k"])
    apply_metadata(track, artist="Daft Punk", title="Get Lucky")
    assert apply_extended_metadata(track, EXTRAS) is True

    tags = MP4(str(track))
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USQX91300108"
    assert tags["\xa9wrt"] == ["Pharrell Williams, Nile Rodgers"]
    assert tags["cprt"] == ["℗ 2013 Daft Life Limited"]
    assert tags["disk"] == [(1, 2)] and tags["trkn"] == [(8, 13)]
    assert tags["tmpo"] == [116]


@needs_ffmpeg
def test_apply_extended_metadata_ignores_empty_values(tmp_path):
    track = _make_audio(tmp_path / "t.flac", ["-c:a", "flac"])
    apply_metadata(track, artist="Daft Punk", title="Get Lucky")
    apply_extended_metadata(track, {"isrc": "", "label": None, "bpm": ""})

    tags = FLAC(str(track))
    assert "ISRC" not in tags and "LABEL" not in tags and "BPM" not in tags


@needs_ffmpeg
def test_download_pipeline_writes_extended_tags(monkeypatch, tmp_path):
    """Проверяет саму проводку: download_track_by_link -> collect_extras -> теги."""
    import core as core_mod

    source = _make_audio(tmp_path / "src.flac", ["-c:a", "flac"])

    monkeypatch.setattr(core_mod.config, "SLSK_USER", "", raising=False)
    monkeypatch.setattr(core_mod.config, "SLSKD_URL", "", raising=False)
    monkeypatch.setattr(core_mod, "search_deezer_track", lambda *a, **kw: None)
    monkeypatch.setattr(core_mod, "search_soulseek", lambda *a, **kw: [])
    monkeypatch.setattr(core_mod, "download_youtube_track", lambda *a, **kw: source)
    monkeypatch.setattr(
        metadata_web,
        "collect_extras",
        lambda **kw: {"isrc": "USQX91300108", "label": "Columbia", "bpm": 116},
    )

    meta = {
        "title": "Get Lucky",
        "artist": "Daft Punk",
        "album": "Random Access Memories",
        "duration": 369,
    }
    result = core_mod.download_track_by_link(
        "Daft Punk - Get Lucky",
        target_quality="MP3",
        dest_dir=tmp_path,
        track_meta=meta,
        reuse_cached_file=False,
    )

    assert result is not None and result.exists()
    # Новый контракт: MP3-запрос всегда нормализуется в .mp3 —
    # исходник-FLAC конвертируется в честный 320k CBR до тегирования.
    assert result.suffix == ".mp3"
    from mutagen.mp3 import MP3

    tags = MP3(str(result))
    assert tags["TXXX:ISRC"].text[0] == "USQX91300108"
    assert tags["TXXX:LABEL"].text[0] == "Columbia"
    assert tags["TBPM"].text[0] == "116"
