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

def download_deezer_track(track_id: str, dest_dir: Path, target_quality: str = "FLAC") -> Optional[Path]:
    """
    Скачивает трек из Deezer по ID.
    target_quality: 'FLAC', 'MP3_320' или 'MP3_128'.
    Возвращает путь к скачанному файлу или None.
    """
    if target_quality == "MP3":
        target_quality = "MP3_320"
    elif target_quality not in QUALITIES:
        target_quality = "FLAC"
        
    quality_info = QUALITIES[target_quality]
    print(f"[*] Deezer: Запрос трека {track_id} ({target_quality})")

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
        # Ищем желаемый формат, если его нет — берем первый попавшийся
        media = next((m for m in media_list if m.get("format") == quality_info["proxyName"]), media_list[0])
        
        if not media.get("sources"):
            print(f"[!] Deezer: Отсутствуют источники для воспроизведения")
            return None
            
        stream_url = media["sources"][0]["url"]
        
        # Получаем метаданные артиста и названия для имени файла
        meta_url = f"https://api.deezer.com/track/{track_id}"
        meta_resp = httpx.get(meta_url, timeout=10)
        meta = meta_resp.json() if meta_resp.status_code == 200 else {}
        
        artist_name = meta.get("artist", {}).get("name", "Unknown Artist")
        track_title = meta.get("title", "Unknown Track")
        
        # Очищаем имя файла от недопустимых символов
        clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist_name} - {track_title}")
        output_filename = f"{clean_name}.{quality_info['ext']}"
        output_path = dest_dir / output_filename

        # 2. Скачиваем зашифрованный поток и дешифруем его на лету
        print(f"[*] Deezer: Скачивание и расшифровка потока...")
        bf_key = get_blowfish_key(track_id)
        
        with httpx.stream("GET", stream_url, headers=headers, timeout=30) as r:
            r.raise_for_status()
            decrypt_and_save(r.iter_bytes(chunk_size=4096), bf_key, output_path)
            
        print(f"[+] Deezer: Скачивание завершено: {output_path}")
        return output_path

    except Exception as e:
        print(f"[!] Deezer: Ошибка скачивания трека {track_id}: {e}")
    return None
