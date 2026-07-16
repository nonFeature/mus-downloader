import httpx
import re
from pathlib import Path
from typing import Optional
from yandex_music import Client
from config import YANDEX_TOKEN

def get_yandex_client() -> Optional[Client]:
    """Инициализирует и возвращает клиент Яндекс.Музыки."""
    if not YANDEX_TOKEN:
        return None
    try:
        client = Client(YANDEX_TOKEN).init()
        return client
    except Exception as e:
        print(f"[!] Yandex: Ошибка инициализации клиента: {e}")
    return None

def download_yandex_track(track_id: str, dest_dir: Path, target_quality: str = "FLAC") -> Optional[Path]:
    """
    Скачивает трек из Яндекс.Музыки по ID.
    """
    client = get_yandex_client()
    if not client:
        print("[!] Yandex: Токен отсутствует или невалиден")
        return None
        
    print(f"[*] Yandex: Запрос трека {track_id}...")
    
    try:
        tracks = client.tracks(track_id)
        if not tracks:
            print(f"[!] Yandex: Трек {track_id} не найден")
            return None
            
        track = tracks[0]
        artist_name = ", ".join(a.name for a in track.artists)
        track_title = track.title
        
        # Получаем доступные ссылки на скачивание
        download_info = track.get_download_info()
        if not download_info:
            print("[!] Yandex: Не удалось получить ссылки на скачивание")
            return None
            
        # Сортируем по качеству
        # 1. Сначала FLAC
        # 2. Затем MP3 с максимальным битрейтом
        best_info = None
        ext = "mp3"
        
        # Фильтруем FLAC при выборе FLAC
        if target_quality == "FLAC":
            flac_infos = [info for info in download_info if info.codec == "flac"]
            if flac_infos:
                best_info = flac_infos[0]
                ext = "flac"
                print("[*] Yandex: Доступен формат FLAC")
                
        # Если FLAC не найден или не затребован, ищем MP3 320
        if not best_info:
            mp3_infos = [info for info in download_info if info.codec == "mp3"]
            if mp3_infos:
                # Сортируем MP3 по битрейту по убыванию
                mp3_infos.sort(key=lambda x: x.bitrate_in_kbps, reverse=True)
                best_info = mp3_infos[0]
                ext = "mp3"
                print(f"[*] Yandex: Доступен формат MP3 {best_info.bitrate_in_kbps}kbps")
                
        if not best_info:
            best_info = download_info[0]
            ext = best_info.codec
            
        # Получаем прямую ссылку
        direct_link = best_info.get_direct_link()
        clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist_name} - {track_title}")
        output_path = dest_dir / f"{clean_name}.{ext}"
        
        print(f"[*] Yandex: Скачивание трека в {output_path}...")
        
        with httpx.stream("GET", direct_link, timeout=30) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    
        print(f"[+] Yandex: Скачивание завершено: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"[!] Yandex: Ошибка при скачивании трека {track_id}: {e}")
        
    return None

def search_yandex_track(artist: str, title: str) -> Optional[str]:
    """
    Ищет трек по названию и артисту в каталоге Яндекс Музыки.
    Возвращает track_id первого совпадения.
    """
    client = get_yandex_client()
    if not client:
        return None
        
    query = f"{artist} - {title}"
    try:
        search_result = client.search(query, type_='track')
        if search_result and search_result.tracks and search_result.tracks.results:
            # Берем первое совпадение
            track = search_result.tracks.results[0]
            return str(track.id)
    except Exception as e:
        print(f"[!] Yandex: Ошибка поиска трека: {e}")
    return None
