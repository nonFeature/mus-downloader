import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

import core
from core.metadata import sanitize_metadata_strings
from core.tagger import apply_metadata, process_cover_art_to_square, download_cover_art
from PIL import Image, ImageDraw
import io

# 1x1 transparent GIF/JPEG bytes for testing cover art
TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
)

def test_sanitize_metadata_strings():
    raw_meta = {
        "title": "I&#x27;m Not Alone &amp; You Know It",
        "artist": "Calvin Harris &amp; Friends",
        "album": "I&#x27;m Not Alone 2019",
        "album_artist": "Calvin Harris",
        "genre": "Dance &amp; Electronic",
        "year": "2019",
        "track_number": 4
    }
    cleaned = sanitize_metadata_strings(raw_meta)
    assert cleaned["title"] == "I'm Not Alone & You Know It"
    assert cleaned["artist"] == "Calvin Harris & Friends"
    assert cleaned["album"] == "I'm Not Alone 2019"
    assert cleaned["genre"] == "Dance & Electronic"
    assert cleaned["year"] == "2019"
    assert cleaned["track_number"] == 4

def test_sanitize_metadata_strings_edge_cases():
    assert sanitize_metadata_strings(None) is None
    assert sanitize_metadata_strings({}) == {}
    
    meta_with_quotes = {
        "title": "&quot;Hello World&quot;",
        "artist": "Artist &#39;Name&#39;",
    }
    cleaned = sanitize_metadata_strings(meta_with_quotes)
    assert cleaned["title"] == '"Hello World"'
    assert cleaned["artist"] == "Artist 'Name'"

def test_flac_tagging_with_cover_art(tmp_path: Path):
    flac_file = tmp_path / "test_track.flac"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "0.2",
        str(flac_file)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert flac_file.exists()

    with patch("core.tagger.download_cover_art", return_value=(TINY_JPEG, "image/jpeg")):
        apply_metadata(
            file_path=flac_file,
            artist="Calvin Harris",
            title="I'm Not Alone - 2009 Remaster",
            album="I'm Not Alone 2019",
            year="2019",
            track_number=4,
            track_total=4,
            album_art_url="https://example.com/cover.jpg",
            source="soulseek",
            source_quality="Lossless",
            genre="Dance"
        )

    # Verify mutagen FLAC tags and pictures
    audio = FLAC(str(flac_file))
    assert audio["ARTIST"] == ["Calvin Harris"]
    assert audio["TITLE"] == ["I'm Not Alone - 2009 Remaster"]
    assert audio["ALBUM"] == ["I'm Not Alone 2019"]
    assert audio["DATE"] == ["2019"]
    assert audio["TRACKNUMBER"] == ["4"]
    assert audio["TRACKTOTAL"] == ["4"]
    assert audio["SOURCE"] == ["soulseek"]
    assert audio["SOURCE_QUALITY"] == ["Lossless"]
    assert audio["GENRE"] == ["Dance"]
    
    assert len(audio.pictures) == 1
    pic = audio.pictures[0]
    assert pic.type == 3  # Front cover
    assert pic.mime == "image/jpeg"
    assert pic.data == TINY_JPEG

def test_mp3_tagging_with_cover_art(tmp_path: Path):
    mp3_file = tmp_path / "test_track.mp3"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "0.2",
        "-c:a", "libmp3lame",
        str(mp3_file)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert mp3_file.exists()

    with patch("core.tagger.download_cover_art", return_value=(TINY_JPEG, "image/jpeg")):
        apply_metadata(
            file_path=mp3_file,
            artist="Calvin Harris",
            title="I'm Not Alone",
            album="I'm Not Alone 2019",
            year="2019",
            track_number=1,
            track_total=1,
            album_art_url="https://example.com/cover.jpg",
            source="deezer",
            source_quality="320kbps CBR"
        )

    audio = MP3(str(mp3_file))
    assert audio.tags is not None
    assert audio.tags.get("TIT2").text == ["I'm Not Alone"]
    assert audio.tags.get("TPE1").text == ["Calvin Harris"]
    assert audio.tags.get("TALB").text == ["I'm Not Alone 2019"]
    apic = audio.tags.getall("APIC")
    assert len(apic) == 1
    assert apic[0].type == 3
    assert apic[0].data == TINY_JPEG


def test_process_cover_art_to_square_wide_with_black_bars():
    # Create 480x360 image (YouTube 4:3) with black bars on sides (0-59 and 421-479)
    img = Image.new("RGB", (480, 360), color="black")
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 0, 420, 360], fill="red")

    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw_bytes = buf.getvalue()

    res_bytes, mime = process_cover_art_to_square(raw_bytes)
    assert mime == "image/jpeg"

    out_img = Image.open(io.BytesIO(res_bytes))
    w, h = out_img.size
    assert w == h
    assert w >= 300


def test_process_cover_art_to_square_already_square():
    img = Image.new("RGB", (300, 300), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    res_bytes, mime = process_cover_art_to_square(raw_bytes)
    assert mime == "image/jpeg"

    out_img = Image.open(io.BytesIO(res_bytes))
    assert out_img.size == (300, 300)


def test_download_cover_art_youtube_probes_maxres():
    with patch("core.tagger.httpx.get") as mock_get:
        # Candidate URLs include maxresdefault.jpg
        def side_effect(url, **kwargs):
            m = MagicMock()
            if "maxresdefault.jpg" in url:
                m.status_code = 200
                img = Image.new("RGB", (1280, 720), color="purple")
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                m.content = buf.getvalue()
            else:
                m.status_code = 404
            return m

        mock_get.side_effect = side_effect
        raw, mime = download_cover_art("https://i.ytimg.com/vi/89F5fpvwPr0/hqdefault.jpg")

        assert raw is not None
        assert mime == "image/jpeg"
        out_img = Image.open(io.BytesIO(raw))
        assert out_img.size[0] == out_img.size[1]

