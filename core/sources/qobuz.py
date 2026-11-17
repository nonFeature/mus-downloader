"""Qobuz через Zarz-шлюз (api.zarz.moe).

Поиск и скачивание идут через подписанные запросы к шлюзу: поиск —
GET /qbz/track/search, скачивание — тикет (resource_hash считается от URL
вида https://open.qobuz.com/track/<id> — так же делает официальный
экстеншен, иначе сервер отвечает «Ticket resource mismatch» 403) и
POST /dl/qbz, который возвращает прямую ссылку на FLAC.

Порт логики экстеншена Mornye `qobuz-web` (index.js + manifest.json).
"""
from pathlib import Path
from typing import Callable, Optional

import config

from . import zarz
from .matching import pick_best

OPEN_TRACK_URL = "https://open.qobuz.com/track/"


def _collection_items(payload: dict, key: str) -> list:
    """Достаёт список элементов из ответа zarz/qobuz (несколько форм формы)."""
    if not isinstance(payload, dict):
        return []
    node = payload.get(key)
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        items = node.get("items")
        if isinstance(items, list):
            return items
    data = payload.get("data")
    if isinstance(data, dict) and data is not node:
        return _collection_items(data, key)
    return []


def _raw_track_to_cand(track: dict) -> Optional[dict]:
    if not isinstance(track, dict) or not track.get("id"):
        return None
    performer = track.get("performer") or {}
    artist = performer.get("name") or ""
    if not artist:
        album = track.get("album") or {}
        artist = ((album.get("artist") or {}).get("name")) or ""
    duration = track.get("duration")
    try:
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    return {
        "id": str(track["id"]),
        "title": track.get("title") or "",
        "artist": artist,
        "duration": duration,
        "isrc": track.get("isrc") or "",
        "album": (track.get("album") or {}).get("title") or "",
        "hires": bool(track.get("hires") or (track.get("maximum_bit_depth") or 0) > 16),
    }


def search_qobuz_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
) -> Optional[dict]:
    """Подписанный поиск трека в каталоге Qobuz через Zarz."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    query = f"{artist} {title}".strip()
    if not query:
        return None
    try:
        from urllib.parse import urlencode

        path = "/qbz/track/search?" + urlencode({"query": query, "limit": 20, "offset": 0})
        payload = zarz.signed_json_sync("qobuz", "GET", path)
        items = _collection_items(payload or {}, "tracks")
    except zarz.ZarzUnavailableError as e:
        print(f"[!] Qobuz: {e}")
        return None
    except Exception as e:
        print(f"[!] Qobuz: ошибка поиска: {e}")
        return None

    candidates = [c for c in (_raw_track_to_cand(t) for t in items) if c]
    best = pick_best(candidates, artist=artist, title=title, duration=duration, isrc=isrc)
    if best:
        print(f"[*] Qobuz: найден трек {best['id']} ({best['artist']} - {best['title']})")
    return best


def _quality_chain(quality: str) -> list[str]:
    q = (quality or "cd").lower()
    if q == "hi-res-max":
        return ["hi-res-max", "hi-res", "cd"]
    if q == "hi-res":
        return ["hi-res", "cd"]
    return ["cd"]


def _extract_download_info(payload: dict) -> tuple[str, int, int]:
    """(direct_url, bit_depth, sample_rate) из ответа /dl/qbz."""
    if not isinstance(payload, dict):
        raise ValueError("пустой ответ /dl/qbz")
    for err_key in ("error", "detail"):
        if payload.get(err_key):
            raise RuntimeError(str(payload[err_key]))
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or "provider returned success=false"))
    data = payload.get("data")
    nested: dict = data if isinstance(data, dict) else {}
    url = ""
    for src in (payload, nested):
        for key in ("download_url", "url", "link"):
            if src.get(key):
                url = str(src[key]).strip()
                break
        if url:
            break
    if not url:
        raise RuntimeError("в ответе /dl/qbz нет download_url")

    def _num(*vals) -> int:
        for v in vals:
            try:
                if v:
                    return int(float(v))
            except (TypeError, ValueError):
                pass
        return 0

    bit_depth = _num(payload.get("bit_depth"), nested.get("bit_depth"))
    sample_rate = _num(payload.get("sampling_rate"), nested.get("sampling_rate"))
    if 0 < sample_rate < 1000:
        sample_rate *= 1000
    return url, bit_depth, sample_rate


def download_qobuz_track(
    track_id: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    quality: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cancel_event=None,
) -> Optional[Path]:
    """Скачивает FLAC из Qobuz через Zarz. Возвращает путь к файлу или None."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    target_quality = str(quality or getattr(config, "ZARZ_QOBUZ_QUALITY", "") or "cd")

    from .youtube import _clean_filename

    artist_name = _clean_filename(artist or "Unknown Artist")
    track_title = _clean_filename(title or f"Track_{track_id}")
    track_url = OPEN_TRACK_URL + str(track_id).strip()

    for q in _quality_chain(target_quality):
        try:
            payload = zarz.ticketed_download_info_sync(
                "qobuz",
                "qbz",
                track_url,  # resource_hash считается от URL, не от ID!
                "/dl/qbz",
                {
                    "quality": q,
                    "upload_to_r2": False,
                    "id": str(track_id).strip(),
                    "type": "track",
                    "url": track_url,
                },
            )
            direct_url, bit_depth, sample_rate = _extract_download_info(payload or {})
            out_path = dest_dir / f"{artist_name} - {track_title}.flac"
            zarz.stream_download(
                direct_url, out_path, "Qobuz",
                progress_callback=progress_callback, cancel_event=cancel_event,
            )
            if not zarz.is_flac(out_path):
                out_path.unlink(missing_ok=True)
                raise RuntimeError("скачанный файл не FLAC")
            label = f"{bit_depth}bit/{sample_rate // 1000}kHz" if bit_depth and sample_rate else q
            print(f"[+] Qobuz: скачан {out_path.name} ({label})")
            return out_path
        except zarz.ZarzUnavailableError as e:
            print(f"[!] Qobuz: {e}")
            return None
        except Exception as e:
            print(f"[!] Qobuz: {q} не удался: {type(e).__name__}: {e}")

    return None


def parse_qobuz_track_id(url: str) -> Optional[str]:
    """Достаёт ID трека из ссылки open.qobuz.com/track/<id> / qobuz.com/**/track-<slug>_<id>."""
    import re

    m = re.search(r"qobuz\.com/(?:\w{2}-\w{2}/)?(?:track/[\w-]+/|track/)(\d+)", url or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"open\.qobuz\.com/track/(\d+)", url or "", re.I)
    return m.group(1) if m else None
