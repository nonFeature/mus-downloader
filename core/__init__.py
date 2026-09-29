import re
import sys
import time
from pathlib import Path
from typing import Optional, Callable

import config

# Ядро скачивателя: metadata/tagger/sources живут внутри пакета core.
from . import metadata, tagger, coverart
from . import cache as cache_mod
from . import filecache
from . import sources as _sources
from .sources.doh_resolver import setup_doh_fallback

# Включаем автоматический DoH-фолбек при сбоях локального DNS
setup_doh_fallback()
from .sources import (
    download_deezer_track,
    search_deezer_track,
    search_soulseek,
    download_soulseek_track,
    download_youtube_track,
    download_fallback_track,
    download_soundcloud_track,
    deezer,
    soulseek,
    soundcloud,
    youtube,
    youtube_matcher,
    fallback,
    doh_resolver,
)


def _install_legacy_aliases() -> None:
    """
    Совместимость со старыми путями импорта (``metadata``, ``tagger``,
    ``sources.*``): регистрируем те же объекты модулей в sys.modules,
    чтобы патчи и импорты из тестов продолжали указывать на те же модули.
    """
    for name, module in {
        "metadata": metadata,
        "tagger": tagger,
        "sources": _sources,
        "sources.deezer": deezer,
        "sources.soulseek": soulseek,
        "sources.soundcloud": soundcloud,
        "sources.youtube": youtube,
        "sources.youtube_matcher": youtube_matcher,
        "sources.fallback": fallback,
        "sources.doh_resolver": doh_resolver,
    }.items():
        sys.modules.setdefault(name, module)


_install_legacy_aliases()


def search_tracks(query: str, limit: int = 8) -> list[dict]:
    """
    Агрегированный поиск треков по Deezer, Apple Music и YouTube Music.
    """
    return metadata.search_multisource_tracks(query, limit=limit)


def download_track_by_link(
    url_or_query: str,
    target_quality: str = "MP3",
    dest_dir: Optional[Path] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    track_meta: Optional[dict] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    reuse_cached_file: Optional[bool] = None,
) -> Optional[Path]:
    """
    Основная логика скачивания трека:
    - Для FLAC: честный поиск Lossless (Soulseek -> Deezer).
    - Для MP3: Deezer (320kbps CBR) -> Soulseek (slsk, если настроен) -> YouTube Music (CSVMusic matcher ~250-280k VBR) -> yt-dlp fallback.
    - При наличии прямых ссылок (SoundCloud, YouTube, VK) скачивает напрямую из указанного источника.
    - ``reuse_cached_file`` — переиспользовать ранее скачанный файл из
      локального кэша вместо повторной загрузки. Бот его выключает: он всё
      равно удаляет файл сразу после отправки.
    """
    target_dir = Path(dest_dir) if dest_dir else config.DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    def report(msg: str):
        if not progress_callback:
            try:
                print(msg)
            except UnicodeEncodeError:
                try:
                    encoding = sys.stdout.encoding or "utf-8"
                    print(msg.encode(encoding, errors="replace").decode(encoding))
                except Exception:
                    pass
        if status_callback:
            try:
                status_callback(msg)
            except Exception:
                pass
        if progress_callback:
            try:
                progress_callback({"stage": "status", "description": msg})
            except Exception:
                pass

    url_or_query = (url_or_query or "").strip().strip("'\"")
    if not url_or_query and not track_meta:
        print("[!] Ссылка или запрос не могут быть пустыми.")
        return None

    if progress_callback:
        progress_callback({
            "stage": "search",
            "description": "Поиск метаданных трека...",
            "percent": 10,
        })

    # 1. Извлекаем метаданные
    is_url = url_or_query.startswith("http://") or url_or_query.startswith("https://")
    is_soundcloud = is_url and ("soundcloud.com" in url_or_query.lower() or "on.soundcloud.com" in url_or_query.lower())
    is_youtube = is_url and ("youtube.com" in url_or_query.lower() or "youtu.be" in url_or_query.lower())

    cache = cache_mod.get_cache()
    cache_url = url_or_query if is_url else ""
    cache_query = "" if is_url else url_or_query

    if track_meta:
        # Метаданные уже обогащены вызывающей стороной (например, результатом
        # поиска dl search) — кладём в кэш, чтобы повтор не ходил в API.
        cache.set_track(track_meta, query=cache_query, url=cache_url)
        meta = track_meta
    else:
        meta = cache.get_track(query=cache_query, url=cache_url)
        if meta:
            report(f"[*] Метаданные из кэша: {meta.get('artist')} - {meta.get('title')}")
        else:
            if is_url:
                meta = metadata.get_track_metadata(url_or_query)
            else:
                meta = metadata.resolve_query_metadata(url_or_query)
            if meta:
                cache.set_track(meta, query=cache_query, url=cache_url)
        
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
    report(f"\n[*] Трек: {artist} - {title}{exp_str}{year_str}{dur_str} [{target_quality}]")
    if progress_callback:
        progress_callback({
            "stage": "search",
            "description": f"Трек: {artist} - {title} [{target_quality}]",
            "percent": 25,
        })
    
    file_path: Optional[Path] = None
    source_used: Optional[str] = None
    fallback_sc_file: Optional[Path] = None

    # 1.5 Локальный кэш скачанных файлов: если трек уже лежит на диске,
    # повторно ходить по источникам не нужно.
    if reuse_cached_file is None:
        reuse_cached_file = True
    if reuse_cached_file:
        file_cache = filecache.get_file_cache()
        cached = file_cache.lookup(meta, target_quality, query=url_or_query if not is_url else "", url=url_or_query if is_url else "")
        if cached:
            report(f"[+] Найден в локальном кэше: {cached.name}")
            if progress_callback:
                progress_callback({
                    "stage": "done",
                    "description": f"Из кэша: {cached.name}",
                    "percent": 100,
                })
            return cached

    deezer_id = meta.get("deezer_id")
    if not deezer_id and artist and title:
        deezer_id = cache.get_deezer_id(artist, title, album or "")
        if not deezer_id:
            deezer_id = search_deezer_track(artist, title, duration=duration, album=album)
            if deezer_id:
                cache.set_deezer_id(artist, title, album or "", deezer_id)

    # 2. Логика для FLAC (строгий поиск lossless: Deezer FLAC -> Soulseek FLAC)
    if target_quality == "FLAC":
        # Шаг FLAC-1: Deezer (мгновенный студийный FLAC напрямую от лейбла)
        if not file_path and deezer_id:
            report("[*] Deezer: скачивание FLAC...")
            file_path = download_deezer_track(
                deezer_id,
                target_dir,
                target_quality="FLAC",
                artist=artist,
                title=title,
                progress_callback=progress_callback,
            )
            # Если исходный ID не сработал, пробуем найти альтернативный релиз на Deezer
            if not file_path and artist and title:
                alt_dz = search_deezer_track(artist, title, duration=duration, album=album)
                if alt_dz and alt_dz != deezer_id:
                    file_path = download_deezer_track(
                        alt_dz,
                        target_dir,
                        target_quality="FLAC",
                        artist=artist,
                        title=title,
                        progress_callback=progress_callback,
                    )
            if file_path and file_path.suffix.lower() == ".flac":
                source_used = "Deezer FLAC"

        # Шаг FLAC-2: Soulseek (P2P Lossless)
        if not file_path and (config.SLSK_USER or config.SLSKD_URL):
            report("[*] Soulseek: поиск FLAC...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="FLAC", duration=duration)
            for idx, cand in enumerate(candidates, 1):
                if idx > 1:
                    report(f"[*] Soulseek: подключение к резервному пиру #{idx} ({cand['slskd_username']})...")
                report(f"[*] Soulseek: скачивание FLAC ({cand['slskd_username']})...")
                file_path = download_soulseek_track(
                    cand["slskd_username"],
                    cand["slskd_filename"],
                    cand["slskd_size"],
                    target_dir,
                    target_quality="FLAC",
                    status_callback=report,
                    progress_callback=progress_callback,
                )
                if file_path and file_path.suffix.lower() == ".flac":
                    source_used = f"Soulseek ({cand['quality']})"
                    break
                if file_path:
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    file_path = None
            if not file_path and candidates:
                report(f"[!] Soulseek: все {len(candidates)} кандидатов недоступны")

        # Если дана прямая ссылка SoundCloud, проверяем наличие Lossless-оригинала (FLAC/WAV)
        if not file_path and is_soundcloud:
            report("[*] SoundCloud: проверка Lossless оригинала...")
            sc_target = meta.get("soundcloud_url") or url_or_query
            sc_candidate = download_soundcloud_track(
                url=sc_target,
                dest_dir=target_dir,
                artist=artist,
                title=title,
                duration=duration,
                album=album,
                progress_callback=progress_callback,
            )
            if sc_candidate:
                if sc_candidate.suffix.lower() in [".flac", ".wav"]:
                    file_path = sc_candidate
                    source_used = "SoundCloud Lossless"
                else:
                    fallback_sc_file = sc_candidate

        if not file_path:
            report("[!] FLAC не найден -> переключение на MP3...")

    # 3. Логика для MP3 / стандартных стримингов (основной режим или откат с FLAC)
    if not file_path:
        # Шаг MP3-1: Deezer MP3 320 (честный студийный 320k CBR)
        if not file_path and deezer_id:
            report("[*] Deezer: скачивание MP3 320...")
            file_path = download_deezer_track(
                deezer_id,
                target_dir,
                target_quality="MP3_320",
                artist=artist,
                title=title,
                progress_callback=progress_callback,
            )
            if not file_path and artist and title:
                alt_dz = search_deezer_track(artist, title, duration=duration, album=album)
                if alt_dz and alt_dz != deezer_id:
                    file_path = download_deezer_track(
                        alt_dz,
                        target_dir,
                        target_quality="MP3_320",
                        artist=artist,
                        title=title,
                        progress_callback=progress_callback,
                    )
            if file_path:
                source_used = "Deezer (MP3 320)"

        # Шаг MP3-2: Soulseek (честный 320k CBR / FLAC)
        if not file_path and (config.SLSK_USER or config.SLSKD_URL):
            report("[*] Soulseek: поиск MP3...")
            candidates = search_soulseek(artist, title, limit=3, target_quality="MP3", duration=duration)
            for idx, cand in enumerate(candidates, 1):
                if idx > 1:
                    report(f"[*] Soulseek: подключение к резервному пиру #{idx} ({cand['slskd_username']})...")
                report(f"[*] Soulseek: скачивание MP3 ({cand['slskd_username']})...")
                file_path = download_soulseek_track(
                    cand["slskd_username"],
                    cand["slskd_filename"],
                    cand["slskd_size"],
                    target_dir,
                    target_quality="MP3",
                    status_callback=report,
                    progress_callback=progress_callback,
                )
                if file_path:
                    source_used = f"Soulseek ({cand['quality']})"
                    break
            if not file_path and candidates:
                report(f"[!] Soulseek: все {len(candidates)} кандидатов недоступны")

        # Шаг MP3-3a: Если есть прямая ссылка на аудиопоток (например, VK Музыка CDN)
        direct_audio_url = meta.get("direct_url")
        if not file_path and direct_audio_url and not is_youtube and "youtube" not in direct_audio_url and not is_soundcloud and "soundcloud" not in direct_audio_url:
            report(f"[*] Прямое скачивание аудиопотока ({artist} - {title})...")
            try:
                import httpx
                from .sources.youtube import _clean_filename
                direct_file = target_dir / f"{_clean_filename(artist)} - {_clean_filename(title)}.mp3"
                with httpx.stream("GET", direct_audio_url, timeout=30.0, follow_redirects=True) as resp:
                    if resp.status_code == 200:
                        total_len = int(resp.headers.get("Content-Length", 0))
                        downloaded = 0
                        start_t = time.time()
                        with open(direct_file, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback:
                                    elapsed = time.time() - start_t
                                    speed = downloaded / elapsed if elapsed > 0 else 0
                                    eta = (total_len - downloaded) / speed if (speed > 0 and total_len > downloaded) else None
                                    progress_callback({
                                        "stage": "download",
                                        "source": "VK Музыка" if meta.get("vk_url") else "Direct Stream",
                                        "description": f"Скачивание аудиопотока ({artist} - {title})...",
                                        "downloaded_bytes": downloaded,
                                        "total_bytes": total_len,
                                        "speed": speed,
                                        "eta": eta,
                                    })
                        file_path = direct_file
                        source_used = "VK Музыка" if meta.get("vk_url") else "Direct Stream"
            except Exception as e:
                report(f"[!] Ошибка прямого скачивания потока: {e}")

        # Шаг MP3-3b: Если передана прямая ссылка YouTube — скачиваем по исходной ссылке
        # (если на Deezer и Soulseek не нашлось официального студийного релиза)
        if not file_path and (is_youtube or (direct_audio_url and ("youtube" in direct_audio_url or "youtu.be" in direct_audio_url))):
            yt_target = direct_audio_url if (direct_audio_url and ("youtube" in direct_audio_url or "youtu.be" in direct_audio_url)) else url_or_query
            report(f"[*] YouTube: скачивание по прямой ссылке ({artist} - {title})...")
            file_path = download_youtube_track(
                artist=artist,
                title=title,
                dest_dir=target_dir,
                duration=duration,
                album=album,
                isrc=isrc,
                direct_url=yt_target,
                explicit=explicit,
                status_callback=report,
                progress_callback=progress_callback,
            )
            if file_path:
                source_used = "YouTube (Direct)"

        # Шаг MP3-3c: Если передана прямая ссылка SoundCloud — скачиваем оригинал с SoundCloud
        # (если на Deezer и Soulseek не нашлось официального студийного релиза)
        if not file_path and is_soundcloud:
            if fallback_sc_file and fallback_sc_file.exists():
                file_path = fallback_sc_file
                source_used = "SoundCloud (Original)"
            else:
                report(f"[*] SoundCloud: скачивание оригинала ({artist} - {title})...")
                sc_target = meta.get("soundcloud_url") or url_or_query
                file_path = download_soundcloud_track(
                    url=sc_target,
                    dest_dir=target_dir,
                    artist=artist,
                    title=title,
                    duration=duration,
                    album=album,
                    progress_callback=progress_callback,
                )
                if file_path:
                    source_used = "SoundCloud (Original)"

        # Шаг MP3-4: YouTube Music с умным поиском CSVMusic (для текстовых запросов или стриминговых ссылок)
        if not file_path:
            report("[*] YouTube Music: поиск трека...")
            direct_yt = meta.get("youtube_music_url")

            file_path = download_youtube_track(
                artist=artist,
                title=title,
                dest_dir=target_dir,
                duration=duration,
                album=album,
                isrc=isrc,
                direct_url=direct_yt,
                explicit=explicit,
                status_callback=report,
                progress_callback=progress_callback,
            )
            if file_path:
                source_used = "YouTube Music"

        # Шаг MP3-5: Финальный фолбек
        if not file_path:
            report("[*] yt-dlp: фолбек-скачивание...")
            fallback_target = meta.get("direct_url") or meta.get("youtube_music_url") or meta.get("soundcloud_url") or url_or_query
            file_path = download_fallback_track(
                url=fallback_target,
                dest_dir=target_dir,
                artist=artist,
                title=title,
                duration=duration,
                album=album,
                isrc=isrc,
                status_callback=report,
                progress_callback=progress_callback,
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
        if progress_callback:
            progress_callback({
                "stage": "tagging",
                "description": f"Тегирование: {file_path.name} ({source_used})...",
                "percent": 96,
            })
        report(f"[*] Тегирование: {file_path.name} ({source_used})...")
        
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
            genre=meta.get("genre"),
            # Размер обложки выбираем по фактическому контейнеру, а не по
            # запрошенному качеству: если FLAC не нашлось, пайплайн откатывается
            # на MP3, и обложка 1400px в lossy-файле будет только балластом.
            cover_size=(
                coverart.COVER_SIZE_LOSSLESS
                if suffix in (".flac", ".wav", ".aiff", ".ape")
                else coverart.COVER_SIZE_LOSSY
            ),
        )
        if progress_callback:
            progress_callback({
                "stage": "done",
                "description": f"Готово: {file_path.name}",
                "percent": 100,
            })

        # 5. Кладём файл в локальный кэш, чтобы следующий запрос того же
        #    трека не ходил по источникам заново. Копия, а не перенос:
        #    файл в папке пользователя должен остаться на месте.
        if reuse_cached_file:
            try:
                filecache.get_file_cache().store(
                    file_path,
                    meta,
                    target_quality,
                    query=url_or_query if not is_url else "",
                    url=url_or_query if is_url else "",
                )
            except Exception:
                pass

        return file_path
    else:
        print("[-] Не удалось скачать трек ни с одного источника.")
        return None
