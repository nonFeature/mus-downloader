"""
Tests for quality-aware cover art resolution.

Cover policy under test:
  * lossless containers (.flac/.wav) -> 1400x1400
  * lossy containers (.mp3/.m4a/.opus) -> 768x768

The unit tests are hermetic. The live tests at the bottom actually hit the
CDNs and are opt-in via ``MUS_LIVE_TESTS=1``.
"""

import io
import os

import pytest
from PIL import Image

from core import coverart
from core.tagger import download_cover_art, process_cover_art_to_square


def make_image(width: int, height: int, fmt: str = "JPEG", color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


# --------------------------------------------------------------------------
# probe_image_size: real pixel dimensions without Pillow
# --------------------------------------------------------------------------

def test_probe_jpeg_size():
    assert coverart.probe_image_size(make_image(1200, 1200)) == (1200, 1200)


def test_probe_png_size():
    assert coverart.probe_image_size(make_image(640, 480, fmt="PNG")) == (640, 480)


def test_probe_gif_size():
    data = make_image(320, 240, fmt="GIF")
    assert coverart.probe_image_size(data) == (320, 240)


def test_probe_webp_size():
    data = make_image(256, 256, fmt="WEBP")
    assert coverart.probe_image_size(data) == (256, 256)


def test_probe_ignores_non_image():
    assert coverart.probe_image_size(b"not an image at all") is None
    assert coverart.probe_image_size(b"") is None


def test_probe_is_not_fooled_by_declared_url_size():
    """A CDN lying about its size must not fool the measurement."""
    data = make_image(500, 500)
    assert coverart.probe_image_size(data) == (500, 500)


# --------------------------------------------------------------------------
# URL rewriting: request the size we actually want
# --------------------------------------------------------------------------

def test_rewrite_deezer_url_to_target_size():
    url = "https://e-cdns-images.dzcdn.net/images/cover/2e01/56x56-000000-80-0-0.jpg"
    assert coverart.resize_dzcdn_url(url, 1400) == (
        "https://e-cdns-images.dzcdn.net/images/cover/2e01/1400x1400-000000-80-0-0.jpg"
    )
    assert coverart.resize_dzcdn_url(url, 768).endswith("/768x768-000000-80-0-0.jpg")


def test_rewrite_deezer_preserves_hash_path():
    url = "https://e-cdns-images.dzcdn.net/images/cover/abcdef0123/1000x1000-000000-80-0-0.jpg"
    out = coverart.resize_dzcdn_url(url, 768)
    assert "/images/cover/abcdef0123/" in out
    assert "1000x1000" not in out


def test_rewrite_itunes_url_to_target_size():
    url = "https://is1-ssl.mzstatic.com/image/thumb/Music122/v4/de/eb/63/abc/4050.jpg/100x100bb.jpg"
    assert coverart.resize_itunes_url(url, 768) == (
        "https://is1-ssl.mzstatic.com/image/thumb/Music122/v4/de/eb/63/abc/4050.jpg/768x768bb.jpg"
    )


def test_rewrite_coverartarchive_clamps_to_available_sizes():
    url = "https://coverartarchive.org/release/rel-123/front-500"
    assert coverart.resize_coverartarchive_url(url, 768) == (
        "https://coverartarchive.org/release/rel-123/front-1200"
    )
    assert coverart.resize_coverartarchive_url(url, 300).endswith("front-500")
    # Nothing above 1200 exists, so we take the best available.
    assert coverart.resize_coverartarchive_url(url, 1400).endswith("front-1200")


def test_resize_url_for_source_leaves_unsupported_cdns_alone():
    yt = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    sc = "https://i1.sndcdn.com/artworks-0001-abcdef-t500x500.jpg"
    assert coverart.resize_url_for_source(yt, 1400) == yt
    assert coverart.resize_url_for_source(sc, 1400) == sc


# --------------------------------------------------------------------------
# Ranking: best achievable resolution wins, not first responder
# --------------------------------------------------------------------------

def test_estimate_max_size_prefers_native_deezer_master_over_itunes():
    dz = "https://e-cdns-images.dzcdn.net/images/cover/h/1000x1000-000000-80-0-0.jpg"
    it = "https://is1-ssl.mzstatic.com/image/thumb/Music1/v4/a/b/c/art.jpg/1000x1000bb.jpg"
    assert coverart.estimate_max_size(dz) > coverart.estimate_max_size(it)


def test_estimate_max_size_ranks_small_sources_low():
    assert coverart.estimate_max_size("https://i1.sndcdn.com/art-t500x500.jpg") < 768
    assert coverart.estimate_max_size("https://i.scdn.co/image/abc") < 768
    assert coverart.estimate_max_size("") == 0


def test_fetch_best_cover_art_prefers_larger_source_over_order():
    """Last.fm answers first but iTunes/Deezer must still win on resolution."""
    from unittest.mock import patch

    from core.metadata import fetch_best_cover_art

    with (
        patch("core.metadata.fetch_coverartarchive_art", return_value=None),
        patch(
            "core.metadata.fetch_lastfm_album_art",
            return_value="https://lastfm.freetls.fastly.net/i/u/hash.png",
        ),
        patch(
            "core.metadata.fetch_itunes_album_art",
            return_value="https://is1-ssl.mzstatic.com/image/thumb/M/v4/a/art.jpg/1000x1000bb.jpg",
        ),
        patch(
            "core.metadata.fetch_deezer_album_art",
            return_value="https://e-cdns-images.dzcdn.net/images/cover/h/1000x1000-000000-80-0-0.jpg",
        ),
    ):
        art = fetch_best_cover_art(artist="A", album="B")
        assert "dzcdn.net" in art


def test_fetch_best_cover_art_rewrites_every_candidate_to_target_size():
    from unittest.mock import patch

    from core.metadata import fetch_best_cover_art

    with (
        patch("core.metadata.fetch_coverartarchive_art", return_value=None),
        patch("core.metadata.fetch_lastfm_album_art", return_value=None),
        patch(
            "core.metadata.fetch_itunes_album_art",
            return_value="https://is1-ssl.mzstatic.com/image/thumb/M/v4/a/art.jpg/1000x1000bb.jpg",
        ),
        patch(
            "core.metadata.fetch_deezer_album_art",
            return_value="https://e-cdns-images.dzcdn.net/images/cover/h/1000x1000-000000-80-0-0.jpg",
        ),
    ):
        art = fetch_best_cover_art(artist="A", album="B", target_size=768)
        assert "768x768" in art


def test_fetch_best_cover_art_returns_none_when_all_sources_empty():
    from unittest.mock import patch

    from core.metadata import fetch_best_cover_art

    with (
        patch("core.metadata.fetch_coverartarchive_art", return_value=None),
        patch("core.metadata.fetch_lastfm_album_art", return_value=None),
        patch("core.metadata.fetch_itunes_album_art", return_value=None),
        patch("core.metadata.fetch_deezer_album_art", return_value=None),
    ):
        assert fetch_best_cover_art(artist="A", album="B") is None


# --------------------------------------------------------------------------
# fit_cover_art: square + resize policy
# --------------------------------------------------------------------------

def test_fit_resizes_small_square_cover_up_to_target():
    out, mime = coverart.fit_cover_art(make_image(500, 500), 768)
    assert mime == "image/jpeg"
    assert coverart.probe_image_size(out) == (768, 768)


def test_fit_downsizes_large_cover_to_target():
    out, _ = coverart.fit_cover_art(make_image(1400, 1400), 768)
    assert coverart.probe_image_size(out) == (768, 768)


def test_fit_keeps_exact_size_untouched():
    data = make_image(768, 768)
    out, _ = coverart.fit_cover_art(data, 768)
    assert coverart.probe_image_size(out) == (768, 768)


def test_fit_does_not_blow_up_tiny_cover_to_lossless_target():
    """Upscaling 500px to 1400px would be pure ballast in a FLAC tag."""
    out, _ = coverart.fit_cover_art(make_image(500, 500), 1400)
    assert coverart.probe_image_size(out) == (500, 500)


def test_fit_crops_widescreen_to_square_then_resizes():
    out, _ = coverart.fit_cover_art(make_image(1280, 720), 768)
    assert coverart.probe_image_size(out) == (768, 768)


def test_fit_without_target_size_only_squares():
    out, _ = coverart.fit_cover_art(make_image(1280, 720), 0)
    w, h = coverart.probe_image_size(out)
    assert w == h == 720


def test_fit_handles_garbage_bytes_gracefully():
    payload = b"definitely not an image"
    out, _ = coverart.fit_cover_art(payload, 768)
    assert out == payload


def test_target_size_for_quality():
    assert coverart.target_size_for("FLAC") == 1400
    assert coverart.target_size_for("MP3") == 768
    assert coverart.target_size_for("flac") == 1400
    assert coverart.target_size_for("") == 768


# --------------------------------------------------------------------------
# tagger-level behaviour
# --------------------------------------------------------------------------

def test_process_cover_art_to_square_respects_target_size():
    out, mime = process_cover_art_to_square(make_image(1000, 1000), 768)
    assert mime == "image/jpeg"
    assert coverart.probe_image_size(out) == (768, 768)


def test_download_cover_art_prefers_higher_resolution_variant(monkeypatch):
    """When several CDN variants respond, the biggest one must win."""

    def fake_download(url, timeout=12.0):
        if "1000x1000" in url:
            return make_image(1000, 1000), "image/jpeg"
        if "1400x1400" in url:
            return make_image(1400, 1400), "image/jpeg"
        if "768x768" in url:
            return make_image(768, 768), "image/jpeg"
        return None, None

    monkeypatch.setattr(coverart, "download_image", fake_download)

    url = "https://e-cdns-images.dzcdn.net/images/cover/h/1000x1000-000000-80-0-0.jpg"
    out, _ = download_cover_art(url, 1400)
    assert coverart.probe_image_size(out) == (1400, 1400)


def test_download_cover_art_rejects_lie_and_picks_honest_candidate(monkeypatch):
    """A URL that promises 1400 but serves 500 must not win over a real 768."""

    def fake_download(url, timeout=12.0):
        if "lies" in url:
            return make_image(500, 500), "image/jpeg"
        if "honest" in url:
            return make_image(768, 768), "image/jpeg"
        return None, None

    monkeypatch.setattr(coverart, "download_image", fake_download)

    out, _ = download_cover_art("https://example.com/lies/1400x1400.jpg", 768)
    assert coverart.probe_image_size(out) == (768, 768)


def test_download_cover_art_returns_none_when_nothing_responds(monkeypatch):
    monkeypatch.setattr(coverart, "download_image", lambda url, timeout=12.0: (None, None))
    assert download_cover_art("https://example.com/art.jpg", 768) == (None, None)


def test_download_cover_art_handles_empty_url():
    assert download_cover_art("", 768) == (None, None)


# --------------------------------------------------------------------------
# Live checks against the real CDNs (opt-in)
# --------------------------------------------------------------------------

live = pytest.mark.skipif(
    not os.environ.get("MUS_LIVE_TESTS"),
    reason="set MUS_LIVE_TESTS=1 to run live CDN checks",
)


@live
@pytest.mark.allow_network
def test_live_deezer_serves_exact_requested_size():
    for size in (768, 1400):
        url = f"https://e-cdns-images.dzcdn.net/images/cover/2e018122cb56986277102d2041a592c8/{size}x{size}-000000-80-0-0.jpg"
        data, _ = coverart.download_image(url)
        assert data, f"deezer {size} returned nothing"
        assert coverart.probe_image_size(data) == (size, size)


@live
@pytest.mark.allow_network
def test_live_itunes_serves_exact_requested_size():
    base = (
        "https://is1-ssl.mzstatic.com/image/thumb/Music122/v4/de/eb/63/"
        "deeb63c1-7bc0-9153-cfa3-fd9e4929aacf/4050538826562.jpg/{}x{}bb.jpg"
    )
    for size in (768, 1400):
        data, _ = coverart.download_image(base.format(size, size))
        assert data, f"itunes {size} returned nothing"
        assert coverart.probe_image_size(data) == (size, size)


@live
@pytest.mark.allow_network
def test_live_end_to_end_download_produces_target_size():
    url = "https://e-cdns-images.dzcdn.net/images/cover/2e018122cb56986277102d2041a592c8/1000x1000-000000-80-0-0.jpg"
    for target in (768, 1400):
        out, mime = download_cover_art(url, target)
        assert out, f"nothing downloaded for target {target}"
        assert mime == "image/jpeg"
        assert coverart.probe_image_size(out) == (target, target)
