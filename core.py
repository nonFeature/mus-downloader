import re
from pathlib import Path
from typing import Optional

import config
import metadata
import tagger
from sources import (
    download_deezer_track,
    download_monochrome_track,
    search_soulseek,
    download_soulseek_track,
    search_yandex_track,
    download_yandex_track,
    search_zvuk_track,
    download_zvuk_track,
    download_fallback_track
)

def download_track_by_link(url: str, target_quality: str = "FLAC") -> Optional[Path]:
    """
    Основная логика скачивания трека по ссылке:
    1. Получение метаданных (через song.link и MusicBrainz).
    2. Опрос источников по приоритету.
    3. Скачивание и запись тегов.
    """
    # 1. Извлекаем метаданные
    meta = metadata.get_track_metadata(url)
    if not meta:
        print("[!] Не удалось извлечь метаданные для трека.")
        return None
        
    artist = meta.get("artist") or "Unknown Artist"
    title = meta.get("title") or "Unknown Track"
    isrc = meta.get("isrc")
    
    print(f"\n[*] Найдена информация о треке:")
    print(f"    Исполнитель : {artist}")
    print(f"    Название    : {title}")
    print(f"    Альбом      : {meta.get('album', 'N/A')}")
    print(f"    Год         : {meta.get('year', 'N/A')}")
    print(f"    ISRC        : {isrc or 'N/A'}")
    print(f"    Номер трека : {meta.get('track_number', 'N/A')}/{meta.get('track_total', 'N/A')}")
    
    file_path = None
    source_used = None
    
    # 2. Пытаемся скачать из различных источников по цепочке
    
    # --- Источник 1: Monochrome (Qobuz / Tidal / Amazon) ---
    if isrc or meta.get("tidal_id"):
        print("\n[*] Попытка скачивания через Monochrome...")
        file_path = download_monochrome_track(
            isrc=isrc,
            artist_hint=artist,
            title_hint=title,
            dest_dir=config.DOWNLOAD_DIR,
            target_quality=target_quality,
            tidal_id=meta.get("tidal_id")
        )
        if file_path:
            source_used = "Monochrome"
            
    # --- Источник 2: Deezer ---
    if not file_path and meta.get("deezer_id"):
        print("\n[*] Попытка скачивания через Deezer (Echo Proxy)...")
        file_path = download_deezer_track(meta["deezer_id"], config.DOWNLOAD_DIR, target_quality)
        if file_path:
            source_used = "Deezer"
            
    # --- Источник 3: Сбер Звук ---
    if not file_path and config.ZVUK_TOKEN:
        print("\n[*] Попытка поиска и скачивания на Сбер Звуке...")
        zvuk_id = search_zvuk_track(artist, title)
        if zvuk_id:
            file_path = download_zvuk_track(zvuk_id, config.DOWNLOAD_DIR, target_quality)
            if file_path:
                source_used = "Zvuk"
                
    # --- Источник 4: Яндекс Музыка ---
    if not file_path and config.YANDEX_TOKEN:
        print("\n[*] Попытка поиска и скачивания на Яндекс Музыке...")
        yandex_id = meta.get("yandex_id") or search_yandex_track(artist, title)
        if yandex_id:
            file_path = download_yandex_track(yandex_id, config.DOWNLOAD_DIR, target_quality)
            if file_path:
                source_used = "Yandex"
                
    # --- Источник 5: Soulseek (через slskd) ---
    if not file_path and config.SLSKD_URL:
        print("\n[*] Попытка поиска и скачивания на Soulseek...")
        candidates = search_soulseek(artist, title, limit=3)
        if candidates:
            # Берем лучшего кандидата
            best = candidates[0]
            file_path = download_soulseek_track(
                best["slskd_username"],
                best["slskd_filename"],
                best["slskd_size"],
                config.DOWNLOAD_DIR
            )
            if file_path:
                source_used = f"Soulseek ({best['quality']})"
                
    # --- Источник 6: Фолбек (YouTube Music / SoundCloud / Входной URL) ---
    if not file_path:
        print("\n[*] Все FLAC-источники недоступны. Запуск фолбека (YouTube/SoundCloud)...")
        fallback_url = meta.get("youtube_music_url") or meta.get("soundcloud_url") or url
        file_path = download_fallback_track(fallback_url, config.DOWNLOAD_DIR)
        if file_path:
            source_used = "Fallback (yt-dlp)"
            
    # 3. Если скачивание успешно, вшиваем теги
    if file_path and file_path.exists():
        print(f"\n[*] Успешно скачан файл с источника: {source_used}")
        print(f"[*] Запуск теггирования файла...")
        
        # Получаем качество источника
        quality_label = "LOSSLESS" if file_path.suffix.lower() == ".flac" else "320kbps"
        
        tagger.apply_metadata(
            file_path=file_path,
            artist=artist,
            title=title,
            album=meta.get("album", ""),
            year=meta.get("year"),
            track_number=meta.get("track_number"),
            track_total=meta.get("track_total"),
            album_art_url=meta.get("album_art"),
            album_artist=meta.get("album_artist"),
            source=source_used,
            source_quality=quality_label
        )
        return file_path
    else:
        print(f"\n[!] Не удалось скачать трек ни с одного из доступных источников.")
        return None
