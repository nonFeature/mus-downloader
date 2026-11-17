"""Дополнительные метаданные из публичных web-API TIDAL и Apple Music.

Оба протокола известны по расширениям Mornye (tidal-web / apple-music там
LGPL-3.0): код здесь написан с нуля по HTTP-контракту — ни строки их JS не
переведено, чтобы лицензия не переехала в этот MIT-проект.

Что даёт TIDAL (`tidal.com/v1` с публичным web-токеном, без аккаунта):
ISRC, ℗-копирайт, номер диска и трека, число дисков/треков с альбома, UPC,
дата релиза, BPM, тональность, ReplayGain, обложка, заявленное качество.

Что даёт Apple Music (`amp-api.music.apple.com`, токен выдирается из страницы
music.apple.com): композиторы, лейбл (recordLabel), жанры пачкой, UPC,
℗-копирайт альбома, компиляция.

Ничего не бросает наружу: любой сбой источника — просто меньше полей.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
from typing import Any, Optional

import httpx

import config

TIDAL_API = "https://tidal.com/v1"
# Тот самый публичный web-токен, с которым ходит listen.tidal.com.
TIDAL_PUBLIC_TOKEN = "49YxDN9a2aFV6RTG"

APPLE_API = "https://amp-api.music.apple.com/v1"


def _storefront() -> str:
    """Регион каталога Apple (нижний регистр — так его ждёт amp-api)."""
    return (getattr(config, "APPLE_STOREFRONT", "us") or "us").strip().lower()


def _tidal_country() -> str:
    return (getattr(config, "TIDAL_COUNTRY_CODE", "US") or "US").strip().upper()


def _apple_browse() -> str:
    return f"https://music.apple.com/{_storefront()}/browse"

UA = config.BROWSER_USER_AGENT
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# Токен Apple живёт ~12 часов; держим его в памяти процесса и обновляем по 401.
_apple_token: Optional[str] = None
_apple_token_expiry: float = 0.0


# ────────────────────────────── helpers ──────────────────────────────


def _clean(value: Any) -> str:
    """Схлопывает HTML-сущности и лишние пробелы; None -> ''."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    return re.sub(r"\s+", " ", text).strip()


def _first(*values: Any) -> str:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cache():
    """Кэш метаданных, если он включён (в тестах его нет — это нормально)."""
    try:
        from . import cache as cache_mod

        return cache_mod.get_cache()
    except Exception:
        return None


# ─────────────────────────────── TIDAL ───────────────────────────────


def _tidal_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    url = path if path.startswith("http") else f"{TIDAL_API}{path}"
    query = {"countryCode": _tidal_country(), "deviceType": "BROWSER"}
    if params:
        query.update(params)
    headers = {
        "x-tidal-token": TIDAL_PUBLIC_TOKEN,
        "Origin": "https://listen.tidal.com",
        "Referer": "https://listen.tidal.com/",
        "User-Agent": UA,
    }
    try:
        response = httpx.get(url, params=query, headers=headers, timeout=_TIMEOUT)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:
        return None


def tidal_search_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    limit: int = 10,
) -> Optional[dict]:
    """Ищет трек в TIDAL и возвращает лучшего кандидата (dict из /search/tracks)."""
    from .metadata import score_text_candidate

    query = f"{artist} {title}".strip()
    if not query:
        return None
    payload = _tidal_get("/search/tracks", {"query": query, "limit": str(limit)})
    items = (payload or {}).get("items") or []
    if not items:
        return None

    target = f"{artist} {title}"
    best: Optional[dict] = None
    best_score = 0.0
    for item in items:
        cand_title = _clean(item.get("title"))
        cand_artist = _clean((item.get("artist") or {}).get("name"))
        score = score_text_candidate(
            cand_title=cand_title,
            cand_artist=cand_artist,
            target_query=target,
            target_title=title,
        )
        # Длительность — второй голос: каверы и ремиксы часто живут дольше.
        cand_duration = _int_or_none(item.get("duration")) or 0
        if duration and cand_duration:
            delta = abs(cand_duration - float(duration))
            if delta > 45:
                continue
            if delta > 15:
                score -= 0.35
            elif delta > 8:
                score -= 0.15
        # Версия "(Live)", "(Remix)" уже штрафуется scorer'ом, но студийный
        # дубль с explicit/clean тоже путает выдачу — не мешаем: обе версии
        # годятся, звук один.
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < 0.55:
        return None
    return best


def tidal_track_extras(track_id: Any) -> dict:
    """Все «мелочи», которые TIDAL отдаёт по треку и его альбому."""
    payload = _tidal_get(f"/tracks/{track_id}")
    if not payload:
        return {}

    album_stub = payload.get("album") or {}
    album: dict = {}
    if album_stub.get("id"):
        album = _tidal_get(f"/albums/{album_stub['id']}") or {}

    cover = _clean(album.get("cover") or album_stub.get("cover"))
    extras: dict = {
        "isrc": _clean(payload.get("isrc")),
        "copyright": _first(album.get("copyright"), payload.get("copyright")),
        "disc_number": _int_or_none(payload.get("volumeNumber")),
        "disc_total": _int_or_none(album.get("numberOfVolumes")),
        "track_number": _int_or_none(payload.get("trackNumber")),
        "track_total": _int_or_none(album.get("numberOfTracks")),
        "release_date": _clean(album.get("releaseDate")),
        "upc": _clean(album.get("upc")),
        "bpm": _int_or_none(payload.get("bpm")),
        "initial_key": _clean(payload.get("key")),
        "audio_quality": _clean(payload.get("audioQuality")),
        "explicit": payload.get("explicit"),
        "album": _clean(album.get("title")),
        "year": (_first(album.get("releaseDate"))[:4] or ""),
        "cover_url": (
            f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg"
            if cover
            else ""
        ),
        "source": "TIDAL",
    }
    # ReplayGain TIDAL считает на своей стороне — это точнее, чем считать самим.
    gain = _float_or_none(payload.get("replayGain"))
    peak = _float_or_none(payload.get("peak"))
    if gain is not None:
        extras["replaygain_track_gain"] = gain
    if peak is not None:
        extras["replaygain_track_peak"] = peak
    return {k: v for k, v in extras.items() if v not in (None, "", 0) or k == "explicit"}


# ──────────────────────────── Apple Music ────────────────────────────


def extract_apple_token(page_html: str = "", bundles: Optional[list[str]] = None) -> str:
    """Вытаскивает devToken Apple из HTML страницы или её JS-бандлов.

    Публичный «WebPlayKid» JWT: именно он открывает amp-api анонимно.
    """
    candidates: list[str] = []
    text = page_html or ""
    direct = re.search(r"devToken=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", text)
    if direct:
        return direct.group(1)
    candidates.extend(re.findall(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", text))
    for blob in bundles or []:
        candidates.extend(re.findall(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", blob))

    fallback = ""
    for token in candidates:
        try:
            header = token.split(".")[0]
            header += "=" * (-len(header) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(header.encode()))
        except Exception:
            continue
        if decoded.get("kid") == "WebPlayKid":
            return token
        fallback = fallback or token
    return fallback


def _fetch_apple_token() -> str:
    headers = {"User-Agent": UA, "Accept": "text/html"}
    try:
        page = httpx.get(_apple_browse(), headers=headers, timeout=_TIMEOUT, follow_redirects=True)
    except Exception:
        return ""
    if page.status_code != 200:
        return ""
    token = extract_apple_token(page.text)
    if token:
        return token

    bundles: list[str] = []
    for src in re.findall(r'src="([^"]+\.js)"', page.text)[:12]:
        url = src if src.startswith("http") else f"https://music.apple.com{src}"
        try:
            bundle = httpx.get(url, headers=headers, timeout=_TIMEOUT)
        except Exception:
            continue
        if bundle.status_code == 200:
            bundles.append(bundle.text)
    return extract_apple_token("", bundles)


def _apple_dev_token(force: bool = False) -> str:
    global _apple_token, _apple_token_expiry

    if not force and _apple_token and time.time() < _apple_token_expiry:
        return _apple_token
    token = _fetch_apple_token()
    if not token:
        return _apple_token or ""
    _apple_token = token
    _apple_token_expiry = time.time() + 6 * 60 * 60
    return token


def _apple_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    url = path if path.startswith("http") else f"{APPLE_API}{path}"
    for attempt in (0, 1):
        token = _apple_dev_token(force=attempt == 1)
        if not token:
            return None
        headers = {
            "User-Agent": UA,
            "Authorization": f"Bearer {token}",
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Accept": "application/json",
        }
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        except Exception:
            return None
        if response.status_code == 401 and attempt == 0:
            continue
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except Exception:
            return None
    return None


def apple_search_song(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: str = "",
) -> Optional[dict]:
    """Ищет трек в каталоге Apple Music: сначала по ISRC, потом по тексту."""
    storefront = _storefront()

    if isrc:
        payload = _apple_get(f"/catalog/{storefront}/songs", {"filter[isrc]": isrc, "limit": "3"})
        data = (payload or {}).get("data") or []
        if data:
            return data[0]

    from .metadata import score_text_candidate

    query = f"{artist} {title}".strip()
    if not query:
        return None
    payload = _apple_get(
        f"/catalog/{storefront}/search",
        {"term": query, "types": "songs", "limit": "10"},
    )
    data = ((payload or {}).get("results") or {}).get("songs", {}).get("data") or []
    if not data:
        return None

    best: Optional[dict] = None
    best_score = 0.0
    for item in data:
        attrs = item.get("attributes") or {}
        score = score_text_candidate(
            cand_title=_clean(attrs.get("name")),
            cand_artist=_clean(attrs.get("artistName")),
            target_query=query,
            target_title=title,
        )
        cand_duration = (_int_or_none(attrs.get("durationInMillis")) or 0) / 1000.0
        if duration and cand_duration:
            delta = abs(cand_duration - float(duration))
            if delta > 45:
                continue
            if delta > 15:
                score -= 0.35
            elif delta > 8:
                score -= 0.15
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < 0.55:
        return None
    return best


def apple_song_extras(song: Any) -> dict:
    """Приводит элемент каталога Apple к общему словарю полей."""
    if isinstance(song, dict) and "attributes" in song:
        song_id = song.get("id")
        attrs = song.get("attributes") or {}
    else:
        song_id, attrs = song, {}

    album_attrs: dict = {}
    if song_id:
        payload = _apple_get(f"/catalog/{_storefront()}/songs/{song_id}", {"include": "albums"})
        albums = ((payload or {}).get("data") or [{}])[0].get("relationships", {}).get("albums", {})
        album_data = albums.get("data") or []
        if album_data:
            album_attrs = album_data[0].get("attributes") or {}

    genres = [g for g in (attrs.get("genreNames") or []) if _clean(g).lower() != "music"]
    album_genres = [g for g in (album_attrs.get("genreNames") or []) if _clean(g).lower() != "music"]
    release_date = _first(attrs.get("releaseDate"), album_attrs.get("releaseDate"))

    extras: dict = {
        "isrc": _clean(attrs.get("isrc")),
        "composer": _clean(attrs.get("composerName")),
        "label": _clean(album_attrs.get("recordLabel")),
        "copyright": _first(album_attrs.get("copyright"), attrs.get("copyright")),
        "disc_number": _int_or_none(attrs.get("discNumber")),
        "track_number": _int_or_none(attrs.get("trackNumber")),
        "track_total": _int_or_none(album_attrs.get("trackCount")),
        "release_date": release_date,
        "year": release_date[:4],
        "upc": _clean(album_attrs.get("upc")),
        "genre": _first(*(genres or album_genres)),
        "genres": genres or album_genres,
        "compilation": album_attrs.get("isCompilation"),
        "album": _clean(_first(attrs.get("albumName"), album_attrs.get("name"))),
        "cover_url": _clean(((attrs.get("artwork") or {}).get("url") or "")).replace(
            "{w}", "1400"
        ).replace("{h}", "1400"),
        "source": "Apple Music",
    }
    return {k: v for k, v in extras.items() if v not in (None, "", [])}


# ───────────────────────────── merge ─────────────────────────────────


def _merge(target: dict, payload: dict) -> None:
    for key, value in (payload or {}).items():
        if value in (None, "", []):
            continue
        if key in ("explicit", "compilation"):
            target.setdefault(key, value)
            continue
        if not target.get(key):
            target[key] = value


def collect_extras(
    artist: str = "",
    title: str = "",
    album: str = "",
    duration: Optional[float] = None,
    isrc: str = "",
    sources: Optional[tuple[str, ...]] = None,
) -> dict:
    """Собирает дополнительные поля из включённых источников.

    Порядок по умолчанию: Apple (композитор, лейбл, жанры) → TIDAL (ISRC,
    номера диска/трека, BPM, тональность, ReplayGain, обложка). Первое
    непустое значение поля побеждает, второй источник дополняет пробелы.
    """
    if not artist and not title and not isrc:
        return {}

    sources = sources or tuple(
        name
        for name, enabled in (
            ("apple", getattr(config, "APPLE_METADATA", True)),
            ("tidal", getattr(config, "TIDAL_METADATA", True)),
        )
        if enabled
    )
    if not sources:
        return {}

    cache = _cache()
    cache_key = f"{artist}|{title}|{isrc}|{int(float(duration or 0))}"
    if cache is not None:
        try:
            cached = cache.get("extras", cache_key)
            if isinstance(cached, dict):
                return cached
        except Exception:
            pass

    extras: dict = {}
    for name in sources:
        try:
            if name == "apple":
                found = apple_search_song(artist, title, duration=duration, isrc=isrc)
                if found:
                    _merge(extras, apple_song_extras(found))
            elif name == "tidal":
                found = tidal_search_track(artist, title, duration=duration)
                if found and found.get("id"):
                    _merge(extras, tidal_track_extras(found["id"]))
        except Exception as exc:  # источник не должен ломать скачивание
            print(f"[!] {name}: доп. метаданные недоступны ({exc})")
            continue

    if extras and cache is not None:
        try:
            cache.set("extras", cache_key, extras, ttl_days=30)
        except Exception:
            pass
    return extras


def _reset_apple_token_cache() -> None:
    """Для тестов: забыть выданный токен."""
    global _apple_token, _apple_token_expiry
    _apple_token = None
    _apple_token_expiry = 0.0
