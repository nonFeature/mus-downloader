import httpx
import re
from pathlib import Path
from typing import Optional
from zvuk_music import Client, Quality, ZvukMusicError
from config import ZVUK_TOKEN

def get_zvuk_client() -> Optional[Client]:
    """Инициализирует и возвращает клиент Звука."""
    if not ZVUK_TOKEN:
        return None
    try:
        # Для авторизации передаем токен
        client = Client(token=ZVUK_TOKEN)
        return client
    except Exception as e:
        print(f"[!] Zvuk: Ошибка инициализации клиента: {e}")
    return None

def search_zvuk_track(artist: str, title: str) -> Optional[str]:
    """
    Ищет трек по артисту и названию на Звуке.
    Возвращает track_id первого совпадения.
    """
    client = get_zvuk_client()
    if not client:
        return None
        
    query = f"{artist} - {title}"
    try:
        search_result = client.search(query, limit=10, tracks=True)
        if search_result and search_result.tracks and search_result.tracks.items:
            # Сверяем исполнителя и название
            for track in search_result.tracks.items:
                track_artists = ", ".join(a.title.lower() for a in track.artists)
                track_title = track.title.lower()
                
                # Небольшая проверка на совпадение названия
                if artist.lower() in track_artists and title.lower() in track_title:
                    return str(track.id)
                    
            # Если точного совпадения не нашли, берем первый попавшийся
            return str(search_result.tracks.items[0].id)
    except Exception as e:
        print(f"[!] Zvuk: Ошибка при поиске трека: {e}")
    return None

def download_zvuk_track(track_id: str, dest_dir: Path, target_quality: str = "FLAC") -> Optional[Path]:
    """
    Скачивает трек из Звука по его ID.
    """
    client = get_zvuk_client()
    if not client:
        print("[!] Zvuk: Токен отсутствует")
        return None
        
    print(f"[*] Zvuk: Запрос трека {track_id}...")
    
    try:
        # Получаем объект трека для метаданных
        track = client.get_track(int(track_id))
        artist_name = ", ".join(a.title for a in track.artists)
        track_title = track.title
        
        stream_url = None
        ext = "mp3"
        
        # 1. Попытка получить FLAC поток
        if target_quality == "FLAC":
            try:
                print("[*] Zvuk: Попытка получить FLAC-поток...")
                stream_url = client.get_stream_url(track.id, quality=Quality.FLAC)
                ext = "flac"
            except Exception as e:
                print(f"[*] Zvuk: FLAC недоступен ({e}), пробуем HIGH качество...")
                
        # 2. Попытка получить HIGH качество (MP3 320kbps)
        if not stream_url:
            try:
                stream_url = client.get_stream_url(track.id, quality=Quality.HIGH)
                ext = "mp3"
            except Exception as e:
                print(f"[*] Zvuk: HIGH недоступен ({e}), пробуем MID качество...")
                
        # 3. Попытка получить MID качество (MP3 128kbps)
        if not stream_url:
            stream_url = client.get_stream_url(track.id, quality=Quality.MID)
            ext = "mp3"

        clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist_name} - {track_title}")
        output_path = dest_dir / f"{clean_name}.{ext}"
        
        print(f"[*] Zvuk: Скачивание трека в {output_path}...")
        
        # Скачиваем поток
        headers = {"User-Agent": "Mozilla/5.0"}
        with httpx.stream("GET", stream_url, headers=headers, timeout=30) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    
        print(f"[+] Zvuk: Скачивание завершено: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"[!] Zvuk: Ошибка при скачивании трека {track_id}: {e}")
        
    return None
