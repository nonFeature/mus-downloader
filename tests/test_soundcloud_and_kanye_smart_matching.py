import re
from unittest.mock import patch, MagicMock
from pathlib import Path

from core.sources.deezer import is_junk_track, search_deezer_track
from core.sources.youtube_matcher import norm_text, toks
from core.metadata import (
    resolve_soundcloud_track,
    validate_deezer_track,
    get_track_metadata,
)
from core import download_track_by_link


def test_is_junk_track_filters_karaoke_and_tributes():
    """Проверяем, что эвристика отсекает караоке, звукоподражателей и трибьюты."""
    target = "Kanye West Flashing Lights"

    # Мусорные треки
    assert is_junk_track("Flashing Lights (Melody Karaoke Version)", "ZZang KARAOKE", target) is True
    assert is_junk_track("Flashing Lights (as made famous by Kanye West)", "Hip Hop Chart Toppers", target) is True
    assert is_junk_track("Flashing Lights (Originally Performed by Kanye West)", "Ameritz Tribute Club", target) is True
    assert is_junk_track("Flashing Lights (Tribute Version)", "The Hit Makers", target) is True
    assert is_junk_track("Flashing Lights (Instrumental)", "Smooth Jazz All Stars", target) is True
    assert is_junk_track("Flashing Lights (Lullaby Rendition)", "Rockabye Baby!", target) is True

    # Настоящие треки
    assert is_junk_track("Flashing Lights", "Kanye West", target) is False
    assert is_junk_track("Flashing Lights (feat. Dwele)", "Kanye West", target) is False
    assert is_junk_track("Flashing Lights", "Ye", target) is False

    # Тест на Otis (Kanye West & Jay-Z)
    target_otis = "Kanye West & JAŸ-Z Otis"
    assert is_junk_track("Otis (Originally By Jay Z & Kanye West Feat. Otis Redding)", "HOT 100", target_otis) is True
    assert is_junk_track("Otis (originally by Kanye West & JAY Z)", "Al the Music Box", target_otis) is True
    assert is_junk_track("Otis (Album Version Edited)", "JAY Z", target_otis) is False
    assert is_junk_track("Otis", "JAY Z", target_otis) is False


def test_norm_text_diacritics_normalization():
    """Проверяем нормализацию нестандартных Unicode символов (диакритики)."""
    assert norm_text("JAŸ-Z") == "jay-z"
    assert "jay" in toks("JAŸ-Z")
    assert "z" in toks("JAŸ-Z")
    assert norm_text("Beyoncé") == "beyonce"
    assert norm_text("Mötley Crüe") == "motley crue"
    assert norm_text("Björk") == "bjork"


def test_search_deezer_smart_scoring_without_hard_author_filter():
    """
    Проверяем, что поиск находит трек коллаборации, даже если в каталоге
    Deezer указан только один из соавторов (например JAY Z вместо Kanye West & JAŸ-Z),
    без жесткой фильтрации автор == целевой_автор.
    """
    mock_search_data = {
        "data": [
            {
                "id": 65601537,
                "title": "Otis (Album Version Edited)",
                "artist": {"name": "JAY Z"},
                "duration": 180,
                "explicit_lyrics": False,
            },
            {
                "id": 63018844,
                "title": "Otis (Originally By Jay Z & Kanye West Feat. Otis Redding)",
                "artist": {"name": "HOT 100"},
                "duration": 179,
                "explicit_lyrics": False,
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_search_data

    with patch("httpx.get", return_value=mock_resp):
        found_id = search_deezer_track("Kanye West & JAŸ-Z", "Otis (feat. Otis Redding)", duration=180)
        # Должен быть выбран именно официальный трек Jay-Z, а не HOT 100
        assert found_id == "65601537"


def test_validate_deezer_track_rejects_poisoned_songlink_id():
    """
    Проверяем, что отравленные связки от song.link
    (например, чужой трек совершенно другого исполнителя) отсекаются.
    """
    # Симулируем случай, когда song.link вернул трек Kashmala Gul для Flashing Lights
    mock_poisoned_deezer = {
        "title": "Che Mu Pora She da Nimgari Armanona",
        "artist": "Kashmala Gul",
        "album": "Pashto Hits",
        "duration": 240,
    }

    mock_valid_deezer = {
        "title": "Flashing Lights",
        "artist": "Kanye West",
        "album": "Graduation",
        "duration": 237,
    }

    with patch("core.metadata.fetch_deezer_metadata", side_effect=lambda tid: mock_poisoned_deezer if tid == "644339892" else mock_valid_deezer):
        # Отравленный ID отклоняется
        assert validate_deezer_track("644339892", "Flashing Lights", "Kanye West", target_duration=237) is None

        # Подлинный ID принимается
        res = validate_deezer_track("630827302", "Flashing Lights", "Kanye West", target_duration=237)
        assert res is not None
        assert res["title"] == "Flashing Lights"
        assert res["artist"] == "Kanye West"


def test_resolve_soundcloud_track_cleans_and_resolves():
    """
    Проверяем очистку заголовка SoundCloud, выбор обложки высокого разрешения (-original.jpg)
    и связку со стримингом.
    """
    mock_oembed_resp = MagicMock()
    mock_oembed_resp.status_code = 200
    mock_oembed_resp.json.return_value = {
        "title": "Skrillex, Fred again.. - Rumble [FREE DL] by Skrillex",
        "author_name": "Skrillex",
        "thumbnail_url": "https://i1.sndcdn.com/artworks-sample-t500x500.jpg"
    }

    mock_itunes = {
        "title": "Rumble",
        "artist": "Skrillex, Fred again.. & Flowdan",
        "album": "Quest For Fire",
        "year": "2023",
        "track_number": 2,
        "track_total": 13,
        "album_artist": "Skrillex",
        "album_art": "https://is1-ssl.mzstatic.com/image/thumb/1000x1000bb.jpg",
        "duration": 146.0,
        "apple_music_url": "https://music.apple.com/us/album/rumble/123?i=456",
        "isrc": "USAT22207138"
    }

    mock_songlink = {
        "deezer_id": "2104576307",
        "spotify_url": "https://open.spotify.com/track/rumble",
        "isrc": "USAT22207138"
    }

    mock_deezer = {
        "title": "Rumble",
        "artist": "Skrillex",
        "album": "Quest For Fire",
        "album_art": "https://e-cdns-images.dzcdn.net/images/cover/1000x1000.jpg",
        "duration": 146,
    }

    with patch("httpx.get", return_value=mock_oembed_resp), \
         patch("core.metadata.fetch_itunes_metadata", return_value=mock_itunes), \
         patch("core.metadata.resolve_song_link", return_value=mock_songlink), \
         patch("core.metadata.fetch_deezer_metadata", return_value=mock_deezer), \
         patch("core.metadata.fetch_lastfm_genres", return_value=["electronic", "dubstep"]):

        res = resolve_soundcloud_track("https://soundcloud.com/skrillex/skrillex-fred-again-flowdan-rumble")

        assert res is not None
        assert res["title"] == "Rumble"
        assert "Skrillex" in res["artist"]
        assert res["album"] == "Quest For Fire"
        assert res["deezer_id"] == "2104576307"
        assert res["soundcloud_url"] == "https://soundcloud.com/skrillex/skrillex-fred-again-flowdan-rumble"


def test_download_track_by_link_soundcloud_fallback(tmp_path):
    """
    Проверяем, что если трек SoundCloud отсутствует на Deezer и Soulseek,
    он скачивается напрямую из SoundCloud через download_soundcloud_track.
    """
    mock_sc_meta = {
        "title": "Unreleased Bootleg Remix",
        "artist": "Underground Producer",
        "soundcloud_url": "https://soundcloud.com/producer/unreleased-bootleg-remix",
        "album_art": "https://i1.sndcdn.com/artworks-sample-original.jpg",
        "deezer_id": None,
        "duration": 185,
    }

    dummy_sc_file = tmp_path / "Underground Producer - Unreleased Bootleg Remix.mp3"
    dummy_sc_file.write_bytes(b"ID3" + b"\x00" * 1024)

    with patch("core.metadata.get_track_metadata", return_value=mock_sc_meta), \
         patch("core.sources.deezer.search_deezer_track", return_value=None), \
         patch("core.download_soundcloud_track", return_value=dummy_sc_file) as mock_dl_sc, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link(
            "https://soundcloud.com/producer/unreleased-bootleg-remix",
            dest_dir=tmp_path,
            target_quality="MP3",
        )

        assert res == dummy_sc_file
        assert mock_dl_sc.called
