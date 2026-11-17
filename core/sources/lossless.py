"""Цепочки источников через Zarz-шлюз.

Lossless (FLAC-ветка): Tidal → Amazon → Qobuz → Beatport.
Lossy (MP3-ветка, после Soulseek): Tidal HIGH (AAC 320) → Amazon Opus 320 →
Beatport high (AAC 256). Qobuz в lossy-цепочке не участвует: он отдаёт
только FLAC, а пережимать lossless ради MP3-ветки расточительно.

Порядок выбран по цене проверки: у Tidal поиск публичный (без капчи),
у Amazon — анонимный mesk-API (без капчи); Qobuz и Beatport ищут уже через
подписанную сессию Zarz, т.е. первый запуск может потребовать решения
Turnstile в браузере. Скачивание у всех четырёх — через шлюз.
"""
from pathlib import Path
from typing import Callable, Optional

import config

from .amazon import download_amazon_track, search_amazon_track
from .beatport import download_beatport_track, search_beatport_track
from .qobuz import download_qobuz_track, search_qobuz_track
from .tidal import download_tidal_track, search_tidal_track


def _lossless_sources() -> list[tuple[str, bool, Callable, Callable, dict]]:
    return [
        ("Tidal", bool(getattr(config, "ZARZ_TIDAL", True)), search_tidal_track, download_tidal_track, {}),
        ("Amazon Music", bool(getattr(config, "ZARZ_AMAZON", True)), search_amazon_track, download_amazon_track, {"codec": "flac"}),
        ("Qobuz", bool(getattr(config, "ZARZ_QOBUZ", True)), search_qobuz_track, download_qobuz_track, {}),
        ("Beatport", bool(getattr(config, "ZARZ_BEATPORT", True)), search_beatport_track, download_beatport_track, {}),
    ]


def _lossy_sources() -> list[tuple[str, bool, Callable, Callable, dict]]:
    return [
        ("Tidal", bool(getattr(config, "ZARZ_TIDAL", True)), search_tidal_track, download_tidal_track, {"quality": "HIGH"}),
        ("Amazon Music", bool(getattr(config, "ZARZ_AMAZON", True)), search_amazon_track, download_amazon_track, {"codec": "opus"}),
        ("Beatport", bool(getattr(config, "ZARZ_BEATPORT", True)), search_beatport_track, download_beatport_track, {"quality": "high"}),
    ]


def _run_chain(
    sources: list[tuple[str, bool, Callable, Callable, dict]],
    *,
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float],
    isrc: Optional[str],
    album: Optional[str],
    status_callback: Optional[Callable[[str], None]],
    progress_callback: Optional[Callable[[dict], None]],
    require_flac: bool,
    source_suffix: str,
) -> tuple[Optional[Path], Optional[str]]:
    """Общий раннер цепочки: ошибка провайдера не останавливает остальных."""

    def report(msg: str) -> None:
        if status_callback:
            try:
                status_callback(msg)
            except Exception:
                pass

    # Плейсхолдеры пайплайна («Unknown Artist»/«Unknown Track») означают, что
    # метаданные не распознаны — искать по ним нельзя: скоринг сматчит любой
    # мусорный трек с подходящими токенами.
    if (artist or "").strip().lower().startswith("unknown") and (title or "").strip().lower().startswith("unknown"):
        report("[!] Zarz: метаданные не определены (Unknown/Unknown) — цепочка пропущена")
        return None, None

    for label, enabled, search_fn, download_fn, dl_kwargs in sources:
        if not enabled:
            continue
        report(f"[*] {label}: поиск...")
        try:
            cand = search_fn(artist, title, duration=duration, isrc=isrc, album=album)
        except Exception as e:
            report(f"[!] {label}: поиск не удался: {e}")
            continue
        if not cand:
            continue
        try:
            file_path = download_fn(
                cand["id"],
                dest_dir,
                artist=artist,
                title=title,
                progress_callback=progress_callback,
                **dl_kwargs,
            )
        except Exception as e:
            report(f"[!] {label}: скачивание не удалось: {e}")
            continue
        if file_path:
            if require_flac and file_path.suffix.lower() != ".flac":
                # Не-FLAC от lossless-запроса — не маскируем, удаляем.
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            return file_path, f"{label} {source_suffix} (Zarz)"
    return None, None


def download_lossless_zarz_track(
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """FLAC-цепочка Zarz. Возвращает (путь к .flac, имя источника) или (None, None)."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None, None
    return _run_chain(
        _lossless_sources(),
        artist=artist,
        title=title,
        dest_dir=dest_dir,
        duration=duration,
        isrc=isrc,
        album=album,
        status_callback=status_callback,
        progress_callback=progress_callback,
        require_flac=True,
        source_suffix="FLAC",
    )


def download_lossy_zarz_track(
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """Lossy-цепочка Zarz (AAC 320 / Opus 320 / AAC 256) для MP3-ветки.

    Возвращает (путь к файлу, имя источника) или (None, None). Файл —
    оригинальный контейнер (.m4a/.opus); пайплайн нормализует его в .mp3
    без раздувания битрейта (см. core._normalize_to_mp3).
    """
    if not getattr(config, "ZARZ_ENABLED", True):
        return None, None
    return _run_chain(
        _lossy_sources(),
        artist=artist,
        title=title,
        dest_dir=dest_dir,
        duration=duration,
        isrc=isrc,
        album=album,
        status_callback=status_callback,
        progress_callback=progress_callback,
        require_flac=False,
        source_suffix="320",
    )
