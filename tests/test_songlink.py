"""
Unit tests for Songlink/Odesli metadata resolver in metadata.py.
Tests cover:
- Parsing official Odesli API responses
- Extraction of cross-platform URLs (Deezer, Spotify, YouTube Music, SoundCloud, Yandex)
- Integration with SONGLINK_API_KEY
- Web-scraping fallback (__NEXT_DATA__) when no API key is provided
"""

from unittest.mock import MagicMock, patch
import metadata
import config


SAMPLE_ODESLI_API_RESPONSE = {
    "entityUniqueId": "SPOTIFY_SONG::4cOdK2wGLETKBW3PvgPWqT",
    "userCountry": "US",
    "pageUrl": "https://song.link/s/4cOdK2wGLETKBW3PvgPWqT",
    "entitiesByUniqueId": {
        "SPOTIFY_SONG::4cOdK2wGLETKBW3PvgPWqT": {
            "id": "4cOdK2wGLETKBW3PvgPWqT",
            "type": "song",
            "title": "Never Gonna Give You Up",
            "artistName": "Rick Astley",
            "albumName": "Whenever You Need Somebody",
            "thumbnailUrl": "https://i.scdn.co/image/ab67616d0000b2735755e164993798e0c9ef7d7a",
            "apiProvider": "spotify",
        },
        "DEEZER_SONG::3135556": {
            "id": "3135556",
            "type": "song",
            "title": "Never Gonna Give You Up",
            "artistName": "Rick Astley",
            "apiProvider": "deezer",
        },
    },
    "linksByPlatform": {
        "spotify": {
            "url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "entityUniqueId": "SPOTIFY_SONG::4cOdK2wGLETKBW3PvgPWqT",
        },
        "deezer": {
            "url": "https://www.deezer.com/track/3135556",
            "entityUniqueId": "DEEZER_SONG::3135556",
        },
        "youtubeMusic": {
            "url": "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "entityUniqueId": "YOUTUBE_SONG::dQw4w9WgXcQ",
        },
        "soundcloud": {
            "url": "https://soundcloud.com/rick-astley/never-gonna-give-you-up",
            "entityUniqueId": "SOUNDCLOUD_SONG::123",
        },
        "yandex": {
            "url": "https://music.yandex.ru/track/12345",
            "entityUniqueId": "YANDEX_SONG::12345",
        },
    },
}


def test_parse_odesli_api_data():
    """Тестирует корректность разбора JSON-ответа от API Odesli."""
    res = metadata._parse_odesli_api_data(SAMPLE_ODESLI_API_RESPONSE)
    assert res is not None
    assert res["title"] == "Never Gonna Give You Up"
    assert res["artist"] == "Rick Astley"
    assert res["album"] == "Whenever You Need Somebody"
    assert res["deezer_id"] == "3135556"
    assert res["spotify_url"] == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    assert res["youtube_music_url"] == "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
    assert res["soundcloud_url"] == "https://soundcloud.com/rick-astley/never-gonna-give-you-up"
    assert res["yandex_url"] == "https://music.yandex.ru/track/12345"


def test_parse_odesli_api_data_empty():
    """Пустой или некорректный ответ API возвращает None."""
    assert metadata._parse_odesli_api_data({}) is None
    assert metadata._parse_odesli_api_data({"entitiesByUniqueId": {}}) is None


def test_resolve_song_link_with_api_key():
    """При наличии SONGLINK_API_KEY запросы направляются в официальный REST API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_ODESLI_API_RESPONSE

    with patch.object(config, "SONGLINK_API_KEY", "secret_odesli_key"), \
         patch("httpx.get", return_value=mock_resp) as mock_get:

        res = metadata.resolve_song_link("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        assert res is not None
        assert res["title"] == "Never Gonna Give You Up"
        assert res["deezer_id"] == "3135556"

        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        assert "api.song.link/v1-alpha.1/links" in called_url
        assert "key=secret_odesli_key" in called_url


def test_resolve_song_link_fallback_to_web_scraper():
    """Без API ключа используется веб-парсер __NEXT_DATA__."""
    html_page = """
    <html>
    <body>
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "pageData": {
            "entityData": {
              "title": "Scraped Track",
              "artistName": "Scraped Artist",
              "albumName": "Scraped Album",
              "thumbnailUrl": "https://img.com/art.jpg",
              "releaseDate": {"year": 2024},
              "duration": 180000
            },
            "sections": [
              {
                "links": [
                  {"platform": "deezer", "url": "https://www.deezer.com/track/777888"}
                ]
              }
            ]
          }
        }
      }
    }
    </script>
    </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html_page

    with patch.object(config, "SONGLINK_API_KEY", ""), \
         patch("httpx.get", return_value=mock_resp) as mock_get:

        res = metadata.resolve_song_link("https://open.spotify.com/track/dummy")
        assert res is not None
        assert res["title"] == "Scraped Track"
        assert res["artist"] == "Scraped Artist"
        assert res["album"] == "Scraped Album"
        assert res["year"] == "2024"
        assert res["duration"] == 180.0
        assert res["deezer_id"] == "777888"

        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        assert "https://song.link/" in called_url
