import re
from pathlib import Path
from typing import Optional

import config
import metadata
import tagger
from sources.doh_resolver import setup_doh_fallback

# Включаем автоматический DoH-фолбек при сбоях локального DNS
setup_doh_fallback()
from sources import (
    download_deezer_track,
    search_deezer_track,
    search_soulseek,
    download_soulseek_track,
    download_youtube_track,
    download_fallback_track
)

def download_track_by_link(url_or_query: str, target_quality: str = "MP3") -> Optional[Path]:
    """
    Основная логика скачивания трека:
    - По умолчанию качает в честный MP3 320kbps (LAME CBR) через YouTube Music (CSVMusic matcher).
    - Если на YouTube трек не найден или не прошёл фильтры — фолбек на Soulseek / Deezer.
    - При запросе FLAC опрашивает только реальные FLAC-источники: Deezer и Soulseek.
    """
    # 1. Извлекаем метаданные
    is_url = url_or_query.startswith("http://") or url_or_query.startswith("https://")
    if is_url:
        meta = metadata.get_track_metadata(url_or_query)
    else:
        meta = metadata.resolve_query_metadata(url_or_query)
        
    if not meta:
        print("[!] Не удалось найти или извлечь метаданные для трека.")
        return None
        
    artist = meta.get("artist") or "Unknown Artist"
    title = meta.get("title") or "Unknown Track"
    isrc = meta.get("isrc")
    duration = meta.get("duration")
    album = meta.get("album")
    
    print(f"\n[*] Найдена информация о треке:")
    print(f"    Исполнитель : {artist}")
    print(f"    Название    : {title}")
    print(f"    Альбом      : {album or 'N/A'}")
    print(f"    Год         : {meta.get('year', 'N/A')}")
    print(f"    ISRC        : {isrc or 'N/A'}")
    print(f"    Длительность: {round(duration, 1) if duration else 'N/A'} сек")
    print(f"    Номер трека : {meta.get('track_number', 'N/A')}/{meta.get('track_total', 'N/A')}")
    print(f"    Целевое качество: {target_quality}")
    
    file_path: Optional[Path] = None
    source_used: Optional[str] = None
    
    deezer_id = meta.get("deezer_id")
    if not deezer_id and artist and title:
        deezer_id = search_deezer_track(artist, title)

    # 2. Логика для FLAC (строгий поиск lossless только в Deezer и Soulseek)
    if target_quality == "FLAC":
        print("\n[*] Режим FLAC: поиск честного Lossless (Deezer / Soulseek)...")
        
        # Шаг FLAC-1: Deezer (только если реально отдается FLAC)
        if deezer_id:
            print(f"[*] Проверка наличия FLAC в Deezer (ID: {deezer_id})...")
            file_path = download_deezer_track(deezer_id, config.DOWNLOAD_DIR, target_quality="FLAC", artist=artist, title=title)
            if file_path and file_path.suffix.lower() == ".flac":
                source_used = "Deezer FLAC"
                
        # Шаг FLAC-2: Soulseek (только FLAC)
        if not file_path and config.SLSKD_URL:
            print("[*] Поиск FLAC на Soulseek...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="FLAC")
            if candidates:
                best = candidates[0]
                file_path = download_soulseek_track(
                    best["slskd_username"],
                    best["slskd_filename"],
                    best["slskd_size"],
                    config.DOWNLOAD_DIR
                )
                if file_path:
                    source_used = f"Soulseek ({best['quality']})"
                    
        if not file_path:
            print("\n[!] Честный FLAC не найден ни в Deezer, ни в Soulseek.")
            print("[*] Мягкое переключение на загрузку MP3 320kbps...")

    # 3. Логика для MP3 320kbps (основной режим или откат с FLAC)
    if not file_path:
        # Шаг MP3-1: YouTube Music с умным поиском CSVMusic (длительность + токенизация + фильтры каверов)
        print("\n[*] Попытка поиска и скачивания с YouTube Music (CSVMusic matcher)...")
        direct_yt = meta.get("youtube_music_url") or meta.get("soundcloud_url") or (url_or_query if is_url else None)
        file_path = download_youtube_track(
            artist=artist,
            title=title,
            dest_dir=config.DOWNLOAD_DIR,
            duration=duration,
            album=album,
            isrc=isrc,
            direct_url=direct_yt
        )
        if file_path:
            source_used = "YouTube Music (MP3 320)"

        # Шаг MP3-2: Deezer MP3 320
        if not file_path and deezer_id:
            print(f"\n[*] Попытка скачивания MP3 320 с Deezer...")
            file_path = download_deezer_track(deezer_id, config.DOWNLOAD_DIR, target_quality="MP3_320", artist=artist, title=title)
            if file_path:
                source_used = "Deezer (MP3 320)"

        # Шаг MP3-3: Soulseek (slsk), если настроен slskd
        if not file_path and config.SLSKD_URL:
            print("\n[*] Поиск MP3 на Soulseek...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="MP3")
            if candidates:
                best = candidates[0]
                file_path = download_soulseek_track(
                    best["slskd_username"],
                    best["slskd_filename"],
                    best["slskd_size"],
                    config.DOWNLOAD_DIR
                )
                if file_path:
                    source_used = f"Soulseek ({best['quality']})"

        # Шаг MP3-4: Финальный фолбек
        if not file_path:
            print("\n[*] Запуск финального прямого фолбека через yt-dlp...")
            fallback_target = meta.get("youtube_music_url") or meta.get("soundcloud_url") or url_or_query
            file_path = download_fallback_track(
                url=fallback_target,
                dest_dir=config.DOWNLOAD_DIR,
                artist=artist,
                title=title,
                duration=duration,
                album=album,
                isrc=isrc
            )
            if file_path:
                source_used = "Fallback (yt-dlp)"

    # 4. Если скачивание успешно, вшиваем метаданные и обложку
    if file_path and file_path.exists():
        print(f"\n[+] Успешно скачан файл с источника: {source_used}")
        print(f"[*] Запись тегов и обложки в {file_path.name}...")
        
        # Определяем метку качества по фактическому файлу
        suffix = file_path.suffix.lower()
        if suffix == ".flac":
            quality_label = "LOSSLESS"
        elif suffix == ".mp3":
            quality_label = "320kbps"
        else:
            quality_label = suffix.lstrip(".").upper()
            
        tagger.apply_metadata(
            file_path=file_path,
            artist=artist,
            title=title,
            album=album or "",
            year=meta.get("year"),
            track_number=meta.get("track_number"),
            track_total=meta.get("track_total"),
            album_art_url=meta.get("album_art"),
            album_artist=meta.get("album_artist"),
            source=source_used,
            source_quality=quality_label,
            genre=meta.get("genre")
        )
        return file_path
    else:
        print(f"\n[-] Не удалось скачать трек ни с одного из доступных источников.")
        return None
