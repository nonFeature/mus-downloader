"""Тесты агрегатора поиска: junk-штраф и Spotify-маппинг (без сети)."""
from core import metadata
from core import metadata_spotify


# ─────────────────────────── junk-фильтр ───────────────────────────


def test_junk_penalty_karaoke_and_tribute():
    cands = [
        {"artist": "Kanye West", "title": "Stronger", "score": 1.0},
        {"artist": "Karaoke Tribute Band", "title": "Stronger (Karaoke Version)", "score": 1.0},
        {"artist": "String Quartet", "title": "Stronger (String Quartet Version)", "score": 1.0},
    ]
    metadata._apply_junk_penalty(cands, "Stronger")
    assert cands[0]["score"] == 1.0
    assert cands[1]["score"] < 0.3
    assert cands[2]["score"] < 0.3


def test_junk_penalty_various_artists():
    cands = [
        {"artist": "Various Artists", "title": "Daft Punk - Get Lucky", "score": 0.96},
        {"artist": "Daft Punk", "title": "Get Lucky", "score": 0.92},
    ]
    metadata._apply_junk_penalty(cands, "Daft Punk Get Lucky")
    assert cands[0]["score"] < cands[1]["score"]  # оригинал теперь выше кавера-сборника


def test_junk_penalty_va_respects_query():
    cands = [{"artist": "Various Artists", "title": "Summer Hits", "score": 1.0}]
    metadata._apply_junk_penalty(cands, "various artists summer hits")
    assert cands[0]["score"] == 1.0  # пользователь сам ищет сборник


def test_junk_penalty_cover_in_title():
    cands = [{"artist": "Someone", "title": "Get Lucky (Cover Version)", "score": 1.0}]
    metadata._apply_junk_penalty(cands, "get lucky")
    assert cands[0]["score"] < 0.3


def test_junk_penalty_cover_with_original_artist_in_parens():
    # Кавер от постороннего артиста с именем оригинала в скобках
    cands = [{"artist": "Charlie Parra del Riego", "title": "Get Lucky (Daft Punk)", "score": 0.92}]
    metadata._apply_junk_penalty(cands, "Daft Punk Get Lucky")
    assert cands[0]["score"] < 0.3


def test_junk_penalty_imitation_artist():
    # «Daft Punk Experience» — имитатор: содержит имя целиком + довесок
    cands = [
        {"artist": "Daft Punk Experience, Zico Oliveira", "title": "Get Lucky", "score": 0.94},
        {"artist": "Daft Punk", "title": "Get Lucky", "score": 0.92},
    ]
    metadata._apply_junk_penalty(cands, "Daft Punk Get Lucky")
    assert cands[0]["score"] < 0.3
    assert cands[1]["score"] == 0.92  # оригинал не тронут


def test_junk_penalty_legit_collab_not_penalized():
    # Легальный коллаб: одна из частей артиста совпадает с именем из запроса точно
    cands = [
        {"artist": "Daft Punk, Pharrell Williams, Nile Rodgers", "title": "Get Lucky", "score": 0.92},
    ]
    metadata._apply_junk_penalty(cands, "Daft Punk Get Lucky")
    assert cands[0]["score"] == 0.92


def test_junk_penalty_no_artist_in_query_noop():
    # Запрос без артиста (только название) — правила имитатора/кавера не срабатывают
    cands = [{"artist": "Random Singer", "title": "Get Lucky", "score": 0.9}]
    metadata._apply_junk_penalty(cands, "Get Lucky")
    assert cands[0]["score"] == 0.9


# ─────────────────────────── spotify search mapping ───────────────────────────


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_search_spotify_maps_candidates(monkeypatch):
    payload = {
        "data": {
            "searchV2": {
                "tracksV2": {
                    "items": [
                        {
                            "track": {
                                "id": "abc123",
                                "name": "Get Lucky",
                                "artists": {"items": [
                                    {"profile": {"name": "Daft Punk"}},
                                    {"profile": {"name": "Pharrell Williams"}},
                                ]},
                                "albumOfTrack": {
                                    "name": "Random Access Memories",
                                    "coverArt": {"sources": [{"url": "https://i.scdn.co/x.jpg"}]},
                                    "date": {"year": 2013},
                                },
                                "duration": {"totalMilliseconds": 369000},
                                "contentRating": {"label": "NOT_EXPLICIT"},
                            }
                        },
                        {
                            "track": {
                                "id": "def456",
                                "name": "Harder Better Faster Stronger",
                                "artists": {"items": [{"profile": {"name": "Daft Punk"}}]},
                                "albumOfTrack": {"name": "Discovery"},
                                "duration": {"totalMilliseconds": 225000},
                                "contentRating": {"label": "EXPLICIT"},
                            }
                        },
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(metadata_spotify, "_request", lambda *a, **k: _FakeResponse(payload))

    out = metadata_spotify.search_spotify("Daft Punk", limit=10)
    assert len(out) == 2
    c = out[0]
    assert c["id"] == "abc123"
    assert c["artist"] == "Daft Punk, Pharrell Williams"
    assert c["album"] == "Random Access Memories"
    assert c["year"] == "2013"
    assert c["duration"] == 369.0
    assert c["explicit"] is False
    assert c["album_art"] == "https://i.scdn.co/x.jpg"
    assert out[1]["explicit"] is True
    assert out[1]["album_art"] is None


def test_search_spotify_error_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(metadata_spotify, "_request", _boom)
    assert metadata_spotify.search_spotify("anything") == []


def test_search_spotify_empty_query():
    assert metadata_spotify.search_spotify("  ") == []
