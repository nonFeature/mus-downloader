import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.sources.youtube import extract_youtube_video_id
from core.metadata import clean_youtube_title_and_artist, resolve_youtube_track
from core import download_track_by_link
import config


def test_extract_youtube_video_id_various_formats():
    urls = [
        "https://www.youtube.com/watch?v=kffacxfA7G4",
        "https://www.youtube.com/watch?v=kffacxfA7G4&feature=related",
        "https://youtu.be/kffacxfA7G4",
        "https://youtu.be/kffacxfA7G4?si=abcdef",
        "https://www.youtube.com/shorts/kffacxfA7G4",
        "https://www.youtube.com/live/kffacxfA7G4",
        "https://www.youtube.com/embed/kffacxfA7G4",
        "https://music.youtube.com/watch?v=kffacxfA7G4&list=RD",
        "kffacxfA7G4",
        ' "https://www.youtube.com/watch?v=kffacxfA7G4" ',
    ]
    for u in urls:
        assert extract_youtube_video_id(u) == "kffacxfA7G4", f"Failed for {u}"

    assert extract_youtube_video_id("https://open.spotify.com/track/123") is None
    assert extract_youtube_video_id("") is None
    assert extract_youtube_video_id(None) is None


def test_clean_youtube_title_and_artist():
    # 1. Standard official music video with VEVO channel
    a, t = clean_youtube_title_and_artist("Justin Bieber - Baby ft. Ludacris (Official Music Video)", "JustinBieberVEVO")
    assert a == "Justin Bieber"
    assert t == "Baby ft. Ludacris"

    # 2. Remastered video with Official channel suffix
    a, t = clean_youtube_title_and_artist("Queen - Bohemian Rhapsody (Official Video Remastered)", "Queen Official")
    assert a == "Queen"
    assert t == "Bohemian Rhapsody"

    # 3. Topic channel with clean title
    a, t = clean_youtube_title_and_artist("Blinding Lights", "The Weeknd - Topic")
    assert a == "The Weeknd"
    assert t == "Blinding Lights"

    # 4. Audio tag removal
    a, t = clean_youtube_title_and_artist("Imagine Dragons - Believer (Audio)", "Imagine Dragons")
    assert a == "Imagine Dragons"
    assert t == "Believer"

    # 5. Russian text and brackets
    a, t = clean_youtube_title_and_artist("Miyagi & Эндшпиль - I Got Love (feat. Рем Дигга) [Official Video]", "Hajime")
    assert a == "Miyagi & Эндшпиль"
    assert t == "I Got Love (feat. Рем Дигга)"

    # 6. Deduplication if artist repeated in title
    a, t = clean_youtube_title_and_artist("Eminem - Eminem - Houdini", "Eminem")
    assert a == "Eminem"
    assert t == "Houdini"


def test_resolve_youtube_track_with_crossplatform_mock():
    mock_oembed = MagicMock()
    mock_oembed.status_code = 200
    mock_oembed.json.return_value = {
        "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
        "author_name": "Rick Astley",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    }

    mock_itunes = {
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "album": "Whenever You Need Somebody",
        "year": "1987",
        "track_number": 1,
        "track_total": 10,
        "album_artist": "Rick Astley",
        "album_art": "https://is1-ssl.mzstatic.com/image/thumb/1000x1000bb.jpg",
        "duration": 213.0,
        "apple_music_url": "https://music.apple.com/us/album/never-gonna-give-you-up/12345?i=12345"
    }

    mock_songlink = {
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "deezer_id": "3135556",
        "spotify_url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "isrc": "GBARL8700041"
    }

    mock_deezer_meta = {
        "isrc": "GBARL8700041",
        "album": "Whenever You Need Somebody",
        "year": "1987",
        "track_number": 1,
        "album_artist": "Rick Astley",
        "album_art": "https://e-cdns-images.dzcdn.net/images/cover/1000x1000-000000-80-0-0.jpg",
        "duration": 213,
        "explicit": False,
    }

    with patch("httpx.get", return_value=mock_oembed), \
         patch("core.metadata.fetch_itunes_metadata", return_value=mock_itunes), \
         patch("core.metadata.resolve_song_link", return_value=mock_songlink), \
         patch("core.metadata.fetch_deezer_metadata", return_value=mock_deezer_meta):

        res = resolve_youtube_track("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        assert res is not None
        assert res["title"] == "Never Gonna Give You Up"
        assert res["artist"] == "Rick Astley"
        assert res["album"] == "Whenever You Need Somebody"
        assert res["year"] == "1987"
        assert res["deezer_id"] == "3135556"
        assert res["isrc"] == "GBARL8700041"
        assert res["direct_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_link_searches_deezer_first_in_mp3(tmp_path):
    dummy_deezer = tmp_path / "Rick Astley - Never Gonna Give You Up.mp3"
    dummy_deezer.write_bytes(b"deezer_320" * 20)

    mock_meta = {
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "album": "Whenever You Need Somebody",
        "deezer_id": "3135556",
        "direct_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "duration": 213.0,
    }

    with patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("core.download_deezer_track", return_value=dummy_deezer) as mock_dz, \
         patch("core.search_soulseek") as mock_slsk, \
         patch("core.download_youtube_track") as mock_yt, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", target_quality="MP3", dest_dir=tmp_path)

        # Deezer must be searched and downloaded first!
        assert mock_dz.called
        assert not mock_slsk.called
        assert not mock_yt.called
        assert res == dummy_deezer


def test_youtube_link_falls_back_to_soulseek_when_deezer_fails(tmp_path):
    dummy_slsk = tmp_path / "Artist - Track.mp3"
    dummy_slsk.write_bytes(b"slsk_320" * 20)

    mock_meta = {
        "title": "Track",
        "artist": "Artist",
        "deezer_id": "12345",
        "direct_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "duration": 180.0,
    }

    slsk_cand = [{
        "slskd_username": "peer1",
        "slskd_filename": "track.mp3",
        "slskd_size": 8000000,
        "quality": "320 kbps",
    }]

    with patch.object(config, "SLSKD_URL", "http://localhost:5030"), \
         patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("core.download_deezer_track", return_value=None), \
         patch("core.search_soulseek", return_value=slsk_cand), \
         patch("core.download_soulseek_track", return_value=dummy_slsk) as mock_slsk_dl, \
         patch("core.download_youtube_track") as mock_yt, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("https://www.youtube.com/watch?v=abcdefghijk", target_quality="MP3", dest_dir=tmp_path)

        assert mock_slsk_dl.called
        assert not mock_yt.called
        assert res == dummy_slsk


def test_youtube_link_falls_back_to_direct_youtube_when_others_fail(tmp_path):
    dummy_yt = tmp_path / "Rare Artist - Unreleased Track.mp3"
    dummy_yt.write_bytes(b"yt_opus_to_mp3" * 20)

    mock_meta = {
        "title": "Unreleased Track",
        "artist": "Rare Artist",
        "deezer_id": None,
        "direct_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "duration": 150.0,
    }

    with patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("core.search_soulseek", return_value=[]), \
         patch("core.download_youtube_track", return_value=dummy_yt) as mock_yt, \
         patch("core.tagger.apply_metadata"):

        # For a YouTube link, when Deezer and Soulseek fail, it directly downloads
        # the user's direct YouTube link without fuzzy-searching YouTube Music
        res = download_track_by_link("https://www.youtube.com/watch?v=abcdefghijk", target_quality="MP3", dest_dir=tmp_path)

        assert mock_yt.call_count == 1
        assert mock_yt.call_args.kwargs.get("direct_url") == "https://www.youtube.com/watch?v=abcdefghijk"
        assert res == dummy_yt
