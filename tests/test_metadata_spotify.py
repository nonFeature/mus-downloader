"""Тесты Spotify-метаданных: TOTP, GID, protobuf и маппинг полей.

Сеть не используется — подменяются ``_request`` и функции поиска.
"""

import pytest

import core  # noqa: F401 — регистрирует legacy-алиасы
from core import metadata_spotify as ms
from core import metadata_web


# ─────────────────────── protobuf-кодировщик для фикстур ───────────────────────


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _len_field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value >= 0 else ((-value - 1) << 1) | 1


def _text_field(number: int, text: str) -> bytes:
    return _len_field(number, text.encode("utf-8"))


ALBUM_GID = bytes.fromhex("8f1ac2002a0f4ee4ae8981e75f2cea00")


def _album_proto(*, with_details: bool = True) -> bytes:
    payload = _len_field(1, ALBUM_GID)
    payload += _text_field(2, "Random Access Memories")
    payload += _text_field(5, "Columbia")
    payload += _len_field(
        6, _varint_field(1, _zigzag(2013)) + _varint_field(2, _zigzag(5)) + _varint_field(3, _zigzag(20))
    )
    payload += _varint_field(4, 1)
    if with_details:
        payload += _len_field(8, b"electronic") + _len_field(8, b"pop")
        payload += _len_field(10, _text_field(1, "upc") + _text_field(2, "886443927087"))
        payload += _len_field(13, _text_field(2, "(P) 2013 Daft Life Limited"))
        # два диска: 4 и 9 треков
        payload += _len_field(
            11,
            _varint_field(1, _zigzag(1)) + b"".join(_len_field(3, b"t") for _ in range(4)),
        )
        payload += _len_field(
            11,
            _varint_field(1, _zigzag(2)) + b"".join(_len_field(3, b"t") for _ in range(9)),
        )
    return payload


def _track_proto() -> bytes:
    payload = _text_field(2, "Get Lucky")
    payload += _len_field(3, _album_proto(with_details=False))
    payload += _varint_field(5, _zigzag(8))
    payload += _varint_field(6, _zigzag(1))
    payload += _varint_field(7, _zigzag(369626))
    payload += _varint_field(9, 1)
    payload += _len_field(10, _text_field(1, "isrc") + _text_field(2, "USQX91300108"))
    payload += _len_field(32, _text_field(2, "Daft Punk") + _varint_field(3, 1))
    payload += _len_field(32, _text_field(2, "Pharrell Williams") + _varint_field(3, 5))
    payload += _len_field(32, _text_field(2, "Nile Rodgers") + _varint_field(3, 5))
    return payload


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", payload: dict | None = None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self) -> dict:
        return self._payload or {}


# ────────────────────────────── TOTP / GID ──────────────────────────────


def test_totp_code_is_six_digits_and_window_stable():
    base = 1_700_000_010  # делится на 30 → начало окна
    first = ms.totp_code(now=base)
    second = ms.totp_code(now=base + 10)
    other = ms.totp_code(now=base + 30)
    assert first.isdigit() and len(first) == 6
    assert first == second          # одно окно 30 с
    assert first != other


def test_spotify_gid_matches_spclient_path():
    # Проверено живым запросом к spclient: этот ID вернул ISRC USQX91300108.
    assert ms.spotify_gid("69kOkLUCkxIZYexIgSG8rq") == "ca16ac687593462f8a7bb7dcd6e93154"


def test_spotify_id_from_url_variants():
    assert ms.spotify_id_from_url("https://open.spotify.com/track/69kOkLUCkxIZYexIgSG8rq?si=x") == "69kOkLUCkxIZYexIgSG8rq"
    assert ms.spotify_id_from_url("https://open.spotify.com/intl-ru/track/69kOkLUCkxIZYexIgSG8rq") == "69kOkLUCkxIZYexIgSG8rq"
    assert ms.spotify_id_from_url("spotify:track:69kOkLUCkxIZYexIgSG8rq") == "69kOkLUCkxIZYexIgSG8rq"
    assert ms.spotify_id_from_url("https://example.com/nope") == ""


# ─────────────────────────────── protobuf ───────────────────────────────


def test_fields_parses_varints_and_length_delimited():
    raw = _varint_field(5, 16) + _text_field(2, "Get Lucky") + _varint_field(9, 1)
    fields = ms._fields(raw)
    assert fields[5] == [16]
    assert ms._text(fields[2][0]) == "Get Lucky"
    assert fields[9] == [1]


def test_album_extras_reads_details():
    extras = ms._album_extras(_album_proto())
    assert extras["album"] == "Random Access Memories"
    assert extras["label"] == "Columbia"
    assert extras["upc"] == "886443927087"
    assert extras["copyright"] == "(P) 2013 Daft Life Limited"
    assert extras["release_date"] == "2013-05-20"
    assert extras["track_total"] == 13 and extras["disc_total"] == 2
    assert extras["genres"] == ["electronic", "pop"]
    assert extras["album_type"] == "album"


def test_spotify_track_extras_maps_track_and_album(monkeypatch):
    calls: list[str] = []

    def fake_request(method, url, allow_retry=True, **kwargs):
        calls.append(url)
        if "/album/" in url:
            return _FakeResponse(200, _album_proto())
        return _FakeResponse(200, _track_proto())

    monkeypatch.setattr(ms, "_request", fake_request)
    extras = ms.spotify_track_extras("69kOkLUCkxIZYexIgSG8rq")

    assert extras["isrc"] == "USQX91300108"
    # В трековом protobuf роли приходят как name + role=5 → это авторы.
    assert extras["composer"] == "Pharrell Williams; Nile Rodgers"
    assert extras["label"] == "Columbia"
    assert extras["upc"] == "886443927087"
    assert extras["copyright"] == "(P) 2013 Daft Life Limited"
    assert extras["disc_number"] == 1 and extras["disc_total"] == 2
    assert extras["track_number"] == 8 and extras["track_total"] == 13
    assert extras["release_date"] == "2013-05-20"
    assert extras["explicit"] is True
    assert any("/album/" in url for url in calls)


def test_spotify_track_extras_survives_missing_album(monkeypatch):
    monkeypatch.setattr(ms, "_request", lambda *a, **kw: _FakeResponse(404))
    assert ms.spotify_track_extras("69kOkLUCkxIZYexIgSG8rq") == {}


# ─────────────────────────────── поиск ───────────────────────────────


SEARCH_PAYLOAD = {
    "data": {
        "searchV2": {
            "tracksV2": {
                "items": [
                    {
                        "item": {
                            "data": {
                                "id": "aaaaaaaaaaaaaaaaaaaaaa",
                                "name": "Get Lucky (Karaoke Version)",
                                "artists": {"items": [{"profile": {"name": "Karaoke Hits"}}]},
                                "duration": {"totalMilliseconds": 370000},
                                "albumOfTrack": {"name": "Karaoke", "id": "album1"},
                            }
                        }
                    },
                    {
                        "item": {
                            "data": {
                                "id": "69kOkLUCkxIZYexIgSG8rq",
                                "name": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                                "artists": {
                                    "items": [
                                        {"profile": {"name": "Daft Punk"}},
                                        {"profile": {"name": "Pharrell Williams"}},
                                    ]
                                },
                                "duration": {"totalMilliseconds": 369626},
                                "albumOfTrack": {"name": "Random Access Memories", "id": "album2"},
                            }
                        }
                    },
                ]
            }
        }
    }
}


def test_search_spotify_track_prefers_real_track(monkeypatch):
    monkeypatch.setattr(ms, "_request", lambda *a, **kw: _FakeResponse(200, payload=SEARCH_PAYLOAD))
    found = ms.search_spotify_track("Daft Punk", "Get Lucky", duration=370)
    assert found is not None
    assert found["id"] == "69kOkLUCkxIZYexIgSG8rq"
    assert found["album"] == "Random Access Memories"


def test_search_spotify_track_rejects_other_song(monkeypatch):
    payload = {
        "data": {
            "searchV2": {
                "tracksV2": {
                    "items": [
                        {
                            "item": {
                                "data": {
                                    "id": "bbbbbbbbbbbbbbbbbbbbbb",
                                    "name": "One More Time",
                                    "artists": {"items": [{"profile": {"name": "Daft Punk"}}]},
                                    "duration": {"totalMilliseconds": 320000},
                                    "albumOfTrack": {"name": "Discovery", "id": "album3"},
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(ms, "_request", lambda *a, **kw: _FakeResponse(200, payload=payload))
    assert ms.search_spotify_track("Daft Punk", "Get Lucky", duration=320) is None


def test_search_spotify_track_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(ms, "_request", lambda *a, **kw: _FakeResponse(429))
    assert ms.search_spotify_track("Daft Punk", "Get Lucky") is None


# ─────────────────────────── встраивание в extras ───────────────────────────


def test_collect_extras_uses_spotify_first(monkeypatch):
    monkeypatch.setattr(
        ms, "search_spotify_track", lambda *a, **kw: {"id": "69kOkLUCkxIZYexIgSG8rq"}
    )
    monkeypatch.setattr(
        ms,
        "spotify_track_extras",
        lambda track_id: {"isrc": "FROM-SPOTIFY", "composer": "Nile Rodgers", "label": "Columbia"},
    )
    monkeypatch.setattr(metadata_web, "apple_search_song", lambda *a, **kw: None)
    monkeypatch.setattr(metadata_web, "tidal_search_track", lambda *a, **kw: None)

    extras = metadata_web.collect_extras("Daft Punk", "Get Lucky", duration=369)

    assert extras["isrc"] == "FROM-SPOTIFY"
    assert extras["composer"] == "Nile Rodgers"
