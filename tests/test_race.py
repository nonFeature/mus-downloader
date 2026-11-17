"""Unit-тесты гонки источников (core/sources/race.py) — без сети."""
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from core.sources import race


@pytest.fixture
def fake_measure(monkeypatch):
    """measure_quality смотрит на имя файла: '16-44100.flac' → (16, 44100), '320.m4a' → 320.0."""

    def _measure(path: Path, mode: str):
        stem = path.stem  # напр. "24-96000" или "320"
        if mode == "flac":
            if path.suffix != ".flac":
                return None
            bits, _, rate = stem.partition("-")
            return (int(bits), int(rate))
        if stem == "lossless":
            return race.LOSSLESS_SCORE
        return float(stem)

    monkeypatch.setattr(race, "measure_quality", _measure)


class FakeWorker:
    """Воркер-заглушка: ждёт delay (с поддержкой отмены), пишет файл с именем-скором."""

    def __init__(self, name: str, score_stem: str, delay: float = 0.0, ext: str = ".flac"):
        self.name = name
        self.score_stem = score_stem
        self.delay = delay
        self.ext = ext
        self.event: Optional[threading.Event] = None

    def __call__(self, work_dir: Path, ev: threading.Event) -> Optional[Path]:
        self.event = ev
        deadline = time.monotonic() + self.delay
        while time.monotonic() < deadline:
            if ev.is_set():
                return None
            time.sleep(0.02)
        if ev.is_set():
            return None
        p = work_dir / f"{self.score_stem}{self.ext}"
        p.write_bytes(b"data")
        return p

    def as_tuple(self):
        return (self.name, self)


def _wait_no_race_dirs(tmp_path: Path, timeout: float = 5.0) -> bool:
    """Уборка temp-папок гонки может быть отложенной — подождать её."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not list(tmp_path.glob(".race-*")):
            return True
        time.sleep(0.1)
    return not list(tmp_path.glob(".race-*"))


def test_flac_race_richest_wins(tmp_path, fake_measure):
    workers = [
        FakeWorker("Fast", "16-44100", delay=0.0).as_tuple(),
        FakeWorker("Fat", "24-96000", delay=0.3).as_tuple(),
    ]
    path, label = race.run_race(workers, mode="flac", dest_dir=tmp_path)
    assert path is not None and path.exists()
    assert path.name == "24-96000.flac"
    assert label is not None
    assert "Fat" in label and "24/96k" in label
    # временные папки гонки убраны
    assert _wait_no_race_dirs(tmp_path)


def test_flac_race_early_stop_on_max(tmp_path, fake_measure):
    slow = FakeWorker("Slow", "24-192000", delay=30.0)
    workers = [
        FakeWorker("Max", "24-192000", delay=0.0).as_tuple(),
        slow.as_tuple(),
    ]
    t0 = time.time()
    path, label = race.run_race(workers, mode="flac", dest_dir=tmp_path)
    elapsed = time.time() - t0
    assert path is not None
    # Медленный воркер получил cancel и не досиживал свои 30 секунд
    assert elapsed < 15.0
    assert slow.event is not None and slow.event.is_set()


def test_lossy_race_first_above_floor_wins(tmp_path, fake_measure):
    workers = [
        FakeWorker("LowFast", "256", delay=0.0, ext=".m4a").as_tuple(),
        FakeWorker("HighSlow", "320", delay=0.4, ext=".m4a").as_tuple(),
    ]
    path, label = race.run_race(workers, mode="lossy", dest_dir=tmp_path, min_kbps=320)
    assert path is not None
    assert path.name == "320.m4a"
    assert label is not None
    assert "HighSlow" in label and "320kbps" in label


def test_lossy_race_best_of_all_when_below_floor(tmp_path, fake_measure):
    workers = [
        FakeWorker("A", "160", delay=0.0, ext=".m4a").as_tuple(),
        FakeWorker("B", "256", delay=0.2, ext=".m4a").as_tuple(),
    ]
    path, label = race.run_race(workers, mode="lossy", dest_dir=tmp_path, min_kbps=320)
    assert path is not None
    assert path.name == "256.m4a"
    assert label is not None and "B 256kbps" in label


def test_lossy_race_lossless_from_soulseek_wins_instantly(tmp_path, fake_measure):
    workers = [
        FakeWorker("Soulseek", "lossless", delay=0.1, ext=".flac").as_tuple(),
        FakeWorker("Tidal", "320", delay=0.5, ext=".m4a").as_tuple(),
    ]
    path, label = race.run_race(workers, mode="lossy", dest_dir=tmp_path, min_kbps=320)
    assert path is not None
    assert path.name == "lossless.flac"
    assert label is not None and "LOSSLESS" in label


def test_race_all_failed(tmp_path, fake_measure):
    def broken(work_dir, ev):
        raise RuntimeError("boom")

    def empty(work_dir, ev):
        return None

    path, label = race.run_race([("Broken", broken), ("Empty", empty)], mode="flac", dest_dir=tmp_path)
    assert path is None and label is None
    assert _wait_no_race_dirs(tmp_path)


def test_race_wrong_format_discarded(tmp_path, fake_measure):
    # FLAC-гонка: воркер отдал .m4a — measure вернёт None, результат отбрасывается
    workers = [FakeWorker("Bad", "320", delay=0.0, ext=".m4a").as_tuple()]
    path, label = race.run_race(workers, mode="flac", dest_dir=tmp_path)
    assert path is None and label is None


def test_build_workers_skip_zarz_on_placeholder(monkeypatch):
    import config

    monkeypatch.setattr(config, "SLSK_USER", "", raising=False)
    monkeypatch.setattr(config, "SLSKD_URL", "", raising=False)
    monkeypatch.setattr(config, "ZARZ_ENABLED", True, raising=False)
    ctx = {"artist": "Unknown Artist", "title": "Unknown Track", "deezer_id": "123"}
    names = [n for n, _ in race.build_flac_workers(ctx)]
    assert names == ["Deezer"]  # zarz-воркеры исключены

    ctx2 = {"artist": "Kanye West", "title": "Stronger", "deezer_id": None}
    names2 = [n for n, _ in race.build_flac_workers(ctx2)]
    assert "Tidal" in names2 and "Qobuz" in names2 and "Amazon Music" in names2 and "Beatport" in names2
    assert "Deezer" not in names2 and "Soulseek" not in names2


def test_build_lossy_workers_direct_links(monkeypatch):
    import config

    monkeypatch.setattr(config, "SLSK_USER", "", raising=False)
    monkeypatch.setattr(config, "SLSKD_URL", "", raising=False)
    monkeypatch.setattr(config, "ZARZ_ENABLED", True, raising=False)
    ctx = {
        "artist": "Some", "title": "Thing",
        "meta": {"vk_url": "https://vk.com/audio", "direct_url": "https://vk-cdn.net/audio.mp3"},
        "direct_audio_url": "https://vk-cdn.net/audio.mp3",
        "is_youtube": False, "is_soundcloud": False,
        "url_or_query": "",
    }
    names = [n for n, _ in race.build_lossy_workers(ctx)]
    assert "VK Музыка" in names
    assert "YouTube (Direct)" not in names and "SoundCloud (Original)" not in names

    ctx_yt = dict(ctx, is_youtube=True, direct_audio_url="", meta={"direct_url": ""})
    names_yt = [n for n, _ in race.build_lossy_workers(ctx_yt)]
    assert "YouTube (Direct)" in names_yt


# ─────────────────── роутинг гонки в пайплайне (PARALLEL_SOURCES=1) ───────────────────


def _pipeline_env(monkeypatch, tmp_path):
    """Общая обвязка: сеть/тегирование/кэши заглушены, метаданные переданы заранее."""
    import config as cfg
    import core

    monkeypatch.setattr(cfg, "PARALLEL_SOURCES", True, raising=False)
    monkeypatch.setattr(cfg, "METADATA_EXTRAS", False, raising=False)
    monkeypatch.setattr(core, "search_deezer_track", lambda *a, **k: None)
    monkeypatch.setattr(core.metadata, "resolve_deezer_by_isrc", lambda *a, **k: None)
    monkeypatch.setattr(core.tagger, "apply_metadata", lambda *a, **k: None)
    monkeypatch.setattr(core.tagger, "apply_extended_metadata", lambda *a, **k: None)
    return cfg, core


def test_pipeline_flac_uses_race(monkeypatch, tmp_path):
    cfg, core = _pipeline_env(monkeypatch, tmp_path)
    calls = {}

    def fake_flac_race(**kw):
        calls["kw"] = kw
        p = tmp_path / "winner.flac"
        p.write_bytes(b"fLaC" + b"\x00" * 64)
        return p, "Qobuz FLAC 24/96k (Race)"

    monkeypatch.setattr(core, "run_flac_race", fake_flac_race)
    meta = {"artist": "A", "title": "B", "duration": 100.0, "isrc": "US123456789", "album": "C"}
    path = core.download_track_by_link(
        "", target_quality="FLAC", dest_dir=tmp_path, track_meta=meta, reuse_cached_file=False
    )
    assert path is not None and path.suffix == ".flac"
    assert calls["kw"]["artist"] == "A" and calls["kw"]["isrc"] == "US123456789"


def test_pipeline_mp3_uses_race(monkeypatch, tmp_path):
    cfg, core = _pipeline_env(monkeypatch, tmp_path)
    calls = {}

    def fake_lossy_race(**kw):
        calls["kw"] = kw
        p = tmp_path / "winner.m4a"
        p.write_bytes(b"\x00" * 64)
        return p, "Tidal 320kbps (Race)"

    monkeypatch.setattr(core, "run_lossy_race", fake_lossy_race)
    meta = {"artist": "A", "title": "B", "duration": 100.0, "isrc": None, "album": "C"}
    path = core.download_track_by_link(
        "", target_quality="MP3", dest_dir=tmp_path, track_meta=meta, reuse_cached_file=False
    )
    assert path is not None and path.suffix == ".m4a"
    assert calls["kw"]["artist"] == "A"


def test_pipeline_race_loss_falls_back_to_youtube(monkeypatch, tmp_path):
    """Гонка ничего не дала → хвост MP3-ветки (YouTube Music) продолжает работать."""
    cfg, core = _pipeline_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "run_lossy_race", lambda **kw: (None, None))
    used = {}

    def fake_yt(*a, **kw):
        used["yt"] = True
        p = tmp_path / "yt.mp3"
        p.write_bytes(b"ID3" + b"\x00" * 64)
        return p

    monkeypatch.setattr(core, "download_youtube_track", fake_yt)
    meta = {"artist": "A", "title": "B", "duration": 100.0, "isrc": None, "album": None}
    path = core.download_track_by_link(
        "", target_quality="MP3", dest_dir=tmp_path, track_meta=meta, reuse_cached_file=False
    )
    assert path is not None and used.get("yt")


# ─────────────────── нормализация контейнера в MP3 ───────────────────


def _ffmpeg_or_skip():
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    return ffmpeg


def _make_tone(ffmpeg, dst, codec_args):
    import subprocess

    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", *codec_args, str(dst)],
        check=True, capture_output=True,
    )


def test_normalize_m4a_to_mp3_no_inflation(tmp_path):
    import core

    ffmpeg = _ffmpeg_or_skip()
    m4a = tmp_path / "tone.m4a"
    _make_tone(ffmpeg, m4a, ["-c:a", "aac", "-b:a", "256k"])

    out = core._normalize_to_mp3(m4a, lambda m: None)
    assert out.suffix == ".mp3" and out.exists()
    assert not m4a.exists()  # оригинал удалён после конвертации
    from mutagen.mp3 import MP3

    bitrate = getattr(MP3(str(out)).info, "bitrate", 0)
    assert 240_000 <= bitrate <= 270_000  # 256k без раздувания до 320


def test_normalize_flac_to_mp3_320(tmp_path):
    import core

    ffmpeg = _ffmpeg_or_skip()
    flac = tmp_path / "tone.flac"
    _make_tone(ffmpeg, flac, ["-c:a", "flac"])

    out = core._normalize_to_mp3(flac, lambda m: None)
    assert out.suffix == ".mp3" and out.exists()
    from mutagen.mp3 import MP3

    bitrate = getattr(MP3(str(out)).info, "bitrate", 0)
    assert 310_000 <= bitrate <= 330_000  # lossless → честный 320 CBR


def test_normalize_keeps_original_on_garbage(tmp_path):
    import core

    _ffmpeg_or_skip()
    bad = tmp_path / "garbage.m4a"
    bad.write_bytes(b"\x00" * 64)
    out = core._normalize_to_mp3(bad, lambda m: None)
    # ffmpeg не смог — остался оригинал, а не пустота
    assert out == bad and bad.exists()
