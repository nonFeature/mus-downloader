"""Unit-тесты Zarz-источников: парсинг ответов, скоринг, контракты хэшей.

Без сети: все живые вызовы (bootstrap, поиск, скачивание) мокаются или
не вызываются вовсе.
"""
import base64
import json
import threading
from pathlib import Path

import pytest

from core.sources import amazon, beatport, matching, qobuz, tidal
from core.sources.zarz.signed_session_mobile import SignedSessionClient


# ─────────────────────────── matching ───────────────────────────


def test_isrc_exact_match_wins_over_everything():
    score = matching.score_candidate(
        artist="Someone",
        title="Totally Different",
        duration=100,
        isrc="GBDUW0000059",
        cand_artist="Kanye West",
        cand_title="Stronger",
        cand_duration=311,
        cand_isrc="gbduw-0000059",
    )
    assert score == 1000.0


def test_title_artist_duration_match_passes():
    score = matching.score_candidate(
        artist="Kanye West",
        title="Stronger",
        duration=311,
        isrc=None,
        cand_artist="Kanye West",
        cand_title="Stronger",
        cand_duration=312,
    )
    assert score >= 40.0


def test_remix_version_marker_rejected():
    score = matching.score_candidate(
        artist="Kanye West",
        title="Stronger",
        duration=311,
        isrc=None,
        cand_artist="Kanye West",
        cand_title="Stronger (Live at Glastonbury)",
        cand_duration=311,
    )
    assert score < 40.0


def test_duration_mismatch_rejected():
    score = matching.score_candidate(
        artist="Kanye West",
        title="Stronger",
        duration=311,
        isrc=None,
        cand_artist="Kanye West",
        cand_title="Stronger",
        cand_duration=90,
    )
    assert score < 40.0


def test_karaoke_junk_rejected():
    score = matching.score_candidate(
        artist="Kanye West",
        title="Stronger",
        duration=311,
        isrc=None,
        cand_artist="Karaoke Tribute Band",
        cand_title="Stronger (Karaoke Version)",
        cand_duration=311,
    )
    assert score < 0


def test_feat_stripping_matches_clean_title():
    score = matching.score_candidate(
        artist="Eminem",
        title="Love The Way You Lie",
        duration=263,
        isrc=None,
        cand_artist="Eminem",
        cand_title="Love The Way You Lie (feat. Rihanna)",
        cand_duration=263,
    )
    assert score >= 40.0


def test_pick_best_returns_highest():
    candidates = [
        {"id": "1", "artist": "Wrong", "title": "Stronger", "duration": 311, "isrc": ""},
        {"id": "2", "artist": "Kanye West", "title": "Stronger", "duration": 311, "isrc": ""},
    ]
    best = matching.pick_best(
        candidates, artist="Kanye West", title="Stronger", duration=311
    )
    assert best and best["id"] == "2"


# ─────────────────────────── tidal ───────────────────────────


def test_tidal_parse_bts_manifest():
    b64 = base64.b64encode(
        json.dumps({"urls": ["https://sp.audio.tidal.com/x.flac"], "mimeType": "audio/flac"}).encode()
    ).decode()
    m = tidal._parse_manifest(b64)
    assert m["kind"] == "direct"
    assert m["url"] == "https://sp.audio.tidal.com/x.flac"
    assert m["mime"] == "audio/flac"


def test_tidal_parse_mpd_manifest_segments():
    mpd = (
        '<?xml version="1.0"?><MPD><Period><AdaptationSet>'
        '<Representation audioSamplingRate="44100" codecs="flac">'
        '<SegmentTemplate initialization="https://sp.audio.tidal.com/init.mp4?sig=1&amp;x=2" '
        'media="https://sp.audio.tidal.com/seg$Number$.m4s?sig=1&amp;x=2"/>'
        '<SegmentList><S d="4000" r="2"/><S d="1500"/></SegmentList>'
        '</Representation></AdaptationSet></Period></MPD>'
    )
    m = tidal._parse_manifest(base64.b64encode(mpd.encode()).decode())
    assert m["kind"] == "segments"
    # r="2" → 3 сегмента + ещё один <S> → всего 4
    assert len(m["media_urls"]) == 4
    assert m["media_urls"][0] == "https://sp.audio.tidal.com/seg1.m4s?sig=1&x=2"
    assert m["media_urls"][3] == "https://sp.audio.tidal.com/seg4.m4s?sig=1&x=2"
    assert m["init_url"] == "https://sp.audio.tidal.com/init.mp4?sig=1&x=2"
    assert m["codecs"] == "flac"


def test_tidal_parse_mpd_aac_codecs():
    mpd = (
        '<MPD><Representation codecs="mp4a.40.2">'
        '<SegmentTemplate initialization="i.mp4" media="s$Number$.m4s"/>'
        '<S d="4000"/></Representation></MPD>'
    )
    m = tidal._parse_manifest(base64.b64encode(mpd.encode()).decode())
    assert m["codecs"] == "mp4a.40.2"
    assert len(m["media_urls"]) == 1


def test_tidal_parse_manifest_garbage_raises():
    b64 = base64.b64encode(b"not a manifest at all").decode()
    with pytest.raises(ValueError):
        tidal._parse_manifest(b64)


def test_tidal_quality_chain():
    assert tidal._quality_chain("HI_RES_LOSSLESS") == ["HI_RES_LOSSLESS", "LOSSLESS"]
    assert tidal._quality_chain("LOSSLESS") == ["LOSSLESS"]


def test_tidal_parse_track_id():
    assert tidal.parse_tidal_track_id("https://tidal.com/browse/track/530979474") == "530979474"
    assert tidal.parse_tidal_track_id("https://listen.tidal.com/track/12345") == "12345"
    assert tidal.parse_tidal_track_id("https://example.com/track/1") is None


# ─────────────────────────── qobuz ───────────────────────────


def test_qobuz_extract_download_info_top_level():
    url, depth, rate = qobuz._extract_download_info(
        {"download_url": "https://streaming.qobuz.com/x.flac", "bit_depth": 24, "sampling_rate": 96}
    )
    assert url == "https://streaming.qobuz.com/x.flac"
    assert depth == 24
    assert rate == 96000  # kHz → Hz


def test_qobuz_extract_download_info_nested():
    url, _, _ = qobuz._extract_download_info(
        {"data": {"url": "https://static.qobuz.com/x.flac"}}
    )
    assert url == "https://static.qobuz.com/x.flac"


def test_qobuz_extract_download_info_error():
    with pytest.raises(RuntimeError):
        qobuz._extract_download_info({"error": "Ticket resource mismatch"})
    with pytest.raises(RuntimeError):
        qobuz._extract_download_info({"success": False, "message": "nope"})


def test_qobuz_collection_items_shapes():
    assert qobuz._collection_items({"tracks": {"items": [1, 2]}}, "tracks") == [1, 2]
    assert qobuz._collection_items({"tracks": [3]}, "tracks") == [3]
    assert qobuz._collection_items({"data": {"tracks": {"items": [4]}}}, "tracks") == [4]
    assert qobuz._collection_items({}, "tracks") == []


def test_qobuz_quality_chain():
    assert qobuz._quality_chain("hi-res-max") == ["hi-res-max", "hi-res", "cd"]
    assert qobuz._quality_chain("cd") == ["cd"]


# ─────────────────────────── beatport ───────────────────────────


def test_beatport_media_url_regex():
    ok = "https://api.zarz.moe/v2/media/btp/" + "a" * 48 + "?token=abc123_XY-9.z"
    assert beatport.MEDIA_URL_RE.match(ok)
    assert not beatport.MEDIA_URL_RE.match("https://evil.example.com/media/btp/x")
    assert not beatport.MEDIA_URL_RE.match("https://api.zarz.moe/v2/media/btp/short?token=***")


def test_beatport_track_title_mix_suffix():
    assert beatport._track_title({"title": "Strobe", "mix_name": "Original Mix"}) == "Strobe (Original Mix)"
    # микс уже в тайтле — не дублируем
    assert beatport._track_title({"title": "Strobe (Original Mix)", "mix_name": "Original Mix"}) == "Strobe (Original Mix)"


def test_beatport_quality_name():
    assert beatport._quality_name("FLAC") == "lossless"
    assert beatport._quality_name("aac") == "high"
    with pytest.raises(ValueError):
        beatport._quality_name("mp3")


def test_beatport_parse_track_id():
    assert beatport.parse_beatport_track_id("https://www.beatport.com/track/strobe-original-mix/1234567") == "1234567"
    assert beatport.parse_beatport_track_id("https://beatport.com/release/x/1") is None


def test_beatport_unavailable_rejected(monkeypatch):
    payload = {
        "tracks": [
            {
                "id": "1",
                "title": "Stronger",
                "artists": [{"name": "Kanye West"}],
                "duration_ms": 311000,
                "available": False,
                "isrc": "",
            }
        ]
    }
    monkeypatch.setattr(
        beatport.zarz, "signed_json_sync", lambda *a, **k: payload
    )
    assert beatport.search_beatport_track("Kanye West", "Stronger", duration=311) is None


# ─────────────────────────── amazon ───────────────────────────


def test_amazon_normalize_asin():
    assert amazon._normalize_asin("b07xj8c8f5") == "B07XJ8C8F5"
    assert amazon._normalize_asin("B07XJ8C8F5?foo=1") == "B07XJ8C8F5"
    assert amazon._normalize_asin("not-an-asin") is None


def test_amazon_deeplink_track_info():
    info = amazon._deeplink_track_info("/tracks/B07XJ8C8F5")
    assert info == {"id": "B07XJ8C8F5", "albumId": ""}
    info = amazon._deeplink_track_info("/albums/B0AAAAAA01?trackAsin=B07XJ8C8F5")
    assert info == {"id": "B07XJ8C8F5", "albumId": "B0AAAAAA01"}
    assert amazon._deeplink_track_info("/albums/B0AAAAAA01") is None
    assert amazon._deeplink_track_info("/artists/B0AAAAAA01") is None


def test_amazon_text_value_nested():
    assert amazon._text_value("plain") == "plain"
    assert amazon._text_value({"text": "Stronger"}) == "Stronger"
    assert amazon._text_value({"defaultValue": {"text": "Nested"}}) == "Nested"
    assert amazon._text_value(None) == ""


def test_amazon_parse_search_results():
    payload = {
        "methods": [
            {
                "payload": {
                    "shoveler": {
                        "interface": "Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.VisualShovelerWidgetElement",
                        "items": [
                            {
                                "interface": "Web.TemplatesInterface.v1_0.Touch.DescriptiveRowItemElement",
                                "primaryText": {"text": "Stronger"},
                                "secondaryText1": {"text": "Kanye West"},
                                "secondaryText3": {"text": "5:11"},
                                "primaryTextLink": {"deeplink": "/tracks/B07XJ8C8F5"},
                            }
                        ],
                    }
                }
            }
        ]
    }
    results = amazon._parse_search_results(payload)
    assert len(results) == 1
    assert results[0]["id"] == "B07XJ8C8F5"
    assert results[0]["title"] == "Stronger"
    assert results[0]["artist"] == "Kanye West"
    assert results[0]["duration"] == 311


# ─────────────────────────── zarz client ───────────────────────────


def test_resource_hash_known_vector():
    # Вектор из документации signed_session_mobile (реальный сетевой захват):
    # sha256("tid:track:530979474") начинается с a5f4aee7d242692d616b4210cd61c48933b
    h = SignedSessionClient.compute_resource_hash("tid", "530979474")
    assert h.startswith("a5f4aee7d242692d616b4210cd61c48933b")


def test_beatport_hash_uses_beatport_prefix():
    h = SignedSessionClient.compute_resource_hash("beatport", "123")
    import hashlib

    assert h == hashlib.sha256(b"beatport:track:123").hexdigest()


def test_client_cache_and_app_versions(tmp_path, monkeypatch):
    from core.sources import zarz as zarz_pkg

    monkeypatch.setenv("ZARZ_DATA_DIR", str(tmp_path))
    zarz_pkg._clients.clear()
    try:
        c1 = zarz_pkg.get_client("tidal")
        c2 = zarz_pkg.get_client("tidal")
        assert c1 is c2
        assert c1.app_version == "tidal-web@1.2.5"
        assert c1.base_url == "https://api.zarz.moe/v2"
        assert c1.namespace == "zarz-v2"
        # сессионный файл создан в нашем временном каталоге
        assert (tmp_path / "signed_sessions").exists()
    finally:
        zarz_pkg._clients.clear()


# ─────────────────────────── агрегатор поиска ───────────────────────────


def test_tidal_map_items():
    items = [{
        "id": 103805726,
        "title": "Stronger",
        "artists": [{"name": "Kanye West"}],
        "duration": 311.9,
        "isrc": "USUM70741299",
        "explicit": True,
        "album": {"title": "Graduation", "releaseDate": "2007-09-11", "cover": "aa-bb-cc"},
    }]
    out = tidal._map_tidal_items(items)
    assert out[0]["id"] == "103805726"
    assert out[0]["artist"] == "Kanye West"
    assert out[0]["year"] == "2007"
    assert out[0]["explicit"] is True
    assert out[0]["album_art"] == "https://resources.tidal.com/images/aa/bb/cc/1280x1280.jpg"


def test_qobuz_map_items():
    from core.sources import qobuz

    items = [{
        "id": 58852382,
        "title": "Stronger",
        "performer": {"name": "Kanye West"},
        "duration": 311,
        "isrc": "USUM70741299",
        "parental_warning": True,
        "maximum_bit_depth": 24,
        "album": {
            "title": "Graduation",
            "release_date_original": "2007-09-11",
            "image": {"large": "https://static.qobuz.com/large.jpg", "thumbnail": "t.jpg"},
        },
    }]
    out = qobuz._map_qobuz_items(items)
    assert out[0]["id"] == "58852382"
    assert out[0]["artist"] == "Kanye West"
    assert out[0]["year"] == "2007"
    assert out[0]["explicit"] is True
    assert out[0]["hires"] is True
    assert out[0]["album_art"] == "https://static.qobuz.com/large.jpg"


def test_beatport_search_requires_live_session(monkeypatch):
    from core.sources import beatport as bp
    import config

    monkeypatch.setattr(config, "ZARZ_ENABLED", True, raising=False)
    monkeypatch.setattr(bp.zarz, "session_ready", lambda provider: False)
    assert bp.search_beatport("anything", 5) == []

    calls = []
    monkeypatch.setattr(bp.zarz, "session_ready", lambda provider: True)
    monkeypatch.setattr(
        bp.zarz, "signed_json_sync",
        lambda *a, **k: calls.append(a) or {"tracks": [
            {"id": "1", "title": "Song", "mix_name": "Original Mix",
             "artists": [{"name": "Artist"}], "duration_ms": 300000,
             "available": True, "cover": "c.jpg", "explicit": False},
        ]},
    )
    out = bp.search_beatport("song artist", 5)
    assert calls and out[0]["id"] == "1"
    assert out[0]["title"] == "Song (Original Mix)"
    assert out[0]["album_art"] == "c.jpg"


def test_zarz_worker_uses_meta_id_without_search(tmp_path):
    from core.sources import race

    searched, downloaded = [], []

    def fake_search(*a, **k):
        searched.append(a)
        return {"id": "WRONG"}

    def fake_download(track_id, work_dir, **k):
        downloaded.append(track_id)
        p = Path(work_dir) / "f.flac"
        p.write_bytes(b"fLaC")
        return p

    ctx = {
        "artist": "A", "title": "B", "duration": None, "isrc": None, "album": None,
        "meta": {"tidal_id": "12345"},
    }
    name, worker = race._mk_zarz_worker("Tidal", fake_search, fake_download, ctx, id_key="tidal_id")
    out = worker(tmp_path, threading.Event())
    assert out is not None
    assert not searched          # поиск не вызывался — есть готовый ID
    assert downloaded == ["12345"]


# ─────────────────────────── lossy-цепочка ───────────────────────────


def test_tidal_quality_chain_high():
    assert tidal._quality_chain("HIGH") == ["HIGH"]


def test_lossy_chain_order_and_kwargs():
    from core.sources import lossless as ll

    rows = [(label, kwargs) for label, _enabled, _s, _d, kwargs in ll._lossy_sources()]
    assert [r[0] for r in rows] == ["Tidal", "Amazon Music", "Beatport"]
    assert rows[0][1] == {"quality": "HIGH"}
    assert rows[1][1] == {"codec": "opus"}
    assert rows[2][1] == {"quality": "high"}


def test_run_chain_falls_through_and_passes_kwargs(tmp_path):
    from core.sources import lossless as ll

    calls = []
    fake_file = tmp_path / "x.m4a"
    fake_file.write_bytes(b"test")

    def mk_search(label, result):
        def s(artist, title, **kw):
            calls.append(("search", label))
            return result
        return s

    def mk_download(label, path):
        def d(tid, dest, **kw):
            calls.append(("download", label, kw.get("quality")))
            return path
        return d

    sources = [
        ("A", True, mk_search("A", None), mk_download("A", None), {}),
        ("B", True, mk_search("B", {"id": "1"}), mk_download("B", fake_file), {"quality": "HIGH"}),
        ("C", True, mk_search("C", {"id": "2"}), mk_download("C", None), {}),
    ]
    path, label = ll._run_chain(
        sources, artist="x", title="y", dest_dir=tmp_path,
        duration=None, isrc=None, album=None,
        status_callback=None, progress_callback=None,
        require_flac=False, source_suffix="320",
    )
    assert path == fake_file
    assert label == "B 320 (Zarz)"
    # C не трогали: B уже отдал файл
    assert calls == [("search", "A"), ("search", "B"), ("download", "B", "HIGH")]


def test_run_chain_require_flac_rejects_m4a(tmp_path):
    from core.sources import lossless as ll

    fake = tmp_path / "y.m4a"
    fake.write_bytes(b"test")
    sources = [
        ("A", True, lambda *a, **k: {"id": "1"}, lambda *a, **k: fake, {}),
    ]
    path, label = ll._run_chain(
        sources, artist="x", title="y", dest_dir=tmp_path,
        duration=None, isrc=None, album=None,
        status_callback=None, progress_callback=None,
        require_flac=True, source_suffix="FLAC",
    )
    assert path is None and label is None
    assert not fake.exists()  # не-FLAC удалён, не маскируется


def test_run_chain_skips_unknown_placeholders(tmp_path):
    from core.sources import lossless as ll

    called = []
    sources = [
        ("A", True, lambda *a, **k: called.append(1) or {"id": "1"}, lambda *a, **k: None, {}),
    ]
    path, label = ll._run_chain(
        sources, artist="Unknown Artist", title="Unknown Track", dest_dir=tmp_path,
        duration=None, isrc=None, album=None,
        status_callback=None, progress_callback=None,
        require_flac=False, source_suffix="320",
    )
    assert path is None and label is None
    assert not called  # поиск даже не запускался


def test_solver_xvfb_missing_falls_back_to_headless(monkeypatch):
    """На Linux сервере без Xvfb: _start_xvfb_if_needed не падает, а build_chromium_options добавляет --headless=new."""
    import platform
    from core.sources.zarz import solver

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(solver, "_display_is_live", lambda d: False)
    monkeypatch.setattr(solver.shutil, "which", lambda cmd: None if cmd == "Xvfb" else "/usr/bin/" + cmd)
    monkeypatch.setattr(solver, "_find_chrome", lambda: "/usr/bin/chromium")
    monkeypatch.delenv("DISPLAY", raising=False)

    # 1. Не падает с FileNotFoundError
    proc = solver._start_xvfb_if_needed()
    assert proc is None

    # 2. Браузер запускается с --headless=new вместо падения без X11
    opts, _ = solver.build_chromium_options()
    assert "--headless=new" in opts.arguments
