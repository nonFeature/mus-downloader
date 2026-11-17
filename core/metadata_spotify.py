"""Spotify: ISRC, композиторы, лейбл, UPC и копирайты — без аккаунта.

Путь такой же, каким ходит web-плеер: анонимный access token (`/api/token`
с TOTP), client-token, затем pathfinder-запрос для поиска и protobuf-эндпоинт
`spclient.wg.spotify.com/metadata/4/{track,album}/{gid}` за «мелочами».

TIDAL и Apple дают почти всё то же; Spotify добавляет то, чего у них нет:
полный список авторов (artistRoles role=5), канонический ISRC, лейбл и
несколько копирайтов альбома строкой. Канал хрупкий — Spotify меняет TOTP
и client-token при каждом релизе клиента, поэтому любой сбой здесь просто
означает меньше полей, а не сломанное скачивание.

Протокол написан по наблюдениям за расширением Mornye (LGPL-3.0) — код не
переводился, только HTTP-контракт.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
import threading
import time
from typing import Any, Optional

import httpx

import config

WEB_BASE = "https://open.spotify.com"
TOKEN_URL = f"{WEB_BASE}/api/token"
CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
METADATA_URL = "https://spclient.wg.spotify.com/metadata/4"

#: Версия TOTP и «секрет» web-плеера: список байт, который перед HMAC
#: прогоняется через XOR с (i % 33 + 9) и base32. Меняется Spotify'ом.
TOTP_VERSION = 61
_TOTP_SECRET = [
    44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76, 94, 102,
    43, 69, 49, 120, 118, 80, 64, 78,
]

#: Persisted-query хеши pathfinder; живут до изменения схемы у Spotify.
SEARCH_HASH = "fcad5a3e0d5af727fb76966f06971c19cfa2275e6ff7671196753e008611873c"

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

_UA = config.BROWSER_USER_AGENT
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

_lock = threading.Lock()
_client: Optional[httpx.Client] = None
_client_version: str = ""
_access_token: str = ""
_access_expiry: float = 0.0
_client_token: str = ""
_client_token_expiry: float = 0.0
_device_id: str = ""


# ──────────────────────────────── TOTP ────────────────────────────────


def _b32_encode(data: bytes) -> str:
    out = []
    value = bits = 0
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            out.append(_B32[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits:
        out.append(_B32[(value << (5 - bits)) & 31])
    return "".join(out)


def _b32_decode(text: str) -> bytes:
    out = bytearray()
    value = bits = 0
    for char in text.upper().rstrip("="):
        index = _B32.find(char)
        if index < 0:
            continue
        value = (value << 5) | index
        bits += 5
        if bits >= 8:
            out.append((value >> (bits - 8)) & 0xFF)
            bits -= 8
    return bytes(out)


def totp_code(now: Optional[float] = None) -> str:
    """Шестизначный TOTP web-плеера для текущего 30-секундного окна."""
    transformed = [_TOTP_SECRET[i] ^ ((i % 33) + 9) for i in range(len(_TOTP_SECRET))]
    joined = "".join(str(value) for value in transformed)
    secret = _b32_decode(_b32_encode(bytes.fromhex("".join(f"{ord(c):02x}" for c in joined))))

    counter = int(now if now is not None else time.time()) // 30
    digest = hmac.new(secret, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return f"{value % 1_000_000:06d}"


# ────────────────────────────── сессия ───────────────────────────────


def _new_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


def _parse_client_version(page_html: str) -> str:
    match = re.search(
        r'<script id="appServerConfig" type="text/plain">([^<]+)</script>', page_html
    )
    if not match:
        return ""
    try:
        config_json = json.loads(base64.b64decode(match.group(1)))
    except Exception:
        return ""
    return str(config_json.get("clientVersion") or "")


def _ensure_session(force: bool = False) -> tuple[httpx.Client, dict]:
    """Готовит клиент с cookies и заголовками авторизации; кэш ~30 минут."""
    global _client, _client_version, _access_token, _access_expiry
    global _client_token, _client_token_expiry, _device_id

    with _lock:
        now = time.time()
        if (
            not force
            and _client is not None
            and _access_token
            and _client_token
            and now < min(_access_expiry, _client_token_expiry)
        ):
            return _client, _auth_headers()

        if _client is None:
            _client = _new_client()

        if force or not _client_version:
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    page = _client.get(f"{WEB_BASE}/")
                    page.raise_for_status()
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt:
                        raise
                    time.sleep(0.5)
            else:  # pragma: no cover - сюда не попадаем при успехе
                raise last_error or RuntimeError("Spotify: сессия не поднялась")
            _client_version = _parse_client_version(page.text)
            if not _client_version:
                raise RuntimeError("Spotify: нет clientVersion на странице")
            _device_id = _client.cookies.get("sp_t") or f"{random.getrandbits(128):032x}"

        code = totp_code()
        token_response = _client.get(
            TOKEN_URL,
            params={
                "reason": "init",
                "productType": "web-player",
                "totp": code,
                "totpVer": TOTP_VERSION,
                "totpServer": code,
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        _access_token = str(token_data.get("accessToken") or "")
        if not _access_token:
            raise RuntimeError("Spotify: пустой accessToken")
        client_id = str(token_data.get("clientId") or "")
        _access_expiry = float(
            token_data.get("accessTokenExpirationTimestampMs") or 0
        ) / 1000.0 or now + 3600

        client_token_response = _client.post(
            CLIENT_TOKEN_URL,
            json={
                "client_data": {
                    "client_version": _client_version,
                    "client_id": client_id,
                    "js_sdk_data": {
                        "device_brand": "unknown",
                        "device_model": "unknown",
                        "os": "windows",
                        "os_version": "NT 10.0",
                        "device_id": _device_id,
                        "device_type": "computer",
                    },
                }
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        client_token_response.raise_for_status()
        client_token_data = client_token_response.json()
        granted = (client_token_data.get("granted_token") or {}).get("token") or ""
        if not granted:
            raise RuntimeError(
                f"Spotify: client token отклонён ({client_token_data.get('response_type')})"
            )
        _client_token = granted
        _client_token_expiry = now + 25 * 60

        return _client, _auth_headers()


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token}",
        "Client-Token": _client_token,
        "Spotify-App-Version": _client_version,
        "App-Platform": "WebPlayer",
    }


def _reset_session() -> None:
    global _client, _access_token, _client_token, _client_version
    global _access_expiry, _client_token_expiry
    with _lock:
        _access_token = ""
        _client_token = ""
        _client_version = ""
        _access_expiry = 0.0
        _client_token_expiry = 0.0


def _request(method: str, url: str, allow_retry: bool = True, **kwargs) -> httpx.Response:
    client, headers = _ensure_session()
    merged = {**headers, **(kwargs.pop("headers", None) or {})}
    try:
        response = client.request(method, url, headers=merged, **kwargs)
    except httpx.TransportError:
        if not allow_retry:
            raise
        response = client.request(method, url, headers=merged, **kwargs)
    if response.status_code == 401 and allow_retry:
        _reset_session()
        client, headers = _ensure_session(force=True)
        merged = {**headers, **(kwargs.pop("headers", None) or {})}
        response = client.request(method, url, headers=merged, **kwargs)
    return response


# ─────────────────────────────── поиск ───────────────────────────────


def _track_items(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    search = data.get("searchV2") or {}
    items = (search.get("tracksV2") or {}).get("items") or []
    if not items:
        items = (search.get("tracks") or {}).get("items") or []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        track = item.get("item", {}).get("data") if isinstance(item.get("item"), dict) else None
        track = track or item.get("track") or item.get("data") or item
        if isinstance(track, dict) and track.get("name"):
            result.append(track)
    return result


def search_spotify_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    limit: int = 10,
) -> Optional[dict]:
    """Ищет трек в Spotify и возвращает лучшего кандидата."""
    from .metadata import score_text_candidate

    term = f"{artist} {title}".strip()
    if not term:
        return None

    payload = {
        "variables": {
            "searchTerm": term,
            "offset": 0,
            "limit": max(1, min(limit, 50)),
            "numberOfTopResults": 5,
            "includeAudiobooks": True,
            "includeArtistHasConcertsField": False,
            "includePreReleases": True,
            "includeAuthors": False,
        },
        "operationName": "searchDesktop",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": SEARCH_HASH}},
    }
    response = _request(
        "POST", PATHFINDER_URL, json=payload, headers={"Content-Type": "application/json"}
    )
    if response.status_code != 200:
        return None

    best: Optional[dict] = None
    best_score = 0.0
    for track in _track_items(response.json()):
        track_id = str(track.get("id") or "")
        if not track_id:
            continue
        artists = [
            str((entry.get("profile") or {}).get("name") or entry.get("name") or "")
            for entry in ((track.get("artists") or {}).get("items") or [])
        ]
        artists_str = ", ".join(name for name in artists if name)
        duration_ms = int(((track.get("duration") or {}).get("totalMilliseconds") or 0))
        score = score_text_candidate(
            cand_title=str(track.get("name") or ""),
            cand_artist=artists_str,
            target_query=term,
            target_title=title,
        )
        if duration and duration_ms:
            delta = abs(duration_ms / 1000.0 - float(duration))
            if delta > 45:
                continue
            if delta > 15:
                score -= 0.35
            elif delta > 8:
                score -= 0.15
        if score > best_score:
            best = {
                "id": track_id,
                "name": str(track.get("name") or ""),
                "artists": artists_str,
                "album": str((track.get("albumOfTrack") or {}).get("name") or ""),
                "album_id": str((track.get("albumOfTrack") or {}).get("id") or ""),
                "duration_ms": duration_ms,
            }
            best_score = score
    if best is None or best_score < 0.55:
        return None
    return best


def search_spotify(query: str, limit: int = 10) -> list[dict]:
    """Список кандидатов из анонимного поиска Spotify (pathfinder searchDesktop).

    Без капчи и аккаунта: client-token + TOTP-подпись, та же сессия, что
    используется для обогащения метаданных. Spotify не является источником
    скачивания — кандидаты нужны для агрегатора поиска (spotify_id, ISRC-
    связка и точные artist/title/album).
    """
    query = (query or "").strip()
    if not query:
        return []

    payload = {
        "variables": {
            "searchTerm": query,
            "offset": 0,
            "limit": max(1, min(limit, 50)),
            "numberOfTopResults": 5,
            "includeAudiobooks": False,
            "includeArtistHasConcertsField": False,
            "includePreReleases": True,
            "includeAuthors": False,
        },
        "operationName": "searchDesktop",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": SEARCH_HASH}},
    }
    try:
        response = _request(
            "POST", PATHFINDER_URL, json=payload, headers={"Content-Type": "application/json"}
        )
    except Exception:
        return []
    if response.status_code != 200:
        return []

    out: list[dict] = []
    for track in _track_items(response.json()):
        track_id = str(track.get("id") or "")
        if not track_id:
            continue
        artists = [
            str((entry.get("profile") or {}).get("name") or entry.get("name") or "")
            for entry in ((track.get("artists") or {}).get("items") or [])
        ]
        artists_str = ", ".join(name for name in artists if name)
        album = track.get("albumOfTrack") or {}
        cover = None
        sources = (album.get("coverArt") or {}).get("sources") or []
        if sources and isinstance(sources[0], dict):
            cover = sources[0].get("url")
        year = (album.get("date") or {}).get("year")
        duration_ms = int((track.get("duration") or {}).get("totalMilliseconds") or 0)
        out.append({
            "id": track_id,
            "title": str(track.get("name") or "").strip(),
            "artist": artists_str,
            "duration": (duration_ms / 1000.0) if duration_ms else None,
            "isrc": "",
            "album": str(album.get("name") or "") or None,
            "year": str(year) if year else None,
            "explicit": (track.get("contentRating") or {}).get("label") == "EXPLICIT",
            "album_art": cover,
        })
        if len(out) >= limit:
            break
    return out


# ──────────────────────────── protobuf ────────────────────────────


def _fields(data: bytes) -> dict[int, list[Any]]:
    """Разбирает protobuf в {номер поля: [значения]} (varint / length-delimited)."""
    out: dict[int, list[Any]] = {}
    position = 0
    length = len(data)

    def varint() -> int:
        nonlocal position
        result = 0
        shift = 0
        while position < length:
            byte = data[position]
            position += 1
            result += (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
        return result

    while position < length:
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            out.setdefault(number, []).append(varint())
        elif wire == 2:
            size = varint()
            out.setdefault(number, []).append(data[position : position + size])
            position += size
        elif wire == 5:
            out.setdefault(number, []).append(data[position : position + 4])
            position += 4
        elif wire == 1:
            out.setdefault(number, []).append(data[position : position + 8])
            position += 8
        else:
            break
    return out


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _sint(value: Any) -> int:
    number = int(value or 0)
    return -((number + 1) // 2) if number % 2 else number // 2


def _first(fields: dict, number: int) -> Any:
    values = fields.get(number) or []
    return values[0] if values else None


def _texts(fields: dict, number: int) -> list[str]:
    return [_text(item) for item in fields.get(number) or [] if item]


def spotify_gid(spotify_id: str) -> str:
    """Base62-идентификатор Spotify в hex-GID, который ждёт spclient."""
    number = 0
    for char in spotify_id:
        index = _B62.find(char)
        if index < 0:
            raise ValueError(f"Некорректный Spotify ID: {spotify_id!r}")
        number = number * 62 + index
    return number.to_bytes(16, "big").hex()


def _external_ids(fields: dict, number: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in fields.get(number) or []:
        entry = _fields(raw)
        key = _text(_first(entry, 1)).lower()
        value = _text(_first(entry, 2))
        if key and value:
            result.setdefault(key, value)
    return result


def _album_extras(raw: bytes) -> dict:
    fields = _fields(raw)
    external = _external_ids(fields, 10)
    copyrights = [
        _text(_first(_fields(item), 2)) for item in fields.get(13) or []
    ]
    discs = fields.get(11) or []
    total_tracks = 0
    total_discs = 0
    for disc in discs:
        disc_fields = _fields(disc)
        number = _sint(_first(disc_fields, 1)) or len(discs)
        total_discs = max(total_discs, number)
        total_tracks += len(disc_fields.get(3) or [])
    if not total_discs and discs:
        total_discs = len(discs)

    release_date = ""
    date_fields = _fields(_first(fields, 6) or b"")
    if date_fields:
        year = _sint(_first(date_fields, 1))
        month = _sint(_first(date_fields, 2))
        day = _sint(_first(date_fields, 3))
        if year:
            release_date = f"{year:04d}"
            if month:
                release_date += f"-{month:02d}"
                if day:
                    release_date += f"-{day:02d}"

    album_types = {1: "album", 2: "single", 3: "compilation", 4: "ep", 5: "audiobook", 6: "podcast"}
    return {
        "album": _text(_first(fields, 2)),
        "label": _text(_first(fields, 5)),
        "release_date": release_date,
        "upc": external.get("upc") or external.get("ean") or "",
        "copyright": "; ".join(item for item in copyrights if item),
        "genres": _texts(fields, 8),
        "track_total": total_tracks or None,
        "disc_total": total_discs or None,
        "album_type": album_types.get(int(_first(fields, 4) or 1), "album"),
    }


def spotify_track_extras(spotify_id: str) -> dict:
    """ISRC, авторы, лейбл, UPC, копирайты и номера из protobuf-метаданных."""
    gid = spotify_gid(spotify_id)
    response = _request(
        "GET", f"{METADATA_URL}/track/{gid}", params={"market": "from_token"}
    )
    if response.status_code != 200 or not response.content:
        return {}

    raw = response.content
    fields = _fields(raw)
    external = _external_ids(fields, 10)

    composers: list[str] = []
    for role_raw in fields.get(32) or []:
        role_fields = _fields(role_raw)
        if int(_first(role_fields, 3) or 0) == 5:
            name = _text(_first(role_fields, 2))
            if name:
                composers.append(name)

    album: dict = {}
    album_raw = _first(fields, 3)
    if album_raw:
        album = _album_extras(album_raw)
        # В трековом ответе альбом урезан (нет UPC, копирайтов, жанров и
        # списка дисков) — добираем полным ответом по его GID.
        album_gid = _first(_fields(album_raw), 1)
        if isinstance(album_gid, (bytes, bytearray)) and len(album_gid) == 16:
            details = _request("GET", f"{METADATA_URL}/album/{bytes(album_gid).hex()}")
            if details.status_code == 200 and details.content:
                full = _album_extras(details.content)
                for key, value in full.items():
                    if value not in (None, "", []) and not album.get(key):
                        album[key] = value

    isrc = external.get("isrc", "").upper()
    if not isrc:
        # Тот же резервный путь, что у веб-клиента: строкой в сырых байтах.
        match = re.search(rb"isrc[\x00-\x1f]{1,4}([A-Za-z0-9]{12})", raw)
        if match:
            isrc = match.group(1).decode("ascii", "ignore").upper()

    extras = {
        "isrc": isrc,
        "composer": "; ".join(dict.fromkeys(composers)),
        "label": album.get("label", ""),
        "copyright": album.get("copyright", ""),
        "upc": album.get("upc", ""),
        "album": album.get("album", ""),
        "album_type": album.get("album_type", ""),
        "release_date": album.get("release_date", ""),
        "year": album.get("release_date", "")[:4],
        "genre": (album.get("genres") or [""])[0],
        "genres": album.get("genres") or [],
        "disc_number": _sint(_first(fields, 6)) or None,
        "disc_total": album.get("disc_total"),
        "track_number": _sint(_first(fields, 5)) or None,
        "track_total": album.get("track_total"),
        "duration_ms": _sint(_first(fields, 7)) or None,
        "explicit": int(_first(fields, 9) or 0) == 1,
        "source": "Spotify",
    }
    return {k: v for k, v in extras.items() if v not in (None, "", [])}


def extract_isrc_from_url(spotify_id: str) -> str:
    """Быстрый ISRC без поиска: только protobuf по известному ID."""
    try:
        return str(spotify_track_extras(spotify_id).get("isrc") or "")
    except Exception:
        return ""


def spotify_id_from_url(url: str) -> str:
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]{22})", url or "")
    if match:
        return match.group(1)
    match = re.search(r"spotify:track:([A-Za-z0-9]{22})", url or "")
    return match.group(1) if match else ""


def _reset_session_cache() -> None:
    """Для тестов: забыть сессию целиком."""
    global _client, _client_version, _device_id
    _reset_session()
    with _lock:
        _client = None
        _client_version = ""
        _device_id = ""
