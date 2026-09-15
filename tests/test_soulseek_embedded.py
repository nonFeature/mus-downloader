import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import config
from sources.soulseek import parse_slskd_quality, search_soulseek, download_soulseek_track, EmbeddedSoulseek
from core import download_track_by_link


def test_parse_slskd_quality_snake_case_flac():
    # Test snake_case (aioslsk) format
    file_info = {
        "bit_depth": 24,
        "sample_rate": 96000,
        "bitrate": 3000,
        "duration": 210,
        "size": 50000000,
        "is_vbr": False,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.flac",
        file_info=file_info,
        target_quality="FLAC",
        expected_duration=210
    )
    assert "FLAC Hi-Res 24bit/96kHz" in label
    assert score > 2000


def test_parse_slskd_quality_snake_case_mp3():
    file_info = {
        "bitrate": 320,
        "duration": 180,
        "size": 7500000,
        "is_vbr": False,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="MP3",
        expected_duration=180
    )
    assert "MP3 320 kbps CBR" in label
    assert score >= 450


def test_parse_slskd_quality_rejects_lossy_in_flac_mode():
    file_info = {
        "bitrate": 320,
        "duration": 180,
        "size": 7500000,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="FLAC",
        expected_duration=180
    )
    assert label == "Lossy"
    assert score == 0.0


def test_parse_slskd_quality_duration_mismatch_penalty():
    file_info = {
        "bitrate": 320,
        "duration": 300,  # 100s difference
        "size": 7500000,
    }
    label, score = parse_slskd_quality(
        filename="Artist - Song.mp3",
        file_info=file_info,
        target_quality="MP3",
        expected_duration=200
    )
    assert label == "Invalid Duration"
    assert score < 0


def test_search_soulseek_uses_embedded_instance():
    mock_es = MagicMock()
    mock_es.search.return_value = [
        {
            "username": "peer1",
            "filename": "Artist - Song.flac",
            "size": 30000000,
            "bit_depth": 16,
            "sample_rate": 44100,
            "bitrate": 900,
            "duration": 200,
            "has_free_slot": True,
            "upload_speed": 1000000,
            "queue_length": 0,
        }
    ]

    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es):
        results = search_soulseek(
            artist="Artist",
            title="Song",
            limit=5,
            target_quality="FLAC",
            duration=200
        )

        assert mock_es.search.called
        assert len(results) == 1
        assert results[0]["slskd_username"] == "peer1"
        assert results[0]["slskd_filename"] == "Artist - Song.flac"
        assert "FLAC 16bit/44kHz" in results[0]["quality"]


def test_download_soulseek_track_embedded_copies_file(tmp_path):
    mock_es = MagicMock()
    source_flac = tmp_path / "cache" / "Artist - Song.flac"
    source_flac.parent.mkdir(parents=True, exist_ok=True)
    source_flac.write_bytes(b"FLACDATA" * 50)

    mock_es.download.return_value = source_flac

    dest_dir = tmp_path / "downloads"
    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es):
        result = download_soulseek_track(
            username="peer1",
            filename="folder\\Artist - Song.flac",
            size=1000,
            dest_dir=dest_dir,
            target_quality="FLAC"
        )

        assert mock_es.download.called
        assert result == dest_dir / "Artist - Song.flac"
        assert result.exists()
        assert result.read_bytes() == b"FLACDATA" * 50


def test_download_soulseek_track_transcodes_flac_in_mp3_mode(tmp_path):
    mock_es = MagicMock()
    source_flac = tmp_path / "cache" / "Artist - Song.flac"
    source_flac.parent.mkdir(parents=True, exist_ok=True)
    source_flac.write_bytes(b"FLACDATA" * 50)

    mock_es.download.return_value = source_flac
    dest_dir = tmp_path / "downloads"

    def fake_ffmpeg(cmd, **kwargs):
        mp3_target = Path(cmd[-1])
        mp3_target.write_bytes(b"MP3DATA" * 2000)
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    with patch.object(EmbeddedSoulseek, "get_instance", return_value=mock_es), \
         patch("subprocess.run", side_effect=fake_ffmpeg):
        result = download_soulseek_track(
            username="peer1",
            filename="Artist - Song.flac",
            size=1000,
            dest_dir=dest_dir,
            target_quality="MP3"
        )

        assert result == dest_dir / "Artist - Song.mp3"
        assert result.exists()


def test_core_flac_uses_slsk_user_without_slskd_url(tmp_path):
    dummy_flac = tmp_path / "Artist - Track.flac"
    dummy_flac.write_bytes(b"flac_bytes" * 20)

    mock_meta = {
        "artist": "Artist",
        "title": "Track",
        "duration": 200,
    }

    slsk_cand = [{
        "slskd_username": "peer1",
        "slskd_filename": "Artist - Track.flac",
        "slskd_size": 20000000,
        "quality": "FLAC 16bit/44kHz",
    }]

    # SLSKD_URL is empty, but SLSK_USER is set
    with patch.object(config, "SLSKD_URL", ""), \
         patch.object(config, "SLSK_USER", "test_user"), \
         patch("core.metadata.resolve_query_metadata", return_value=mock_meta), \
         patch("core.search_soulseek", return_value=slsk_cand) as mock_search, \
         patch("core.download_soulseek_track", return_value=dummy_flac) as mock_dl, \
         patch("core.tagger.apply_metadata"):

        res = download_track_by_link("Artist - Track", target_quality="FLAC")
        assert mock_search.called
        assert mock_dl.called
        assert res == dummy_flac
