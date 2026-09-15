import hashlib
import re
import httpx
from pathlib import Path
from Crypto.Cipher import Blowfish
from typing import Optional

PROXY_API = "https://lufts-dzmedia.fly.dev/get_url"
SECRET = b"g4el58wc0zvf9na1"

# Маппинг качеств
QUALITIES = {
    "FLAC": {"proxyName": "FLAC", "ext": "flac"},
    "MP3_320": {"proxyName": "MP3_320", "ext": "mp3"},
    "MP3_128": {"proxyName": "MP3_128", "ext": "mp3"},
}

def get_blowfish_key(track_id: str) -> bytes:
    """Генерирует Blowfish-ключ для расшифровки трека по его ID."""
    md5 = hashlib.md5(str(track_id).encode()).hexdigest()
    key = bytearray(16)
    for i in range(16):
        key[i] = ord(md5[i]) ^ ord(md5[i + 16]) ^ SECRET[i]
    return bytes(key)

def decrypt_and_save(stream_iterator, bf_key: bytes, dest_path: Path):
    """Дешифрует и сохраняет стрим трека."""
    iv = bytes([0, 1, 2, 3, 4, 5, 6, 7])
    buffer = bytearray()
    chunk_idx = 0

    with open(dest_path, "wb") as f:
        for chunk in stream_iterator:
            buffer.extend(chunk)
            while len(buffer) >= 2048:
                block = bytes(buffer[:2048])
                del buffer[:2048]

                if chunk_idx % 3 == 0:
                    cipher = Blowfish.new(bf_key, Blowfish.MODE_CBC, iv)
                    decrypted = cipher.decrypt(block)
                    f.write(decrypted)
                else:
                    f.write(block)
                chunk_idx += 1

        # Записываем остаток, который не шифруется
        if len(buffer) > 0:
            f.write(bytes(buffer))

def download_deezer_track(
    track_id: str,
    dest_dir: Path,
    target_quality: str = "FLAC",
    artist: str = "",
    title: str = ""
) -> Optional[Path]:
    """
    Скачивает трек из Deezer по ID.
    target_quality: 'FLAC', 'MP3_320' или 'MP3_128'.
    Возвращает путь к скачанному файлу или None.
    Никогда не маскирует MP3 под FLAC.
    """
    if target_quality == "MP3":
        target_quality = "MP3_320"
    elif target_quality not in QUALITIES:
        target_quality = "MP3_320"
        
    quality_info = QUALITIES[target_quality]

    try:
        # 1. Запрашиваем URL потока через прокси-сервер Echo
        payload = {
            "ids": [int(track_id)],
            "formats": [quality_info["proxyName"]]
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        
        resp = httpx.post(PROXY_API, json=payload, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"[!] Deezer: Прокси вернул код {resp.status_code}")
            return None
            
        data = resp.json()
        tracks_data = data.get("data")
        if not tracks_data or not tracks_data[0].get("media"):
            print(f"[!] Deezer: Трек {track_id} не найден или недоступен")
            return None
            
        media_list = tracks_data[0]["media"]
        # Ищем желаемый формат
        media = next((m for m in media_list if m.get("format") == quality_info["proxyName"]), None)
        
        # Если запрашивался FLAC, но его нет - НЕ сохраняем MP3 как .flac, а выходим для Soulseek
        if not media:
            available_formats = [m.get("format") for m in media_list]
            if target_quality == "FLAC":
                print(f"[!] Deezer: FLAC недоступен (сервер вернул только: {available_formats}). Переход на Soulseek...")
                return None
            # Для MP3 режима берём лучший доступный
            media = media_list[0]
            
        if not media.get("sources"):
            print(f"[!] Deezer: Отсутствуют источники для воспроизведения")
            return None
            
        stream_url = media["sources"][0]["url"]
        actual_format = media.get("format", "")
        actual_ext = "flac" if actual_format == "FLAC" else "mp3"
        
        # Получаем метаданные артиста и названия для имени файла
        artist_name = artist
        track_title = title
        if not artist_name or not track_title:
            try:
                meta_url = f"https://api.deezer.com/track/{track_id}"
                meta_resp = httpx.get(meta_url, timeout=3.0)
                if meta_resp.status_code == 200:
                    meta = meta_resp.json()
                    artist_name = artist_name or meta.get("artist", {}).get("name")
                    track_title = track_title or meta.get("title")
            except Exception:
                pass
                
        artist_name = artist_name or "Unknown Artist"
        track_title = track_title or f"Track_{track_id}"
        
        # Очищаем имя файла от недопустимых символов
        clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist_name} - {track_title}")
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        output_filename = f"{clean_name}.{actual_ext}"
        output_path = dest_dir / output_filename

        # 2. Скачиваем зашифрованный поток и дешифруем его на лету
        print(f"[*] Deezer: скачивание {actual_format}...")
        bf_key = get_blowfish_key(track_id)
        
        with httpx.stream("GET", stream_url, headers=headers, timeout=30) as r:
            r.raise_for_status()
            decrypt_and_save(r.iter_bytes(chunk_size=4096), bf_key, output_path)
            
        print(f"[+] Deezer: скачан {output_path.name}")
        return output_path

    except Exception as e:
        print(f"[!] Deezer: Ошибка скачивания трека {track_id}: {e}")
    return None

def search_deezer_track(artist: str, title: str) -> Optional[str]:
    """
    Ищет трек по артисту и названию на Deezer с валидацией версий (без нежелательных ремиксов).
    Возвращает track_id наиболее подходящего совпадения.
    """
    from sources.youtube_matcher import toks, _version_markers
    query = f"{artist} {title}"
    try:
        r = httpx.get("https://api.deezer.com/search", params={"q": query, "limit": 10}, timeout=4.0)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if not data:
                return None
            
            target_query = f"{artist} {title}"
            target_toks = toks(target_query)
            target_vm = _version_markers(target_query)
            
            best_id = None
            best_score = -1.0
            
            for track in data:
                track_title = track.get("title", "")
                track_artist = track.get("artist", {}).get("name", "")
                cand_full = f"{track_artist} {track_title}"
                c_toks = toks(cand_full)
                
                if not target_toks:
                    continue
                    
                overlap = len(target_toks & c_toks) / len(target_toks)
                c_vm = _version_markers(track_title)
                extra_vm = c_vm - target_vm
                
                score = overlap
                CRITICAL_MARKERS = {"remix", "cover", "karaoke", "instrumental", "tribute", "parody", "live"}
                if extra_vm & CRITICAL_MARKERS:
                    score -= 0.6 * len(extra_vm & CRITICAL_MARKERS)
                elif extra_vm:
                    score -= 0.3 * len(extra_vm)
                    
                extra_words = len(c_toks - target_toks)
                score -= 0.02 * min(extra_words, 10)
                
                if score > best_score:
                    best_score = score
                    best_id = str(track.get("id"))
                    
            if best_id and best_score >= 0.5:
                return best_id
    except Exception as e:
        print(f"[!] Deezer: Ошибка поиска трека: {e}")
    return None
