from unittest.mock import patch, MagicMock
from pathlib import Path
import pytest

from core.metadata import (
    resolve_lastfm_track,
    resolve_vk_track,
    search_multisource_tracks,
    get_track_metadata,
)
from core import search_tracks, download_track_by_link
from cli.main import (
    format_search_result_item,
    select_candidate_interactive,
    cli_search_and_download,
)


def test_resolve_lastfm_track_simple():
    """Тест парсинга стандартной ссылки Last.fm: /music/Artist/_/Track."""
    with patch("core.metadata.fetch_deezer_metadata") as mock_dz, \
         patch("core.metadata.fetch_itunes_metadata") as mock_it, \
         patch("core.sources.deezer.search_deezer_track", return_value="12345"):
        mock_dz.return_value = {
            "title": "Get Lucky",
            "artist": "Daft Punk",
            "album": "Random Access Memories",
            "year": "2013",
            "album_art": "https://e-cdns-images.dzcdn.net/cover.jpg",
            "duration": 248,
            "isrc": "USQX91300105",
        }
        mock_it.return_value = None

        meta = resolve_lastfm_track("https://www.last.fm/music/Daft+Punk/_/Get+Lucky")
        assert meta is not None
        assert meta["artist"] == "Daft Punk"
        assert meta["title"] == "Get Lucky"
        assert meta["year"] == "2013"
        assert meta["album"] == "Random Access Memories"


def test_resolve_lastfm_track_with_album_and_locale():
    """Тест парсинга локализованной ссылки Last.fm с альбомом: /ru/music/Artist/Album/Track."""
    with patch("core.metadata.fetch_deezer_metadata", return_value=None), \
         patch("core.metadata.fetch_itunes_metadata", return_value=None):
        meta = resolve_lastfm_track("https://last.fm/ru/music/Queen/A+Night+at+the+Opera/Bohemian+Rhapsody")
        assert meta is not None
        assert meta["artist"] == "Queen"
        assert meta["album"] == "A Night at the Opera"
        assert meta["title"] == "Bohemian Rhapsody"


def test_resolve_lastfm_track_invalid():
    """Не трековые ссылки Last.fm должны возвращать None."""
    assert resolve_lastfm_track("https://www.last.fm/music/Daft+Punk") is None
    assert resolve_lastfm_track("https://www.last.fm/music/Daft+Punk/+wiki") is None
    assert resolve_lastfm_track("https://example.com/not-lastfm") is None


def test_get_track_metadata_dispatches_lastfm():
    """Проверка диспетчеризации get_track_metadata на Last.fm."""
    with patch("core.metadata.resolve_lastfm_track") as mock_resolve:
        mock_resolve.return_value = {"artist": "Nirvana", "title": "Smells Like Teen Spirit"}
        res = get_track_metadata("https://www.last.fm/music/Nirvana/_/Smells+Like+Teen+Spirit")
        assert res == {"artist": "Nirvana", "title": "Smells Like Teen Spirit"}
        mock_resolve.assert_called_once()


def test_resolve_vk_track_with_query_param():
    """Тест извлечения трека из ссылки VK с query-параметром ?q=Artist+-+Title."""
    meta = resolve_vk_track("https://m.vk.com/audio?q=The+Beatles+-+Yesterday")
    assert meta is not None
    assert meta["artist"] == "The Beatles"
    assert meta["title"] == "Yesterday"


def test_resolve_vk_track_with_token_and_api():
    """Тест работы VK API audio.getById при заданном VK_TOKEN."""
    with patch("config.VK_TOKEN", "mock_vk_token_123"):
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "response": [
                    {
                        "id": 12345,
                        "owner_id": -2001,
                        "artist": "Linkin Park",
                        "title": "Numb",
                        "duration": 187,
                        "url": "https://cs1-23.vkusercontents.com/audio/linkin_park_numb.mp3",
                        "album": {
                            "title": "Meteora",
                            "thumb": {"photo_600": "https://sun6-20.userapi.com/cover.jpg"}
                        }
                    }
                ]
            }
            mock_get.return_value = mock_resp

            meta = resolve_vk_track("https://vk.com/audio-2001_12345")
            assert meta is not None
            assert meta["artist"] == "Linkin Park"
            assert meta["title"] == "Numb"
            assert meta["duration"] == 187
            assert meta["album"] == "Meteora"
            assert meta["direct_url"] == "https://cs1-23.vkusercontents.com/audio/linkin_park_numb.mp3"


def test_resolve_vk_track_without_token_graceful():
    """Тест вызова VK audio ссылки без токена: не падает и корректно сообщает."""
    with patch("config.VK_TOKEN", ""):
        meta = resolve_vk_track("https://vk.com/audio-2001_12345")
        # Без токена и без query param q= метаданные не извлекутся
        assert meta is None


def test_search_multisource_tracks_aggregates_and_deduplicates():
    """Тест агрегации и дедупликации результатов из Deezer, iTunes и YouTube Music."""
    mock_deezer_resp = MagicMock()
    mock_deezer_resp.status_code = 200
    mock_deezer_resp.json.return_value = {
        "data": [
            {
                "id": 101,
                "title": "Bohemian Rhapsody",
                "artist": {"name": "Queen"},
                "album": {"title": "A Night at the Opera", "release_date": "1975-11-21", "cover_xl": "https://dz/art.jpg"},
                "duration": 354,
                "explicit_lyrics": False,
            }
        ]
    }

    mock_itunes_resp = MagicMock()
    mock_itunes_resp.status_code = 200
    mock_itunes_resp.json.return_value = {
        "results": [
            {
                "trackName": "Bohemian Rhapsody",
                "artistName": "Queen",
                "collectionName": "A Night at the Opera (Deluxe)",
                "releaseDate": "1975-11-21T00:00:00Z",
                "trackTimeMillis": 354000,
                "artworkUrl100": "https://itunes/100x100bb.jpg",
                "trackViewUrl": "https://music.apple.com/track/101",
            },
            {
                "trackName": "Radio Ga Ga",
                "artistName": "Queen",
                "collectionName": "The Works",
                "releaseDate": "1984-02-27T00:00:00Z",
                "trackTimeMillis": 348000,
                "artworkUrl100": "https://itunes/radio100.jpg",
                "trackViewUrl": "https://music.apple.com/track/102",
            }
        ]
    }

    with patch("httpx.get") as mock_get, \
         patch("ytmusicapi.YTMusic.search", return_value=[]):
        def side_effect(url, **kwargs):
            if "deezer" in str(url):
                return mock_deezer_resp
            elif "apple" in str(url):
                return mock_itunes_resp
            m = MagicMock()
            m.status_code = 404
            return m
        mock_get.side_effect = side_effect

        results = search_multisource_tracks("Queen Bohemian Rhapsody", limit=5)
        assert len(results) >= 2
        # Первый результат должен быть Bohemian Rhapsody (высокий скор)
        assert results[0]["artist"] == "Queen"
        assert "Bohemian Rhapsody" in results[0]["title"]
        # Дедупликация: Queen - Bohemian Rhapsody должна быть представлена одной записью
        queen_bohemian = [r for r in results if r["artist"] == "Queen" and r["title"] == "Bohemian Rhapsody"]
        assert len(queen_bohemian) == 1
        # Также присутствует Radio Ga Ga
        queen_radio = [r for r in results if r["title"] == "Radio Ga Ga"]
        assert len(queen_radio) == 1


def test_format_search_result_item():
    """Тест форматирования элемента выдачи в консоли."""
    item = {
        "artist": "Queen",
        "title": "Bohemian Rhapsody",
        "album": "A Night at the Opera",
        "year": "1975",
        "duration": 354,
        "source_quality": "Deezer (FLAC / 320k)",
        "explicit": False,
    }
    line = format_search_result_item(1, item)
    assert "[1] Queen - Bohemian Rhapsody" in line
    assert "A Night at the Opera" in line
    assert "1975" in line
    assert "5:54" in line
    assert "[Deezer (FLAC / 320k)]" in line


def test_select_candidate_interactive():
    """Тест интерактивного выбора трека."""
    candidates = [
        {"artist": "Artist 1", "title": "Track 1"},
        {"artist": "Artist 2", "title": "Track 2"},
    ]

    # Enter -> выбирает первый по умолчанию
    with patch("builtins.input", return_value=""):
        choice = select_candidate_interactive(candidates)
        assert choice == candidates[0]

    # Выбор 2
    with patch("builtins.input", return_value="2"):
        choice = select_candidate_interactive(candidates)
        assert choice == candidates[1]

    # Выбор 0 -> отмена
    with patch("builtins.input", return_value="0"):
        choice = select_candidate_interactive(candidates)
        assert choice is None


def test_download_track_by_link_with_direct_vk_stream(tmp_path):
    """Тест прямого скачивания MP3 из direct_url (например, VK CDN) без YouTube фолбека."""
    mock_meta = {
        "artist": "Test Artist",
        "title": "Test Title",
        "album": "Test Album",
        "year": "2024",
        "direct_url": "https://cs1-23.vkusercontents.com/audio/test.mp3",
        "vk_url": "https://vk.com/audio-1_2",
    }

    with patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("httpx.stream") as mock_stream, \
         patch("core.tagger.apply_metadata") as mock_tagger:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_bytes.return_value = [b"ID3\x03\x00\x00\x00\x00\x00\x00FAKE_MP3_DATA"]
        mock_stream.return_value.__enter__.return_value = mock_response

        saved_path = download_track_by_link(
            "https://vk.com/audio-1_2",
            target_quality="MP3",
            dest_dir=tmp_path
        )
        assert saved_path is not None
        assert saved_path.exists()
        assert saved_path.suffix == ".mp3"
        mock_tagger.assert_called_once()
