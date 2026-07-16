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

def resolve_qobuz_track(isrc: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Ищет трек по ISRC на прокси-серверах Qobuz.
    Возвращает (track_id, title, performer) или (None, None, None).
    """
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
                
                # Фильтруем точное совпадение по ISRC
                exact_matches = [t for t in items if (t.get("isrc") or "").upper() == isrc.upper()]
                if exact_matches:
                    track = exact_matches[0]
                    # Предпочитаем Hi-Res версию при наличии
                    hires_matches = [t for t in exact_matches if t.get("hires")]
                    if hires_matches:
                        track = hires_matches[0]
                        
                    track_id = track.get("id")
                    title = track.get("title")
                    performer = track.get("performer", {}).get("name")
                    return track_id, title, performer
        except Exception as e:
            # Пробуем следующий прокси в списке
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

def download_monochrome_track(isrc: str, artist_hint: str, title_hint: str, dest_dir: Path, target_quality: str = "FLAC") -> Optional[Path]:
    """
    Скачивает трек во FLAC (или MP3 при выборе) используя ISRC.
    """
    if not isrc:
        print("[!] Monochrome: ISRC отсутствует, скачивание невозможно")
        return None

    # Находим ID трека на Qobuz
    track_id, title, performer = resolve_qobuz_track(isrc)
    if not track_id:
        print(f"[!] Monochrome: Не удалось найти трек с ISRC {isrc} на Qobuz прокси")
        return None

    artist = performer or artist_hint or "Unknown Artist"
    track_title = title or title_hint or "Unknown Track"
    
    clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist} - {track_title}")
    
    # Пытаемся получить поток.
    # Если затребован FLAC, сначала пробуем Hi-Res (27), затем стандартный FLAC (7).
    # В крайнем случае пробуем MP3 320 (6).
    stream_url = None
    ext = "flac"
    
    if target_quality == "FLAC":
        print("[*] Monochrome: Попытка получить Hi-Res FLAC (24-bit)...")
        stream_url = get_qobuz_download_url(track_id, 27)
        if not stream_url:
            print("[*] Monochrome: Hi-Res недоступен, пробуем стандартный FLAC (16-bit)...")
            stream_url = get_qobuz_download_url(track_id, 7)
        if not stream_url:
            print("[!] Monochrome: FLAC недоступен, пробуем MP3 320kbps...")
            stream_url = get_qobuz_download_url(track_id, 6)
            ext = "mp3"
    else:
        stream_url = get_qobuz_download_url(track_id, 6)
        ext = "mp3"

    if not stream_url:
        print("[!] Monochrome: Не удалось получить ссылку на поток")
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
