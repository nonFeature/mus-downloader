import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from sources.youtube import _detect_audio_info, _select_mp3_bitrate, download_youtube_track
from sources.fallback import download_fallback_track
from sources.soundcloud import download_soundcloud_track
from sources import download_soundcloud_track as exported_sc_download


def test_select_mp3_bitrate_opus():
    # For Opus (standard on YouTube ~160k format 251), bitrate must be in 250-280 range -> 256k
    assert _select_mp3_bitrate("opus", 160) == "256k"
    assert _select_mp3_bitrate("opus", 128) == "256k"
    assert _select_mp3_bitrate("opus", None) == "256k"
    assert _select_mp3_bitrate("libopus", 160) == "256k"


def test_select_mp3_bitrate_aac():
    # For AAC <= 96k, use 128k
    assert _select_mp3_bitrate("aac", 96) == "128k"
    # For AAC <= 128k, use 192k (do not inflate to 320)
    assert _select_mp3_bitrate("aac", 128) == "192k"
    assert _select_mp3_bitrate("mp4a", 128) == "192k"
    # For higher AAC, use 256k
    assert _select_mp3_bitrate("aac", 160) == "256k"
    assert _select_mp3_bitrate("aac", 256) == "256k"
    # For very high bitrate AAC (>280k)
    assert _select_mp3_bitrate("aac", 320) == "320k"


def test_select_mp3_bitrate_high_bitrate_and_fallback():
    assert _select_mp3_bitrate("flac", 900) == "320k"
    assert _select_mp3_bitrate("unknown", None) == "256k"
    assert _select_mp3_bitrate("", None) == "256k"


def test_detect_audio_info_from_ytdl_info():
    info = {"acodec": "opus", "abr": 160.0}
    codec, abr = _detect_audio_info(Path("dummy.webm"), info)
    assert "opus" in codec
    assert abr == 160

    info_reqs = {
        "acodec": "none",
        "requested_downloads": [{"acodec": "opus", "abr": 128.4}]
    }
    codec, abr = _detect_audio_info(Path("dummy.webm"), info_reqs)
    assert "opus" in codec
    assert abr == 128


def test_detect_audio_info_suffix_fallback():
    codec, abr = _detect_audio_info(Path("test_track.opus"), {})
    assert codec == "opus"

    codec, abr = _detect_audio_info(Path("test_track.webm"), {})
    assert codec == "opus"

    codec, abr = _detect_audio_info(Path("test_track.m4a"), {})
    assert codec == "aac"

    codec, abr = _detect_audio_info(Path("test_track.mp3"), {})
    assert codec == "mp3"


def test_exported_soundcloud_download():
    assert exported_sc_download is not None
    assert callable(exported_sc_download)


def test_fallback_routes_soundcloud_to_soundcloud_downloader(tmp_path):
    sc_url = "https://soundcloud.com/artist-name/track-name"
    with patch("sources.fallback.download_soundcloud_track") as mock_sc:
        mock_sc.return_value = tmp_path / "mock.mp3"
        res = download_fallback_track(url=sc_url, dest_dir=tmp_path, artist="Artist", title="Track")
        assert mock_sc.called
        assert res == tmp_path / "mock.mp3"

    sc_short_url = "https://on.soundcloud.com/abc123"
    with patch("sources.fallback.download_soundcloud_track") as mock_sc:
        mock_sc.return_value = tmp_path / "mock.mp3"
        res = download_fallback_track(url=sc_short_url, dest_dir=tmp_path, artist="Artist", title="Track")
        assert mock_sc.called
        assert res == tmp_path / "mock.mp3"


def test_fallback_routes_youtube_to_youtube_downloader(tmp_path):
    yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with patch("sources.fallback.download_youtube_track") as mock_yt:
        mock_yt.return_value = tmp_path / "mock.mp3"
        res = download_fallback_track(url=yt_url, dest_dir=tmp_path, artist="Artist", title="Track")
        assert mock_yt.called
        assert res == tmp_path / "mock.mp3"


def test_youtube_downloader_routes_soundcloud_url(tmp_path):
    sc_url = "https://soundcloud.com/artist-name/track-name"
    with patch("sources.soundcloud.download_soundcloud_track") as mock_sc:
        mock_sc.return_value = tmp_path / "sc.mp3"
        res = download_youtube_track(
            artist="Artist",
            title="Track",
            dest_dir=tmp_path,
            direct_url=sc_url
        )
        assert mock_sc.called
        assert res == tmp_path / "sc.mp3"


def test_youtube_transcode_uses_adaptive_bitrate(tmp_path):
    mock_info = {
        "id": "mock_id",
        "acodec": "opus",
        "abr": 160,
    }
    dummy_source = tmp_path / "temp_mock_id_mock_id.webm"
    dummy_source.write_bytes(b"dummy_opus_audio_data" * 100)

    with patch("sources.youtube.yt_dlp.YoutubeDL") as mock_ydl_cls, \
         patch("sources.youtube._transcode_to_mp3") as mock_transcode:
        
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.prepare_filename.return_value = str(dummy_source)
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl

        mock_transcode.side_effect = lambda src, dst, bitrate="256k": dst.write_bytes(b"mp3_data" * 200) or True

        final_file = download_youtube_track(
            artist="Test Artist",
            title="Test Track",
            dest_dir=tmp_path,
            direct_url="https://www.youtube.com/watch?v=mock_id"
        )

        assert mock_transcode.called
        call_kwargs = mock_transcode.call_args
        # Verify bitrate is 256k (range 250-280 kbps) for opus stream, NOT 320k
        assert call_kwargs.kwargs.get("bitrate") == "256k"
        assert final_file is not None
        assert final_file.exists()


def test_soundcloud_download_preserves_original_without_transcoding(tmp_path):
    mock_info = {
        "id": "123456",
        "title": "Cool Artist - Cool Song",
        "uploader": "Cool Artist",
    }
    dummy_orig = tmp_path / "temp_sc_artist_track_123456.mp3"
    dummy_orig.write_bytes(b"original_128k_mp3_data" * 50)

    with patch("sources.soundcloud.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.prepare_filename.return_value = str(dummy_orig)
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl

        ydl_opts_used = {}
        def capture_opts(opts):
            nonlocal ydl_opts_used
            ydl_opts_used = opts
            mock_ydl.__enter__.return_value = mock_ydl
            return mock_ydl

        mock_ydl_cls.side_effect = capture_opts

        res = download_soundcloud_track(
            url="https://soundcloud.com/cool-artist/cool-song",
            dest_dir=tmp_path,
            artist="Cool Artist",
            title="Cool Song"
        )

        assert ydl_opts_used.get("format") == "original/bestaudio/best"
        assert res is not None
        # File preserved with original .mp3 extension and contents
        assert res.name == "Cool Artist - Cool Song.mp3"
        assert res.read_bytes() == b"original_128k_mp3_data" * 50


def test_soundcloud_download_converts_m4a_to_mp3_with_preserved_bitrate(tmp_path):
    mock_info = {
        "id": "789012",
        "title": "Cool Artist - Cool Song",
        "uploader": "Cool Artist",
        "acodec": "mp4a.40.2",
        "abr": 160,
    }
    dummy_orig = tmp_path / "temp_sc_artist_track_789012.m4a"
    dummy_orig.write_bytes(b"original_160k_aac_data" * 50)

    with patch("sources.soundcloud.yt_dlp.YoutubeDL") as mock_ydl_cls, \
         patch("sources.youtube._transcode_to_mp3") as mock_transcode:

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.prepare_filename.return_value = str(dummy_orig)
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl_cls.return_value = mock_ydl

        mock_transcode.side_effect = lambda src, dst, bitrate="256k": dst.write_bytes(b"mp3_converted" * 50) or True

        res = download_soundcloud_track(
            url="https://soundcloud.com/cool-artist/cool-song",
            dest_dir=tmp_path,
            artist="Cool Artist",
            title="Cool Song"
        )

        assert res is not None
        assert res.suffix == ".mp3"
        assert mock_transcode.called
        call_kwargs = mock_transcode.call_args.kwargs
        # For 160k AAC stream, target bitrate is 256k, NOT 320k
        assert call_kwargs.get("bitrate") == "256k"


def test_core_direct_soundcloud_download_bypasses_youtube_music(tmp_path):
    from core import download_track_by_link
    sc_url = "https://soundcloud.com/cool-artist/cool-song"
    dummy_sc_file = tmp_path / "Cool Artist - Cool Song.mp3"
    dummy_sc_file.write_bytes(b"original_mp3_data" * 50)

    mock_meta = {
        "artist": "Cool Artist",
        "title": "Cool Song",
        "soundcloud_url": sc_url,
        "album": None,
        "year": "2024",
        "duration": 180,
    }

    with patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("core.download_soundcloud_track", return_value=dummy_sc_file) as mock_sc, \
         patch("core.download_youtube_track") as mock_yt, \
         patch("core.tagger.apply_metadata") as mock_tagger:

        res = download_track_by_link(sc_url, target_quality="MP3")

        # Must call SoundCloud downloader directly
        assert mock_sc.called
        # Must NOT call YouTube downloader
        assert not mock_yt.called
        assert res == dummy_sc_file
        assert mock_tagger.called


def test_tagger_handles_nonexistent_gracefully(tmp_path):
    import tagger
    non_existent = tmp_path / "not_found.opus"
    # Should not raise exception
    tagger.apply_metadata(non_existent, "Artist", "Title")


def test_core_fallback_sc_file_used_if_flac_unavailable(tmp_path):
    from core import download_track_by_link
    sc_url = "https://soundcloud.com/artist/song"
    dummy_sc_file = tmp_path / "Artist - Song.mp3"
    dummy_sc_file.write_bytes(b"lossy_mp3" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Song",
        "soundcloud_url": sc_url,
    }

    with patch("core.metadata.get_track_metadata", return_value=mock_meta), \
         patch("core.download_soundcloud_track", return_value=dummy_sc_file), \
         patch("core.search_soulseek", return_value=[]), \
         patch("core.search_deezer_track", return_value=None), \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link(sc_url, target_quality="FLAC")
        assert res == dummy_sc_file


def test_core_mp3_priority_deezer_first(tmp_path):
    from core import download_track_by_link
    dummy_deezer_file = tmp_path / "Artist - Track.mp3"
    dummy_deezer_file.write_bytes(b"deezer_320_data" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Track",
        "deezer_id": "12345",
        "youtube_music_url": "https://music.youtube.com/watch?v=abc",
    }

    with patch("core.metadata.resolve_query_metadata", return_value=mock_meta), \
         patch("core.download_deezer_track", return_value=dummy_deezer_file) as mock_dz, \
         patch("core.search_soulseek") as mock_slsk, \
         patch("core.download_youtube_track") as mock_yt, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("Artist - Track", target_quality="MP3")

        # Deezer must be called first
        assert mock_dz.called
        # Soulseek and YouTube must NOT be called since Deezer succeeded
        assert not mock_slsk.called
        assert not mock_yt.called
        assert res == dummy_deezer_file


def test_core_mp3_priority_soulseek_second_when_deezer_fails(tmp_path):
    from core import download_track_by_link
    import config
    dummy_slsk_file = tmp_path / "Artist - Track.mp3"
    dummy_slsk_file.write_bytes(b"slsk_320_data" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Track",
        "deezer_id": "12345",
        "youtube_music_url": "https://music.youtube.com/watch?v=abc",
    }

    slsk_cand = [{
        "slskd_username": "user1",
        "slskd_filename": "track.mp3",
        "slskd_size": 10000000,
        "quality": "320 kbps",
    }]

    with patch.object(config, "SLSKD_URL", "http://localhost:5030"), \
         patch("core.metadata.resolve_query_metadata", return_value=mock_meta), \
         patch("core.download_deezer_track", return_value=None), \
         patch("core.search_soulseek", return_value=slsk_cand) as mock_slsk_search, \
         patch("core.download_soulseek_track", return_value=dummy_slsk_file) as mock_slsk_dl, \
         patch("core.download_youtube_track") as mock_yt, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("Artist - Track", target_quality="MP3")

        assert mock_slsk_search.called
        assert mock_slsk_dl.called
        assert not mock_yt.called
        assert res == dummy_slsk_file


def test_core_mp3_priority_youtube_third_when_deezer_and_soulseek_fail(tmp_path):
    from core import download_track_by_link
    import config
    dummy_yt_file = tmp_path / "Artist - Track.mp3"
    dummy_yt_file.write_bytes(b"yt_vbr_data" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Track",
        "deezer_id": "12345",
        "youtube_music_url": "https://music.youtube.com/watch?v=abc",
    }

    with patch.object(config, "SLSKD_URL", "http://localhost:5030"), \
         patch("core.metadata.resolve_query_metadata", return_value=mock_meta), \
         patch("core.download_deezer_track", return_value=None), \
         patch("core.search_soulseek", return_value=[]), \
         patch("core.download_youtube_track", return_value=dummy_yt_file) as mock_yt, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("Artist - Track", target_quality="MP3")

        assert mock_yt.called
        assert res == dummy_yt_file
