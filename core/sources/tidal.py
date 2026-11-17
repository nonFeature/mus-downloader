"""Tidal через Zarz-шлюз (api.zarz.moe).

Поиск и метаданные — публичный web-API tidal.com/v1 с открытым токеном
web-плеера (аккаунт не нужен). Скачивание lossless — через шлюз Zarz:
тикет + POST /dl/tid, в ответе base64-манифест (BTS JSON с прямой ссылкой
на FLAC либо DASH MPD для Atmos — MPD не поддерживаем, только прямой путь).

Порт логики экстеншена Mornye `tidal-web` (index.js + manifest.json).
"""
import base64
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

import config

from . import zarz
from .matching import pick_best

API_BASE = "https://tidal.com/v1"
PUBLIC_TOKEN = "49YxDN9a2aFV6RTG"


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "User-Agent": config.BROWSER_USER_AGENT,
        "x-tidal-token": PUBLIC_TOKEN,
    }


def _build_url(path: str, params: dict) -> str:
    query = {
        "countryCode": getattr(config, "TIDAL_COUNTRY_CODE", "US"),
        "locale": "en_US",
        "deviceType": "BROWSER",
    }
    query.update({k: v for k, v in params.items() if v not in (None, "")})
    return f"{API_BASE}/{path.lstrip('/')}?{urlencode(query)}"


def _raw_track_to_cand(track: dict) -> Optional[dict]:
    if not track or not track.get("id"):
        return None
    artists = [a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)]
    if not artists and track.get("artist"):
        artists = [track["artist"].get("name", "")]
    return {
        "id": str(track["id"]),
        "title": track.get("title") or "",
        "artist": ", ".join(a for a in artists if a),
        "duration": float(track.get("duration") or 0) or None,
        "isrc": track.get("isrc") or "",
        "album": (track.get("album") or {}).get("title") or "",
    }


def _tidal_cover_url(cover_id: str, size: str = "1280x1280") -> Optional[str]:
    """UUID обложки Tidal → URL на resources.tidal.com (дефисы → слеши)."""
    cover_id = (cover_id or "").strip()
    if not cover_id:
        return None
    return f"https://resources.tidal.com/images/{cover_id.replace('-', '/')}/{size}.jpg"


def _map_tidal_items(items: list) -> list[dict]:
    """Сырые items из /v1/search/tracks → кандидаты для агрегатора поиска."""
    out = []
    for t in items:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        artists = [a.get("name", "") for a in (t.get("artists") or []) if isinstance(a, dict)]
        if not artists and isinstance(t.get("artist"), dict):
            artists = [t["artist"].get("name", "")]
        album = t.get("album") or {}
        out.append({
            "id": str(t["id"]),
            "title": (t.get("title") or "").strip(),
            "artist": ", ".join(a for a in artists if a),
            "duration": float(t.get("duration") or 0) or None,
            "isrc": t.get("isrc") or "",
            "album": album.get("title") or None,
            "year": (album.get("releaseDate") or "")[:4] or None,
            "explicit": bool(t.get("explicit")),
            "album_art": _tidal_cover_url(album.get("cover") or ""),
        })
    return out


def search_tidal(query: str, limit: int = 10) -> list[dict]:
    """Публичный поиск по tidal.com/v1 (без капчи). Список кандидатов."""
    query = (query or "").strip()
    if not query:
        return []
    r = httpx.get(
        _build_url("search/tracks", {"query": query, "limit": max(1, min(limit, 50)), "offset": 0}),
        headers=_headers(),
        timeout=httpx.Timeout(8.0, connect=5.0),
    )
    if r.status_code != 200:
        return []
    return _map_tidal_items((r.json() or {}).get("items") or [])


def search_tidal_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
) -> Optional[dict]:
    """Ищет трек в публичном web-API Tidal. Возвращает кандидат pick_best или None."""
    query = f"{artist} {title}".strip()
    if not query:
        return None
    try:
        r = httpx.get(
            _build_url("search/tracks", {"query": query, "limit": 20, "offset": 0}),
            headers=_headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        if r.status_code != 200:
            print(f"[!] Tidal: поиск вернул HTTP {r.status_code}")
            return None
        items = (r.json() or {}).get("items") or []
    except Exception as e:
        print(f"[!] Tidal: ошибка поиска: {e}")
        return None

    candidates = [c for c in (_raw_track_to_cand(t) for t in items) if c]
    best = pick_best(candidates, artist=artist, title=title, duration=duration, isrc=isrc)
    if best:
        print(f"[*] Tidal: найден трек {best['id']} ({best['artist']} - {best['title']})")
    return best


def _replace_amp_entities(s: str) -> str:
    return (s or "").replace("&amp;", "&")


def _parse_manifest(manifest_b64: str) -> dict:
    """Разбирает base64-манифест из ответа /dl/tid.

    Два вида (как в официальном экстеншене):
    - BTS JSON {urls:[...], mimeType} → прямая ссылка (kind="direct");
    - DASH MPD → initialization/media-шаблоны и список сегментов
      (kind="segments"): init + media-сегменты скачиваются и склеиваются
      в fragmented MP4, затем ремуксятся в .flac.
    """
    text = base64.b64decode(manifest_b64 or "").decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        data = json.loads(text)
        urls = data.get("urls") or []
        if not urls:
            raise ValueError("BTS-манифест без urls")
        return {
            "kind": "direct",
            "url": str(urls[0]),
            "mime": str(data.get("mimeType") or "audio/flac"),
        }

    init_m = re.search(r'initialization="([^"]+)"', text, re.I)
    media_m = re.search(r'media="([^"]+)"', text, re.I)
    if not init_m or not media_m:
        raise ValueError("MPD без initialization/media-шаблонов")

    codecs_m = re.search(r'codecs="([^"]+)"', text, re.I)
    codecs = (codecs_m.group(1) if codecs_m else "").lower()

    segment_count = 0
    for m in re.finditer(r'<S\s+[^>]*d="(\d+)"(?:\s+r="(-?\d+)")?[^>]*\/?>', text, re.I):
        repeat = int(m.group(2) or 0)
        if repeat < 0:
            repeat = 0
        segment_count += repeat + 1
    if segment_count <= 0:
        raise ValueError("MPD без сегментов")

    media_template = _replace_amp_entities(media_m.group(1))
    media_urls = [media_template.replace("$Number$", str(i)) for i in range(1, segment_count + 1)]
    return {
        "kind": "segments",
        "init_url": _replace_amp_entities(init_m.group(1)),
        "media_urls": media_urls,
        "mime": "application/dash+xml",
        "codecs": codecs,
    }


def _download_segments(
    init_url: str,
    media_urls: list[str],
    dest_path: Path,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cancel_event=None,
) -> Path:
    """Склеивает init + media-сегменты DASH в один fMP4-файл (побайтово)."""
    headers = {"User-Agent": config.BROWSER_USER_AGENT}
    total = len(media_urls) + 1
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    start_time = time.time()
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
            with open(tmp_path, "wb") as f:
                for idx, url in enumerate([init_url] + media_urls):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("скачивание отменено (гонка)")
                    resp = client.get(url)
                    resp.raise_for_status()
                    f.write(resp.content)
                    if progress_callback:
                        progress_callback({
                            "stage": "download",
                            "source": "Tidal (DASH)",
                            "description": f"Tidal: сегмент {idx + 1}/{total}...",
                            "downloaded_bytes": idx + 1,
                            "total_bytes": total,
                            "speed": (idx + 1) / max(1e-6, time.time() - start_time),
                            "eta": None,
                        })
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(dest_path)
    return dest_path


def _remux_container(src: Path, dst: Path) -> bool:
    """Ремукс fMP4-контейнера без пережатия (ffmpeg -c copy).

    Для FLAC-потока даёт .flac, для AAC — .m4a; формат определяется
    расширением dst. MP4-вендор-теги не переносим — пайплайн проставит свои.
    """
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("[!] Tidal: ffmpeg не найден — не могу ремукснуть DASH-сегменты в FLAC")
        return False
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-fflags", "+bitexact",
            "-i", str(src),
            "-c", "copy",
            # Не тащим MP4-вендор-теги (major_brand и т.п.) в FLAC —
            # пайплайн всё равно проставит свои Vorbis-комментарии.
            "-map_metadata", "-1",
            str(dst),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0 or not dst.exists():
        print(f"[!] Tidal: ремукс в FLAC не удался: {proc.stderr.strip()[:200]}")
        return False
    return True


def _quality_chain(quality: str) -> list[str]:
    q = (quality or "LOSSLESS").upper()
    if q == "HI_RES_LOSSLESS":
        return ["HI_RES_LOSSLESS", "LOSSLESS"]
    return [q]


def download_tidal_track(
    track_id: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    quality: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cancel_event=None,
) -> Optional[Path]:
    """Скачивает lossless-трек Tidal через Zarz. Возвращает путь к .flac или None."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    target_quality: str = str(quality or getattr(config, "ZARZ_TIDAL_QUALITY", "") or "LOSSLESS")

    from .youtube import _clean_filename

    artist_name = _clean_filename(artist or "Unknown Artist")
    track_title = _clean_filename(title or f"Track_{track_id}")

    last_error: Optional[Exception] = None
    for q in _quality_chain(target_quality):
        try:
            payload = zarz.ticketed_download_info_sync(
                "tidal", "tid", str(track_id), "/dl/tid", {"id": str(track_id), "quality": q}
            )
            data = (payload or {}).get("data") or {}
            if str(data.get("assetPresentation") or "").upper() == "PREVIEW":
                raise RuntimeError("Zarz вернул PREVIEW-ассет")
            if not data.get("manifest"):
                raise RuntimeError("в ответе нет манифеста")
            manifest = _parse_manifest(data["manifest"])

            base_name = f"{artist_name} - {track_title}"
            if manifest["kind"] == "direct":
                mime = str(manifest.get("mime") or "")
                ext = "flac" if "flac" in mime.lower() else "m4a"
                out_path = dest_dir / f"{base_name}.{ext}"
                zarz.stream_download(
                    manifest["url"], out_path, f"Tidal ({q})",
                    progress_callback=progress_callback, cancel_event=cancel_event,
                )
                if ext == "flac" and not zarz.is_flac(out_path):
                    out_path.unlink(missing_ok=True)
                    raise RuntimeError("скачанный файл не FLAC")
            else:
                # DASH: init+сегменты → fMP4 → lossless-ремукс в .flac
                # (или в .m4a для lossy-качеств вроде HIGH/AAC).
                codecs = str(manifest.get("codecs") or "")
                want_flac = (not codecs) or ("flac" in codecs)
                container = dest_dir / f"{base_name}.dash.tmp"
                try:
                    _download_segments(
                        manifest["init_url"],
                        manifest["media_urls"],
                        container,
                        progress_callback=progress_callback,
                        cancel_event=cancel_event,
                    )
                    ext = "flac" if want_flac else "m4a"
                    out_path = dest_dir / f"{base_name}.{ext}"
                    if not _remux_container(container, out_path):
                        out_path.unlink(missing_ok=True)
                        raise RuntimeError(f"DASH-ремукс в {ext} не удался")
                    if want_flac and not zarz.is_flac(out_path):
                        out_path.unlink(missing_ok=True)
                        raise RuntimeError("ремукснутый файл не FLAC")
                finally:
                    container.unlink(missing_ok=True)

            print(f"[+] Tidal: скачан {out_path.name} ({data.get('audioQuality') or q})")
            return out_path
        except zarz.ZarzUnavailableError as e:
            print(f"[!] Tidal: {e}")
            return None
        except Exception as e:
            last_error = e
            print(f"[!] Tidal: {q} не удался: {type(e).__name__}: {e}")

    if last_error:
        print(f"[!] Tidal: все попытки качества исчерпаны для трека {track_id}")
    return None


def parse_tidal_track_id(url: str) -> Optional[str]:
    """Достаёт числовой ID трека из ссылки tidal.com/track/123456 и подобных."""
    m = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url or "", re.I)
    return m.group(1) if m else None
