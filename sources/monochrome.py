import httpx
import re
from pathlib import Path
from typing import Optional
from config import MONOCHROME_QOBUZ_PROXY_URL, MONOCHROME_HIFI_URL

# Список публичных прокси Qobuz для отказоустойчивости
QOBUZ_PROXIES = [
    MONOCHROME_QOBUZ_PROXY_URL,
    "https://qobuz.kennyy.com.br",
    "https://mono.scavengerfurs.net",
    "https://qdl-api.monochrome.tf"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
}

def resolve_qobuz_track(isrc: str, query: str = None) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Ищет трек по ISRC или по текстовому запросу на прокси-серверах Qobuz.
    Возвращает (track_id, title, performer) или (None, None, None).
    """
    # 1. Попытка поиска по ISRC
    if isrc:
        for base_url in QOBUZ_PROXIES:
            if not base_url:
                continue
            try:
                url = f"{base_url.rstrip('/')}/api/get-music"
                print(f"[*] Monochrome: Запрос ISRC {isrc} на {base_url}")
                resp = httpx.get(url, params={"q": isrc, "offset": 0}, headers=HEADERS, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", {}).get("tracks", {}).get("items", [])
                    exact_matches = [t for t in items if (t.get("isrc") or "").upper() == isrc.upper()]
                    if exact_matches:
                        track = exact_matches[0]
                        hires_matches = [t for t in exact_matches if t.get("hires")]
                        if hires_matches:
                            track = hires_matches[0]
                        return str(track.get("id")), track.get("title"), track.get("performer", {}).get("name")
            except Exception:
                continue

    # 2. Попытка поиска по тексту (если по ISRC не нашли или его нет)
    if query:
        for base_url in QOBUZ_PROXIES:
            if not base_url:
                continue
            try:
                url = f"{base_url.rstrip('/')}/api/get-music"
                print(f"[*] Monochrome: Поиск по тексту '{query}' на {base_url}")
                resp = httpx.get(url, params={"q": query, "offset": 0}, headers=HEADERS, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", {}).get("tracks", {}).get("items", [])
                    if items:
                        track = items[0]
                        hires_items = [t for t in items if t.get("hires")]
                        if hires_items:
                            track = hires_items[0]
                        return str(track.get("id")), track.get("title"), track.get("performer", {}).get("name")
            except Exception:
                continue
                
    return None, None, None

def get_qobuz_download_url(track_id: str, quality_fmt: int) -> Optional[str]:
    """
    Получает прямую CDN ссылку для скачивания трека с Qobuz.
    quality_fmt: 27 (24-bit FLAC), 7 (16-bit FLAC), 6 (320kbps MP3/AAC)
    """
    for base_url in QOBUZ_PROXIES:
        if not base_url:
            continue
        try:
            url = f"{base_url.rstrip('/')}/api/download-music"
            resp = httpx.get(
                url,
                params={"track_id": track_id, "quality": quality_fmt},
                headers=HEADERS,
                timeout=12
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("success"):
                    stream_url = body.get("data", {}).get("url")
                    if stream_url:
                        return stream_url
        except Exception:
            continue
    return None

def _tidal_stream_url(tidal_id: str, quality: str) -> Optional[str]:
    """
    Получает прямой поток Tidal из Monochrome HiFi API.
    """
    hifi_api_endpoints = [
        MONOCHROME_HIFI_URL,
        "https://monochrome-api.samidy.com",
        "https://api.monochrome.tf",
        "https://us-west.monochrome.tf",
        "https://eu-central.monochrome.tf"
    ]
    
    q_val = "LOSSLESS" if quality == "FLAC" else "HIGH"
    
    for base in hifi_api_endpoints:
        if not base:
            continue
        try:
            url = f"{base.rstrip('/')}/track/"
            print(f"[*] Monochrome: Запрос потока Tidal {tidal_id} ({q_val}) на {base}")
            resp = httpx.get(url, params={"id": int(tidal_id), "quality": q_val}, headers=HEADERS, timeout=12)
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    for key in ("OriginalTrackUrl", "originalTrackUrl", "url"):
                        stream_url = data.get(key)
                        if stream_url and stream_url.startswith("http"):
                            return stream_url
                    urls = data.get("urls")
                    if urls and isinstance(urls, list) and urls[0].startswith("http"):
                        return urls[0]
        except Exception:
            continue
    return None

def download_monochrome_track(isrc: str, artist_hint: str, title_hint: str, dest_dir: Path, target_quality: str = "FLAC", tidal_id: str = None) -> Optional[Path]:
    """
    Скачивает трек во FLAC (или MP3 при выборе) через Qobuz прокси или Tidal hifi-api.
    """
    if not isrc and not (artist_hint and title_hint):
        print("[!] Monochrome: Идентификаторы отсутствуют, скачивание невозможно")
        return None

    # 1. Сначала пробуем Qobuz прокси (по ISRC или тексту)
    track_id, title, performer = resolve_qobuz_track(isrc, f"{artist_hint} - {title_hint}")
    
    artist = performer or artist_hint or "Unknown Artist"
    track_title = title or title_hint or "Unknown Track"
    clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist} - {track_title}")
    
    stream_url = None
    ext = "flac"
    
    if track_id:
        if target_quality == "FLAC":
            print("[*] Monochrome: Попытка получить Hi-Res FLAC (24-bit) с Qobuz...")
            stream_url = get_qobuz_download_url(track_id, 27)
            if not stream_url:
                print("[*] Monochrome: Hi-Res недоступен, пробуем стандартный FLAC (16-bit) с Qobuz...")
                stream_url = get_qobuz_download_url(track_id, 7)
            if not stream_url:
                print("[!] Monochrome: FLAC недоступен на Qobuz, пробуем MP3 320kbps...")
                stream_url = get_qobuz_download_url(track_id, 6)
                ext = "mp3"
        else:
            stream_url = get_qobuz_download_url(track_id, 6)
            ext = "mp3"

    # 2. Если Qobuz прокси не сработал, пробуем Tidal hifi-api
    if not stream_url and tidal_id:
        print("[*] Monochrome: Qobuz прокси не дали результатов. Попытка через Tidal hifi-api...")
        stream_url = _tidal_stream_url(tidal_id, target_quality)
        if stream_url:
            print("[+] Monochrome: Получен прямой поток из Tidal hifi-api")
            ext = "flac" if target_quality == "FLAC" else "mp3"

    if not stream_url:
        print("[!] Monochrome: Не удалось получить ссылку на поток ни из одного Monochrome источника")
        return None

    output_path = dest_dir / f"{clean_name}.{ext}"
    print(f"[*] Monochrome: Скачивание потока: {output_path}")
    
    try:
        with httpx.stream("GET", stream_url, headers=HEADERS, timeout=30) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
        print(f"[+] Monochrome: Скачивание завершено: {output_path}")
        return output_path
    except Exception as e:
        print(f"[!] Monochrome: Ошибка сохранения файла: {e}")
        if output_path.exists():
            output_path.unlink()
            
    return None
