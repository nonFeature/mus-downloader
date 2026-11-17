"""Amazon Music: поиск через анонимный mesk-API + скачивание через Zarz.

Две независимые части (порт экстеншена Mornye `amzn`):

1. Поиск — анонимная сессия web-плеера Amazon: POST config.json выдаёт
   deviceId/sessionId/CSRF, дальше POST /api/showSearch на
   na.mesk.skill.music.a2z.com. Аккаунт не нужен.
2. Скачивание — Zarz-шлюз: тикет (provider ``amazeamazeamaze``, hash от
   ASIN) + POST /dl/amazeamazeamaze {asin, codec} → зашифрованный
   контейнер MOV + ключ; расшифровка через ``ffmpeg -decryption_key``.
"""
import json
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

import config

from . import zarz
from .matching import pick_best

MUSIC_BASE_URL = "https://music.amazon.com"
SKILL_API_BASE = "https://na.mesk.skill.music.a2z.com/api"
DEVICE_MODEL = "WEBPLAYER"
DEVICE_FAMILY = "WebPlayer"
APP_VERSION_FALLBACK = "1.0.9678.0"
ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


# ─────────────────────────── mesk-сессия (поиск) ───────────────────────────

_session: dict = {}


def _init_session() -> None:
    """POST config.json выдаёт deviceId/sessionId/CSRF — анонимно."""
    if _session.get("initialized"):
        return
    resp = httpx.post(
        f"{MUSIC_BASE_URL}/config.json?skipToken=false&clientApplication=skyfire",
        headers={"User-Agent": _random_ua(), "Accept": "application/json"},
        timeout=httpx.Timeout(15.0, connect=8.0),
    )
    resp.raise_for_status()
    cfg = resp.json() or {}
    csrf = cfg.get("csrf") or {}
    _session.update(
        {
            "deviceId": cfg.get("deviceId") or "",
            "sessionId": cfg.get("sessionId") or "",
            "appVersion": cfg.get("version") or APP_VERSION_FALLBACK,
            "displayLanguage": cfg.get("displayLanguage") or "en_US",
            "csrfToken": csrf.get("token") or "",
            "csrfTs": str(csrf.get("ts") or int(time.time())),
            "csrfRnd": str(csrf.get("rnd") or random.randint(0, 2_000_000_000)),
            "initialized": True,
        }
    )


def _reset_session() -> None:
    _session.clear()


def _build_amzn_headers(page_url: str) -> str:
    csrf = json.dumps(
        {
            "interface": "CSRFInterface.v1_0.CSRFHeaderElement",
            "token": _session.get("csrfToken", ""),
            "timestamp": _session.get("csrfTs") or str(int(time.time())),
            "rndNonce": _session.get("csrfRnd") or str(random.randint(0, 2_000_000_000)),
        }
    )
    auth = json.dumps(
        {
            "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
            "accessToken": "",
        }
    )
    return json.dumps(
        {
            "x-amzn-authentication": auth,
            "x-amzn-device-model": DEVICE_MODEL,
            "x-amzn-device-width": "1920",
            "x-amzn-device-family": DEVICE_FAMILY,
            "x-amzn-device-id": _session.get("deviceId", ""),
            "x-amzn-user-agent": _random_ua(),
            "x-amzn-session-id": _session.get("sessionId", ""),
            "x-amzn-device-height": "1080",
            "x-amzn-request-id": f"{random.random():.13f}"[2:] + "-" + str(int(time.time() * 1000)),
            "x-amzn-device-language": _session.get("displayLanguage") or "en_US",
            "x-amzn-currency-of-preference": "USD",
            "x-amzn-os-version": "1.0",
            "x-amzn-application-version": _session.get("appVersion") or APP_VERSION_FALLBACK,
            "x-amzn-device-time-zone": "UTC",
            "x-amzn-timestamp": str(int(time.time() * 1000)),
            "x-amzn-csrf": csrf,
            "x-amzn-music-domain": "music.amazon.com",
            "x-amzn-referer": "",
            "x-amzn-affiliate-tags": "",
            "x-amzn-ref-marker": "",
            "x-amzn-page-url": page_url,
            "x-amzn-weblab-id-overrides": "",
            "x-amzn-video-player-token": "",
            "x-amzn-feature-flags": "",
            "x-amzn-has-profile-id": "",
            "x-amzn-age-band": "",
        }
    )


def _show_search(keyword: str) -> dict:
    _init_session()
    from urllib.parse import quote

    page_url = f"{MUSIC_BASE_URL}/search/{quote(keyword)}"
    body = {
        "filter": json.dumps({"IsLibrary": ["false"]}),
        "keyword": json.dumps(
            {
                "interface": "Web.TemplatesInterface.v1_0.Touch.SearchTemplateInterface.SearchKeywordClientInformation",
                "keyword": keyword,
            }
        ),
        "suggestedKeyword": keyword,
        "userHash": json.dumps({"level": "LIBRARY_MEMBER"}),
        "headers": _build_amzn_headers(page_url),
    }
    resp = httpx.post(
        f"{SKILL_API_BASE}/showSearch",
        content=json.dumps(body),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "User-Agent": _random_ua(),
            "Origin": MUSIC_BASE_URL,
            "Referer": page_url,
        },
        timeout=httpx.Timeout(20.0, connect=8.0),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"showSearch HTTP {resp.status_code}")
    return resp.json() or {}


def _text_value(value) -> str:
    """Аналог textValue() из JS: текст может лежать в .text/.defaultValue."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text:
            return text
        for key in ("defaultValue",):
            nested = _text_value(value.get(key))
            if nested:
                return nested
        observer = value.get("observer")
        if isinstance(observer, dict):
            nested = _text_value(observer.get("defaultValue"))
            if nested:
                return nested
    return ""


def _parse_duration_mmss(value) -> float:
    text = _text_value(value)
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def _normalize_asin(candidate: str) -> Optional[str]:
    s = (candidate or "").strip()
    if not s:
        return None
    try:
        s = unquote(s)
    except Exception:
        pass
    s = s.upper()
    cut = re.search(r"[?#&/]", s)
    if cut:
        s = s[: cut.start()]
    return s if ASIN_RE.match(s) else None


def _deeplink_track_info(deeplink: str) -> Optional[dict]:
    """/tracks/<ASIN> или /albums/<ASIN>?trackAsin=<ASIN> → {id, albumId}."""
    if not deeplink:
        return None
    try:
        parsed = urlparse(deeplink if "://" in deeplink else MUSIC_BASE_URL + deeplink)
    except ValueError:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return None
    kind = segments[0].lower()
    raw_id = segments[1] if len(segments) > 1 else ""
    if kind == "albums":
        track_asin = (parse_qs(parsed.query).get("trackAsin") or [""])[0]
        asin = _normalize_asin(track_asin)
        if asin:
            return {"id": asin, "albumId": _normalize_asin(raw_id) or raw_id}
        return None
    if kind == "tracks":
        asin = _normalize_asin(raw_id)
        return {"id": asin, "albumId": ""} if asin else None
    return None


def _walk_track_items(node, out: list) -> None:
    """Рекурсивно собирает элементы-треки из ответа showSearch."""
    if isinstance(node, dict):
        iface = str(node.get("interface") or "")
        if "DescriptiveRowItemElement" in iface or "SquareHorizontalItemElement" in iface:
            out.append(node)
        for value in node.values():
            _walk_track_items(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_track_items(value, out)


def _parse_search_results(data: dict) -> list[dict]:
    items: list = []
    _walk_track_items(data, items)
    candidates: list[dict] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _text_value(item.get("primaryText"))
        deeplink = ""
        for link_key in ("primaryTextLink", "primaryLink"):
            link = item.get(link_key)
            if isinstance(link, dict) and link.get("deeplink"):
                deeplink = str(link["deeplink"])
                break
        info = _deeplink_track_info(deeplink)
        if not title or not info or info["id"] in seen:
            continue
        seen.add(info["id"])
        artist = _text_value(item.get("secondaryText1")) or _text_value(item.get("secondaryText"))
        duration = _parse_duration_mmss(item.get("secondaryText3")) or _parse_duration_mmss(
            item.get("duration")
        )
        candidates.append(
            {
                "id": info["id"],
                "title": title,
                "artist": artist,
                "duration": duration or None,
                "isrc": "",
                "album_id": info.get("albumId") or "",
            }
        )
    return candidates


def search_amazon_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    album: Optional[str] = None,
) -> Optional[dict]:
    """Ищет трек через анонимный showSearch. Возвращает кандидат с ASIN или None."""
    query = f"{title} {artist}".strip()
    if not query:
        return None
    try:
        data = _show_search(query)
    except Exception as e:
        # Одна попытка со свежей сессией — как fetchWithRetry/refreshSession в JS.
        try:
            _reset_session()
            data = _show_search(query)
        except Exception as e2:
            print(f"[!] Amazon: ошибка поиска: {e2}")
            return None
    candidates = _parse_search_results(data)
    best = pick_best(candidates, artist=artist, title=title, duration=duration)
    if best:
        print(f"[*] Amazon: найден ASIN {best['id']} ({best['artist']} - {best['title']})")
    return best


# ─────────────────────────── скачивание через Zarz ───────────────────────────


def _decrypt_with_ffmpeg(src: Path, key: str, out: Path) -> None:
    """ffmpeg -decryption_key: контейнер MOV с шифрованным потоком (mov_key)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg не найден в PATH — не могу расшифровать поток Amazon")
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-decryption_key", key,
        "-i", str(src),
        "-c", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg-расшифровка не удалась: {proc.stderr.strip()[:300]}")


def download_amazon_track(
    asin: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    codec: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    cancel_event=None,
) -> Optional[Path]:
    """Скачивает трек Amazon Music через Zarz. Возвращает путь к файлу или None."""
    if not getattr(config, "ZARZ_ENABLED", True):
        return None
    target_codec = str(codec or getattr(config, "ZARZ_AMAZON_CODEC", "") or "flac").lower()
    if target_codec not in ("flac", "opus", "eac3", "ac4"):
        target_codec = "flac"

    from .youtube import _clean_filename

    artist_name = _clean_filename(artist or "Unknown Artist")
    track_title = _clean_filename(title or f"Track_{asin}")

    try:
        payload = zarz.ticketed_download_info_sync(
            "amazon",
            "amazeamazeamaze",
            str(asin),
            "/dl/amazeamazeamaze",
            {"asin": str(asin), "codec": target_codec},
        )
        if isinstance(payload, list):
            data = payload[0] if payload else {}
        else:
            data = payload or {}
        if not isinstance(data, dict):
            data = {}
        audio_node = data.get("audio")
        audio: dict = audio_node if isinstance(audio_node, dict) else {}
        stream_url = str(audio.get("url") or "")
        if not stream_url:
            print(f"[!] Amazon: в ответе zarz нет audio.url")
            return None
        key = str(audio.get("key") or "").strip()
        actual_codec = str(audio.get("codec") or target_codec).lower()

        if actual_codec == "ac4":
            ext = "mp4"
        elif actual_codec == "opus":
            # Opus не ложится в m4a/ipod-контейнер («Could not find tag for
            # codec opus») — держим в Ogg (.opus), mutagen его тегает, а
            # пайплайн знает качество OPUS.
            ext = "opus"
        elif actual_codec == "eac3":
            ext = "m4a"
        else:
            ext = "flac"

        raw_path = dest_dir / f"{artist_name} - {track_title}.raw.tmp"
        zarz.stream_download(
            stream_url,
            raw_path,
            "Amazon Music",
            progress_callback=progress_callback,
            headers={"User-Agent": _random_ua()},
            cancel_event=cancel_event,
        )

        out_path = dest_dir / f"{artist_name} - {track_title}.{ext}"
        try:
            if key:
                _decrypt_with_ffmpeg(raw_path, key, out_path)
            else:
                raw_path.replace(out_path)
        finally:
            raw_path.unlink(missing_ok=True)

        if ext == "flac" and not zarz.is_flac(out_path):
            out_path.unlink(missing_ok=True)
            raise RuntimeError("расшифрованный файл не FLAC")
        print(f"[+] Amazon: скачан {out_path.name} ({actual_codec})")
        return out_path
    except zarz.ZarzUnavailableError as e:
        print(f"[!] Amazon: {e}")
        return None
    except Exception as e:
        print(f"[!] Amazon: ошибка скачивания {asin}: {e}")
        return None


def parse_amazon_asin(url: str) -> Optional[str]:
    """Достаёт ASIN трека из ссылки music.amazon.com/tracks/<ASIN> и подобных."""
    info = _deeplink_track_info(url or "")
    return info["id"] if info else None
