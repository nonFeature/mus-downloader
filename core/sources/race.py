"""Параллельная гонка источников.

Вместо последовательного перебора все источники стартуют одновременно
(каждый — в своём потоке и своей временной подпапке), а победитель
определяется по политике качества:

- FLAC-гонка: побеждает самый «жирный» файл по фактическим битности и
  семплрейту (замер mutagen'ом после скачивания). Как только приезжает
  теоретический максимум (24bit/192kHz) — остальные отменяются.
- MP3-гонка: первый, кто отдал файл не хуже ``RACE_MIN_BITRATE`` (по
  умолчанию 320 kbps; lossless-файл от Soulseek считается безусловной
  победой). Если никто не достиг планки — лучший по битрейту из доехавших.

Проигравшие отменяются кооперативно (``cancel_event`` в загрузчиках) и
удаляются вместе с временными папками. Источники без поддержки отмены
(Soulseek, yt-dlp) просто докачиваются в фоне — их файлы удаляются.
"""
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Callable, Optional

import config

LOSSLESS_SCORE = float(10**9)
#: Теоретический максимум FLAC-цепочки: дальше ждать нечего.
MAX_FLAC_SCORE = (24, 192000)


# ─────────────────────────── замер качества ───────────────────────────


def measure_quality(path: Path, mode: str):
    """FLAC-режим: (bits_per_sample, sample_rate) или None.
    Lossy-режим: kbps (float); lossless-контейнер → LOSSLESS_SCORE."""
    suffix = path.suffix.lower()
    try:
        if mode == "flac":
            if suffix != ".flac":
                return None
            from mutagen.flac import FLAC

            info = FLAC(str(path)).info
            return (int(info.bits_per_sample), int(info.sample_rate))

        if suffix in (".flac", ".wav", ".aiff", ".ape"):
            return LOSSLESS_SCORE
        info = None
        if suffix == ".mp3":
            from mutagen.mp3 import MP3

            info = MP3(str(path)).info
        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            info = MP4(str(path)).info
        elif suffix in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus

            info = OggOpus(str(path)).info
        if info is None:
            return None
        bitrate = float(getattr(info, "bitrate", 0) or 0) / 1000.0
        if bitrate <= 0 and getattr(info, "length", 0):
            # у некоторых контейнеров bitrate пуст — считаем по размеру
            bitrate = path.stat().st_size * 8 / info.length / 1000.0
        return bitrate or None
    except Exception:
        return None


def _fmt_score(score, mode: str) -> str:
    if mode == "flac":
        bits, rate = score
        return f"{bits}bit/{rate / 1000:g}kHz"
    if score >= LOSSLESS_SCORE:
        return "LOSSLESS"
    return f"{score:.0f}kbps"


def _label(name: str, score, mode: str) -> str:
    if mode == "flac":
        bits, rate = score
        return f"{name} FLAC {bits}/{rate / 1000:g}k (Race)"
    if score >= LOSSLESS_SCORE:
        return f"{name} LOSSLESS (Race)"
    return f"{name} {score:.0f}kbps (Race)"


# ─────────────────────────── раннер гонки ───────────────────────────


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name)[:40]


def _sweep_stale_race_dirs(dest_dir: Path) -> None:
    """Убирает забытые папки прошлых гонок (недавние не трогает).

    Порог 10 минут: RACE_TIMEOUT (240с) + запас на докачивание проигравших.
    Папки остаются, если процесс умер до завершения фоновой уборки (CLI).
    """
    cutoff = time.time() - 600
    try:
        for d in dest_dir.glob(".race-*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def run_race(
    workers: list[tuple[str, Callable[[Path, threading.Event], Optional[Path]]]],
    *,
    mode: str,
    dest_dir: Path,
    status_callback: Optional[Callable[[str], None]] = None,
    min_kbps: float = 320.0,
    timeout: Optional[float] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """Запускает воркеров параллельно и выбирает победителя по политике mode.

    Воркер: ``fn(work_dir, cancel_event) -> Optional[Path]`` — качает файл
    в ``work_dir``. Возвращает (итоговый путь в dest_dir, метка источника).
    """
    if not workers:
        return None, None
    if timeout is None:
        timeout = float(getattr(config, "RACE_TIMEOUT", 240))

    def report(msg: str) -> None:
        if status_callback:
            try:
                status_callback(msg)
            except Exception:
                pass

    _sweep_stale_race_dirs(dest_dir)
    base = dest_dir / f".race-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    event = threading.Event()

    def _run_one(name: str, fn, work_dir: Path):
        try:
            path = fn(work_dir, event)
            if path and Path(path).exists():
                score = measure_quality(Path(path), mode)
                if score is not None:
                    return (name, Path(path), score)
                report(f"[!] Гонка: {name} отдал файл не того формата — отброшен")
        except Exception as e:
            report(f"[!] Гонка: {name}: {type(e).__name__}: {e}")
        return None

    report(f"[*] Гонка ({'FLAC' if mode == 'flac' else 'MP3'}): {len(workers)} источников — " + ", ".join(n for n, _ in workers))
    executor = ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="race")
    futures = {}
    for name, fn in workers:
        work_dir = base / _slug(name)
        work_dir.mkdir(parents=True, exist_ok=True)
        futures[executor.submit(_run_one, name, fn, work_dir)] = name

    best: Optional[tuple] = None
    try:
        for fut in as_completed(futures, timeout=timeout):
            res = fut.result()
            if res is None:
                continue
            name, path, score = res
            report(f"[*] Гонка: {name} готов — {_fmt_score(score, mode)}")
            if mode == "flac":
                if best is None or score > best[2]:
                    best = res
                if score >= MAX_FLAC_SCORE:
                    report("[*] Гонка: достигнут максимум 24/192k — остальные отменяются")
                    break
            else:
                if score >= LOSSLESS_SCORE or score >= min_kbps:
                    best = res
                    report(f"[*] Гонка: {name} первым взял планку {min_kbps:.0f}k+ — остальные отменяются")
                    break
                if best is None or score > best[2]:
                    best = res
    except FuturesTimeout:
        report("[!] Гонка: общий таймаут — выбираю из доехавших")
    finally:
        event.set()
        executor.shutdown(wait=False, cancel_futures=True)

    # Небольшая пауза: отменяемые загрузки должны отпустить файл победителя
    # (Windows) до переноса.
    time.sleep(0.75)

    final_path: Optional[Path] = None
    label: Optional[str] = None
    if best:
        name, path, score = best
        target = dest_dir / path.name
        moved = False
        # Windows: свежесозданный файл может быть кратковременно залочен
        # (антивирус/индексатор) — пара попыток вместо мгновенной сдачи.
        for attempt in range(3):
            try:
                if target.exists():
                    target.unlink()
                shutil.move(str(path), str(target))
                moved = True
                break
            except Exception as e:
                if attempt == 2:
                    report(f"[!] Гонка: не удалось перенести файл победителя: {e}")
                else:
                    time.sleep(0.3)
        if moved:
            final_path = target
            label = _label(name, score, mode)
            report(f"[+] Гонка: победитель — {label}")

    # Уборка временных папок. Если какие-то воркеры ещё докачиваются (отмена
    # кооперативная, а Soulseek/yt-dlp её вообще не поддерживают), удалять
    # папки сейчас — значит ломать им запись (ENOENT-шум в логах). Поэтому:
    # все воркеры завершены — убираем синхронно, иначе фоновый поток ждёт
    # их окончания и убирает потом.
    if any(not f.done() for f in futures):
        def _deferred_cleanup():
            executor.shutdown(wait=True)
            shutil.rmtree(base, ignore_errors=True)

        threading.Thread(target=_deferred_cleanup, daemon=True, name="race-cleanup").start()
    else:
        shutil.rmtree(base, ignore_errors=True)
    return final_path, label


# ─────────────────────────── сборка воркеров ───────────────────────────


def _is_placeholder_meta(artist: str, title: str) -> bool:
    a = (artist or "").strip().lower()
    t = (title or "").strip().lower()
    return a.startswith("unknown") and t.startswith("unknown")


def _mk_zarz_worker(label, search_fn, download_fn, ctx, id_key: Optional[str] = None, **dl_kwargs):
    def worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
        # Если кандидат пришёл из поиска с готовым provider-ID — качаем
        # напрямую, без повторного поиска.
        track_id = (ctx.get("meta") or {}).get(id_key) if id_key else None
        if not track_id:
            cand = search_fn(
                ctx["artist"], ctx["title"],
                duration=ctx.get("duration"), isrc=ctx.get("isrc"), album=ctx.get("album"),
            )
            track_id = cand["id"] if cand else None
        if not track_id or ev.is_set():
            return None
        return download_fn(
            track_id, work_dir,
            artist=ctx["artist"], title=ctx["title"],
            progress_callback=None, cancel_event=ev, **dl_kwargs,
        )
    return (label, worker)


def _mk_soulseek_worker(target_quality: str, ctx):
    from .soulseek import download_soulseek_track, search_soulseek

    def worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
        # Soulseek не поддерживает отмену — просто докачает в фоне.
        candidates = search_soulseek(
            ctx["artist"], ctx["title"], limit=3,
            target_quality=target_quality, duration=ctx.get("duration"),
        )
        for cand in candidates:
            if ev.is_set():
                return None
            path = download_soulseek_track(
                cand["slskd_username"], cand["slskd_filename"], cand["slskd_size"],
                work_dir, target_quality=target_quality, progress_callback=None,
            )
            if path:
                return path
        return None
    return ("Soulseek", worker)


def _mk_deezer_worker(target_quality: str, ctx):
    from .deezer import download_deezer_track

    def worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
        return download_deezer_track(
            ctx["deezer_id"], work_dir, target_quality=target_quality,
            artist=ctx["artist"], title=ctx["title"],
            progress_callback=None, cancel_event=ev,
        )
    return ("Deezer", worker)


def build_flac_workers(ctx: dict) -> list:
    workers = []
    if config.SLSK_USER or config.SLSKD_URL:
        workers.append(_mk_soulseek_worker("FLAC", ctx))
    if ctx.get("deezer_id"):
        workers.append(_mk_deezer_worker("FLAC", ctx))
    if getattr(config, "ZARZ_ENABLED", True) and not _is_placeholder_meta(ctx["artist"], ctx["title"]):
        from .amazon import download_amazon_track, search_amazon_track
        from .beatport import download_beatport_track, search_beatport_track
        from .qobuz import download_qobuz_track, search_qobuz_track
        from .tidal import download_tidal_track, search_tidal_track

        if getattr(config, "ZARZ_TIDAL", True):
            workers.append(_mk_zarz_worker("Tidal", search_tidal_track, download_tidal_track, ctx, id_key="tidal_id"))
        if getattr(config, "ZARZ_AMAZON", True):
            workers.append(_mk_zarz_worker("Amazon Music", search_amazon_track, download_amazon_track, ctx, id_key="amazon_asin", codec="flac"))
        if getattr(config, "ZARZ_QOBUZ", True):
            workers.append(_mk_zarz_worker("Qobuz", search_qobuz_track, download_qobuz_track, ctx, id_key="qobuz_id"))
        if getattr(config, "ZARZ_BEATPORT", True):
            workers.append(_mk_zarz_worker("Beatport", search_beatport_track, download_beatport_track, ctx, id_key="beatport_id"))
    return workers


def build_lossy_workers(ctx: dict) -> list:
    workers = []
    if ctx.get("deezer_id"):
        workers.append(_mk_deezer_worker("MP3_320", ctx))
    if config.SLSK_USER or config.SLSKD_URL:
        workers.append(_mk_soulseek_worker("MP3", ctx))

    artist, title = ctx["artist"], ctx["title"]
    placeholder = _is_placeholder_meta(artist, title)
    if getattr(config, "ZARZ_ENABLED", True) and not placeholder:
        from .amazon import download_amazon_track, search_amazon_track
        from .beatport import download_beatport_track, search_beatport_track
        from .tidal import download_tidal_track, search_tidal_track

        if getattr(config, "ZARZ_TIDAL", True):
            workers.append(_mk_zarz_worker("Tidal", search_tidal_track, download_tidal_track, ctx, id_key="tidal_id", quality="HIGH"))
        if getattr(config, "ZARZ_AMAZON", True):
            workers.append(_mk_zarz_worker("Amazon Music", search_amazon_track, download_amazon_track, ctx, id_key="amazon_asin", codec="opus"))
        if getattr(config, "ZARZ_BEATPORT", True):
            workers.append(_mk_zarz_worker("Beatport", search_beatport_track, download_beatport_track, ctx, id_key="beatport_id", quality="high"))

    # Прямые ссылки — тоже участники гонки (если они вообще есть).
    direct_audio_url = ctx.get("direct_audio_url") or ""
    is_youtube = bool(ctx.get("is_youtube"))
    is_soundcloud = bool(ctx.get("is_soundcloud"))

    if direct_audio_url and not is_youtube and "youtube" not in direct_audio_url \
            and not is_soundcloud and "soundcloud" not in direct_audio_url:
        label = "VK Музыка" if ctx.get("meta", {}).get("vk_url") else "Direct Stream"

        def vk_worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
            import httpx

            from .youtube import _clean_filename
            out = work_dir / f"{_clean_filename(artist)} - {_clean_filename(title)}.mp3"
            with httpx.stream("GET", direct_audio_url, timeout=30.0, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    return None
                with open(out, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if ev.is_set():
                            raise RuntimeError("скачивание отменено (гонка)")
                        f.write(chunk)
            return out

        workers.append((label, vk_worker))

    if is_youtube or ("youtube" in direct_audio_url or "youtu.be" in direct_audio_url):
        from .youtube import download_youtube_track

        yt_target = direct_audio_url if ("youtube" in direct_audio_url or "youtu.be" in direct_audio_url) else ctx.get("url_or_query")

        def yt_worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
            # yt-dlp отмену не поддерживает — докачает в фоне, если проиграет.
            return download_youtube_track(
                artist=artist, title=title, dest_dir=work_dir,
                duration=ctx.get("duration"), album=ctx.get("album"),
                isrc=ctx.get("isrc"), direct_url=yt_target,
                explicit=ctx.get("explicit"), progress_callback=None,
            )

        workers.append(("YouTube (Direct)", yt_worker))

    if is_soundcloud:
        from .soundcloud import download_soundcloud_track

        sc_target = str(ctx.get("meta", {}).get("soundcloud_url") or ctx.get("url_or_query") or "")

        def sc_worker(work_dir: Path, ev: threading.Event) -> Optional[Path]:
            return download_soundcloud_track(
                url=sc_target, dest_dir=work_dir, artist=artist, title=title,
                duration=ctx.get("duration"), album=ctx.get("album"),
                progress_callback=None,
            )

        workers.append(("SoundCloud (Original)", sc_worker))

    return workers


def run_flac_race(
    *,
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
    deezer_id: Optional[str] = None,
    meta: Optional[dict] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[Path], Optional[str]]:
    ctx = {
        "artist": artist, "title": title, "duration": duration,
        "isrc": isrc, "album": album, "deezer_id": deezer_id,
        "meta": meta or {},
    }
    workers = build_flac_workers(ctx)
    if not workers:
        return None, None
    return run_race(workers, mode="flac", dest_dir=dest_dir, status_callback=status_callback)


def run_lossy_race(
    *,
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
    explicit: Optional[bool] = None,
    deezer_id: Optional[str] = None,
    meta: Optional[dict] = None,
    url_or_query: str = "",
    is_youtube: bool = False,
    is_soundcloud: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[Path], Optional[str]]:
    ctx = {
        "artist": artist, "title": title, "duration": duration,
        "isrc": isrc, "album": album, "explicit": explicit,
        "deezer_id": deezer_id, "meta": meta or {},
        "url_or_query": url_or_query,
        "is_youtube": is_youtube, "is_soundcloud": is_soundcloud,
        "direct_audio_url": (meta or {}).get("direct_url") or "",
    }
    workers = build_lossy_workers(ctx)
    if not workers:
        return None, None
    return run_race(
        workers, mode="lossy", dest_dir=dest_dir,
        status_callback=status_callback,
        min_kbps=float(getattr(config, "RACE_MIN_BITRATE", 320)),
    )
