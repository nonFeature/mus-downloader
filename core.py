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
    download_fallback_track,
    download_soundcloud_track
)

def download_track_by_link(url_or_query: str, target_quality: str = "MP3") -> Optional[Path]:
    """
    Основная логика скачивания трека:
    - Для FLAC: честный поиск Lossless (Soulseek -> Deezer).
    - Для MP3: Deezer (320kbps CBR) -> Soulseek (slsk, если настроен) -> YouTube Music (CSVMusic matcher ~250-280k VBR) -> yt-dlp fallback.
    - При наличии прямых ссылок (SoundCloud, YouTube) скачивает напрямую из указанного источника.
    """
    # 1. Извлекаем метаданные
    is_url = url_or_query.startswith("http://") or url_or_query.startswith("https://")
    is_soundcloud = is_url and ("soundcloud.com" in url_or_query.lower() or "on.soundcloud.com" in url_or_query.lower())
    is_youtube = is_url and ("youtube.com" in url_or_query.lower() or "youtu.be" in url_or_query.lower())
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
    
    explicit = meta.get("explicit")
    
    exp_str = " [E]" if explicit else ""
    year_str = f" ({meta.get('year')})" if meta.get('year') else ""
    dur_str = f" [{round(duration)}s]" if duration else ""
    print(f"\n[*] Трек: {artist} - {title}{exp_str}{year_str}{dur_str} [{target_quality}]")
    
    file_path: Optional[Path] = None
    source_used: Optional[str] = None
    fallback_sc_file: Optional[Path] = None
    
    deezer_id = meta.get("deezer_id")
    if not deezer_id and artist and title:
        deezer_id = search_deezer_track(artist, title)

    # 2. Логика для FLAC (строгий поиск lossless: Soulseek -> Deezer)
    if target_quality == "FLAC":
        # Если дана прямая ссылка SoundCloud, проверяем наличие Lossless-оригинала (FLAC/WAV)
        if is_soundcloud:
            print("[*] SoundCloud: проверка Lossless оригинала...")
            sc_target = meta.get("soundcloud_url") or url_or_query
            sc_candidate = download_soundcloud_track(
                url=sc_target,
                dest_dir=config.DOWNLOAD_DIR,
                artist=artist,
                title=title,
                duration=duration,
                album=album
            )
            if sc_candidate:
                if sc_candidate.suffix.lower() in [".flac", ".wav"]:
                    file_path = sc_candidate
                    source_used = "SoundCloud Lossless"
                else:
                    fallback_sc_file = sc_candidate
        
        # Шаг FLAC-1: Soulseek (только FLAC, в первую очередь)
        if not file_path and (config.SLSK_USER or config.SLSKD_URL):
            print("[*] Soulseek: поиск FLAC...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="FLAC", duration=duration)
            for idx, cand in enumerate(candidates, 1):
                if idx > 1:
                    print(f"[*] Soulseek: резервный пир #{idx} ({cand['slskd_username']})...")
                file_path = download_soulseek_track(
                    cand["slskd_username"],
                    cand["slskd_filename"],
                    cand["slskd_size"],
                    config.DOWNLOAD_DIR,
                    target_quality="FLAC"
                )
                if file_path:
                    source_used = f"Soulseek ({cand['quality']})"
                    break
            if not file_path and candidates:
                print(f"[!] Soulseek: все {len(candidates)} кандидатов недоступны")

        # Шаг FLAC-2: Deezer (только если реально отдается FLAC)
        if not file_path and deezer_id:
            file_path = download_deezer_track(deezer_id, config.DOWNLOAD_DIR, target_quality="FLAC", artist=artist, title=title)
            if file_path and file_path.suffix.lower() == ".flac":
                source_used = "Deezer FLAC"

        if not file_path:
            print("[!] FLAC не найден -> переключение на MP3...")

    # 3. Логика для MP3 / стандартных стримингов (основной режим или откат с FLAC)
    if not file_path:
        # Если прямая ссылка SoundCloud: скачиваем оригинал без раздувания битрейта
        if is_soundcloud:
            if fallback_sc_file and fallback_sc_file.exists():
                file_path = fallback_sc_file
                source_used = "SoundCloud (Original)"
            else:
                print("\n[*] Прямая ссылка на SoundCloud: скачивание оригинала без раздувания...")
                sc_target = meta.get("soundcloud_url") or url_or_query
                file_path = download_soundcloud_track(
                    url=sc_target,
                    dest_dir=config.DOWNLOAD_DIR,
                    artist=artist,
                    title=title,
                    duration=duration,
                    album=album
                )
                if file_path:
                    source_used = "SoundCloud (Original)"

        # 1. Если передана прямая ссылка на YouTube: скачиваем напрямую
        if not file_path and is_youtube:
            print("\n[*] Прямая ссылка на YouTube: скачивание трека...")
            file_path = download_youtube_track(
                artist=artist,
                title=title,
                dest_dir=config.DOWNLOAD_DIR,
                duration=duration,
                album=album,
                isrc=isrc,
                direct_url=url_or_query,
                explicit=explicit
            )
            if file_path:
                source_used = "YouTube Music"

        # Шаг MP3-1: Deezer MP3 320 (честный студийный 320k CBR)
        if not file_path and deezer_id:
            file_path = download_deezer_track(deezer_id, config.DOWNLOAD_DIR, target_quality="MP3_320", artist=artist, title=title)
            if file_path:
                source_used = "Deezer (MP3 320)"

        # Шаг MP3-2: Soulseek (честный 320k CBR / FLAC)
        if not file_path and (config.SLSK_USER or config.SLSKD_URL):
            print("[*] Soulseek: поиск MP3...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="MP3", duration=duration)
            for idx, cand in enumerate(candidates, 1):
                if idx > 1:
                    print(f"[*] Soulseek: резервный пир #{idx} ({cand['slskd_username']})...")
                file_path = download_soulseek_track(
                    cand["slskd_username"],
                    cand["slskd_filename"],
                    cand["slskd_size"],
                    config.DOWNLOAD_DIR,
                    target_quality="MP3"
                )
                if file_path:
                    source_used = f"Soulseek ({cand['quality']})"
                    break
            if not file_path and candidates:
                print(f"[!] Soulseek: все {len(candidates)} кандидатов недоступны")

        # Шаг MP3-3: YouTube Music с умным поиском CSVMusic (длительность + токенизация + фильтры каверов + Explicit)
        if not file_path:
            print("[*] YouTube Music: поиск и скачивание...")
            direct_yt = meta.get("youtube_music_url")

            file_path = download_youtube_track(
                artist=artist,
                title=title,
                dest_dir=config.DOWNLOAD_DIR,
                duration=duration,
                album=album,
                isrc=isrc,
                direct_url=direct_yt,
                explicit=explicit
            )
            if file_path:
                source_used = "YouTube Music"

        # Шаг MP3-4: Финальный фолбек
        if not file_path:
            print("[*] yt-dlp: фолбек-скачивание...")
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
                if fallback_target and ("soundcloud.com" in fallback_target.lower() or "on.soundcloud.com" in fallback_target.lower()):
                    source_used = "SoundCloud (Original)"
                else:
                    source_used = "Fallback (yt-dlp)"

    # Очищаем неиспользованный резервный файл SoundCloud (если скачался лучший источник)
    if fallback_sc_file and fallback_sc_file.exists() and fallback_sc_file != file_path:
        try:
            fallback_sc_file.unlink()
        except Exception:
            pass

    # 4. Если скачивание успешно, вшиваем метаданные и обложку
    if file_path and file_path.exists():
        print(f"[*] Тегирование: {file_path.name} ({source_used})...")
        
        # Определяем метку качества по фактическому файлу
        suffix = file_path.suffix.lower()
        if suffix == ".flac":
            quality_label = "LOSSLESS"
        elif suffix == ".mp3":
            try:
                from mutagen.mp3 import MP3
                mp3_info = MP3(str(file_path)).info
                if mp3_info and mp3_info.bitrate:
                    quality_label = f"{round(mp3_info.bitrate / 1000)}kbps"
                else:
                    quality_label = "MP3"
            except Exception:
                quality_label = "MP3"
        elif suffix in [".m4a", ".mp4"]:
            try:
                from mutagen.mp4 import MP4
                mp4_info = MP4(str(file_path)).info
                if mp4_info and mp4_info.bitrate:
                    quality_label = f"{round(mp4_info.bitrate / 1000)}kbps"
                else:
                    quality_label = "AAC"
            except Exception:
                quality_label = "AAC"
        elif suffix in [".opus", ".ogg"]:
            try:
                from mutagen.oggopus import OggOpus
                opus_info = OggOpus(str(file_path)).info
                if opus_info and opus_info.bitrate:
                    quality_label = f"{round(opus_info.bitrate / 1000)}kbps"
                else:
                    quality_label = "OPUS"
            except Exception:
                quality_label = "OPUS"
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
        print("[-] Не удалось скачать трек ни с одного источника.")
        return None
