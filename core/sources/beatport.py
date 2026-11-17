"""Beatport через Zarz-шлюз (api.zarz.moe).

Каталог и скачивание целиком на стороне шлюза («provider credentials stay
on the server»): поиск — подписанный POST /btp/catalog, скачивание — тикет
+ POST /dl/btp, откуда приходит временная ссылка вида
``https://api.zarz.moe/v2/media/btp/<hash>?token=***

Особенность тикета: в теле /tickets провайдер ``btp``, а resource_hash
считается от строки ``beatport:track:<id>`` — ровно как в официальном
экстеншене Mornye `beatport` (index.js).
"""
import re
from pathlib import Path
from typing import Callable, Optional

import config

from . import zarz
from .matching import norm_text, pick_best

MEDIA_URL_RE = re.compile(
    r"^https://api\.zarz\.moe/v2/media/btp/[a-f0-9]{48}\?token=[A-Za-z0-9_.\-]+$"
)


def _track_title(raw: dict) -> str:
    title = str(raw.get("title") or "").strip()
    mix = str(raw.get("mix_name") or "").strip()
    if mix and mix.lower() not in title.lower():
        title = f"{title} ({mix})"
    return title


def _raw_track_to_cand(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    artists = ", ".join(
        str(a.get("name") or "") for a in (raw.get("artists") or []) if isinstance(a, dict)
    )
    duration_ms = raw.get("duration_ms") or 0
    try:
        duration = float(duration_ms) / 1000.0 if duration_ms else None
    except (TypeError, ValueError):
        duration = None
    return {
        "id": str(raw["id"]),
        "title": _track_title(raw),
        "artist": artists,
        "duration": duration,
        "isrc": raw.get("isrc") or "",
        "album": raw.get("album") or "",
        "available": bool(raw.get("available")),
    }


def _map_beatport_items(raw_tracks: list) -> list[dict]:
    """Сырые tracks из /btp/catalog search → кандидаты для агрегатора поиска."""
    out = []
    for raw in raw_tracks:
        cand = _raw_track_to_cand(raw)
        if not cand:
            continue
        cand["album_art"] = raw.get("cover") or None
        cand["explicit"] = bool(raw.get("explicit"))
        out.append(cand)
    return out


def search_beatport(query: str, limit: int = 10) -> list[dict]:
    """Подписанный поиск Beatport, но ТОЛЬКО при уже живой сессии Zarz.

    Агрегатор поиска не должен открывать браузер с капчей посреди
    пользовательского запроса — если сессии нет, источник молча пропускается
    (скачивание при выборе кандидата капчу при необходимости решит).
    """
    query = (query or "").strip()
    if not query or not getattr(config, "ZARZ_ENABLED", True):
        return []
    if not zarz.session_ready("beatport"):
        return []
    try:
        payload = zarz.signed_json_sync(
            "beatport",
            "POST",
            "/btp/catalog",
            json_body={
                "action": "search",
                "q": query,
                "type": "tracks",
                "limit": max(1, min(limit, 50)),
                "page": 1,
            },
        )
    except Exception:
        return []
    return _map_beatport_items((payload or {}).get("tracks") or [])


def search_beatport_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
) -> Optional[dict]:
    """Подписанный поиск трека в каталоге Beatport через Zarz."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    query = f"{title} {artist}".strip()
    if not query:
        return None
    try:
        payload = zarz.signed_json_sync(
            "beatport",
            "POST",
            "/btp/catalog",
            json_body={"action": "search", "q": query, "type": "tracks", "limit": 20, "page": 1},
        )
    except zarz.ZarzUnavailableError as e:
        print(f"[!] Beatport: {e}")
        return None
    except Exception as e:
        print(f"[!] Beatport: ошибка поиска: {e}")
        return None

    raw_tracks = (payload or {}).get("tracks") or []
    candidates = [c for c in (_raw_track_to_cand(t) for t in raw_tracks) if c]

    # Beatport отдаёт много ремиксов: если есть точное совпадение по ISRC —
    # берём его сразу, иначе скоринг с предпочтением доступных треков.
    if isrc:
        isrc_norm = re.sub(r"[^A-Za-z0-9]", "", isrc).upper()
        for cand in candidates:
            if cand.get("isrc") and cand["isrc"].upper() == isrc_norm and cand.get("available"):
                return cand

    # Тайтлы на Beatport почти всегда содержат «(Original Mix)» и подобные —
    # сравнение без микс-суффикса, иначе скоринг зарубит точные совпадения.
    query_norm = norm_text(title)
    for cand in candidates:
        cand["_title_plain"] = norm_text(re.sub(r"\([^)]*\)\s*$", "", cand["title"]))
        if query_norm and cand["_title_plain"] == query_norm:
            cand["title"] = re.sub(r"\([^)]*\)\s*$", "", cand["title"]).strip()

    available = [c for c in candidates if c.get("available")]
    best = pick_best(available or candidates, artist=artist, title=title, duration=duration, isrc=isrc)
    if best and not best.get("available"):
        print(f"[!] Beatport: трек {best['id']} найден, но недоступен для скачивания")
        return None
    if best:
        print(f"[*] Beatport: найден трек {best['id']} ({best['artist']} - {best['title']})")
    return best


def _quality_name(quality: str) -> str:
    q = (quality or "lossless").lower()
    if q in ("lossless", "flac", "best", "hi_res", "hi_res_lossless"):
        return "lossless"
    if q in ("high", "aac", "256"):
        return "high"
    raise ValueError(f"неподдерживаемое качество Beatport: {quality}")


def download_beatport_track(
    track_id: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    quality: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cancel_event=None,
) -> Optional[Path]:
    """Скачивает FLAC/AAC с Beatport через Zarz. Возвращает путь к файлу или None."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    try:
        q = _quality_name(str(quality or getattr(config, "ZARZ_BEATPORT_QUALITY", "") or "lossless"))
    except ValueError as e:
        print(f"[!] Beatport: {e}")
        return None

    from .youtube import _clean_filename

    artist_name = _clean_filename(artist or "Unknown Artist")
    track_title = _clean_filename(title or f"Track_{track_id}")

    try:
        payload = zarz.ticketed_download_info_sync(
            "beatport",
            "btp",
            str(track_id),
            "/dl/btp",
            {"id": str(track_id), "quality": q, "allow_fallback": False},
            hash_provider="beatport",  # resource_hash от "beatport:track:<id>"
        )
        result = payload or {}
        download_url = str(result.get("download_url") or "")
        if not MEDIA_URL_RE.match(download_url):
            print(f"[!] Beatport: неожиданный адрес медиа: {download_url[:120]}")
            return None
        expected_format = str(result.get("format") or ("flac" if q == "lossless" else "m4a"))
        expected_codec = "flac" if q == "lossless" else "aac"
        audio = result.get("audio") or {}
        if audio.get("codec") and audio["codec"] != expected_codec:
            print(f"[!] Beatport: сервер отдал кодек {audio['codec']} вместо {expected_codec}")
            return None
        try:
            expected_size = int(result.get("size") or 0) or None
        except (TypeError, ValueError):
            expected_size = None

        out_path = dest_dir / f"{artist_name} - {track_title}.{expected_format}"
        zarz.stream_download(
            download_url,
            out_path,
            "Beatport",
            progress_callback=progress_callback,
            expected_size=expected_size,
            cancel_event=cancel_event,
        )
        if expected_format == "flac" and not zarz.is_flac(out_path):
            out_path.unlink(missing_ok=True)
            raise RuntimeError("скачанный файл не FLAC")
        print(f"[+] Beatport: скачан {out_path.name}")
        return out_path
    except zarz.ZarzUnavailableError as e:
        print(f"[!] Beatport: {e}")
        return None
    except Exception as e:
        print(f"[!] Beatport: ошибка скачивания {track_id}: {e}")
        return None


def parse_beatport_track_id(url: str) -> Optional[str]:
    """Достаёт ID трека из ссылки beatport.com/track/<slug>/<id>."""
    m = re.search(
        r"beatport\.com/(?:[a-z]{2}/)?track/(?:[^/?#]+/)?([1-9]\d{0,14})/?(?:[?#]|$)",
        url or "",
        re.I,
    )
    return m.group(1) if m else None
