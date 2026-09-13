import pytest
from sources.youtube_matcher import (
    norm_text,
    toks,
    score_candidate,
    _duration_within_tolerance,
    _query_variants,
    _version_markers
)

def test_norm_text():
    assert norm_text("  The   Weeknd  ") == "the weeknd"
    assert norm_text("Kanye West & Kid Cudi") == "kanye west & kid cudi"

def test_toks():
    tokens = toks("Linkin Park - In The End (Official Video)")
    assert "linkin" in tokens
    assert "park" in tokens
    assert "in" in tokens
    assert "end" in tokens

def test_version_markers():
    assert "live" in _version_markers("Song Title (Live in Paris)")
    assert "acoustic" in _version_markers("Song Title (Acoustic Version)")
    assert "remix" in _version_markers("Song Title (David Guetta Remix)")
    assert len(_version_markers("Standard Song Title")) == 0

def test_duration_within_tolerance():
    # Regular track: 210s, cand: 215s (delta 5s ~ 2.3% <= 10%) -> True
    assert _duration_within_tolerance(210, 215) is True
    # Regular track: 210s, cand: 280s (delta 70s ~ 33% > 10%) -> False
    assert _duration_within_tolerance(210, 280) is False
    # Short track: 35s, cand: 42s (delta 7s <= 8s) -> True
    assert _duration_within_tolerance(35, 42) is True
    # Short track: 35s, cand: 50s (delta 15s > 8s) -> False
    assert _duration_within_tolerance(35, 50) is False

def test_score_authentic_track():
    track = {"artist": "Daft Punk", "title": "Get Lucky", "duration": 248}
    cand = {
        "title": "Get Lucky",
        "author": "Daft Punk - Topic",
        "artists": [{"name": "Daft Punk"}],
        "duration_seconds": 248
    }
    score = score_candidate(track, cand)
    assert score >= 0.85

def test_score_penalizes_cover_and_live():
    track = {"artist": "Daft Punk", "title": "Get Lucky", "duration": 248}
    
    cand_cover = {
        "title": "Get Lucky (Acoustic Cover)",
        "author": "Random Cover Channel",
        "artists": [{"name": "Random Artist"}],
        "duration_seconds": 248
    }
    score_cover = score_candidate(track, cand_cover)
    assert score_cover < 0.60

    cand_live = {
        "title": "Get Lucky (Live at Grammy Awards)",
        "author": "Live Performances",
        "artists": [{"name": "Daft Punk"}],
        "duration_seconds": 320
    }
    score_live = score_candidate(track, cand_live)
    assert score_live < 0.60

def test_query_variants():
    track = {
        "artist": "Eminem",
        "title": "Stan (feat. Dido) [Official Music Video]"
    }
    variants = _query_variants(track)
    assert any("Eminem - Stan" in v for v in variants)

def test_score_text_candidate_remix_penalty():
    from metadata import score_text_candidate
    q = "Nothing Is Safe .clipping"
    s_clean = score_text_candidate("Nothing is Safe", "clipping.", q)
    s_remix = score_text_candidate("Nothing Is Safe (Loraine James Remix)", "clipping.", q)
    s_remx = score_text_candidate("nothing is safe (remx)", "clipping.", q)
    assert s_clean > 0.8
    assert s_clean > s_remix
    assert s_clean > s_remx
    assert s_remix < 0.5
    assert s_remx < 0.5

def test_score_text_candidate_explicit_remix_request():
    from metadata import score_text_candidate
    q = "Nothing Is Safe Loraine James Remix"
    s_clean = score_text_candidate("Nothing is Safe", "clipping.", q)
    s_remix = score_text_candidate("Nothing Is Safe (Loraine James Remix)", "clipping.", q)
    assert s_remix > s_clean
    assert s_remix > 0.8
