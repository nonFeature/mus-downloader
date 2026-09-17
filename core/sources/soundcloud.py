import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
import yt_dlp

def _clean_filename(name: str) -> str:
    """Удаляет запрещенные символы для создания безопасного имени файла."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:120]

def download_soundcloud_track(
    url: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    duration: Optional[float] = None,
    album: Optional[str] = None
) -> Optional[Path]:
    """
    Скачивает трек с SoundCloud в оригинальном формате и битрейте (без апскейла/раздувания до 320 kbps).
    - Если автор открыл оригинальный файл (original download, wav/flac/mp3), yt-dlp скачивает его.
    - Иначе скачивает наилучший доступный аудиопоток (например, 128k MP3, AAC или Opus)
      в исходном контейнере без перекодирования.
    - Если поток в голом формате ADTS AAC (.aac), упаковывает его без перекодирования (-c copy)
      в контейнер .m4a для поддержки метаданных.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        from metadata import unshorten_url
        clean_url = unshorten_url(url.strip())
    except Exception:
        clean_url = url.strip()

    print(f"[*] SoundCloud: Скачивание оригинала без раздувания битрейта: {clean_url}")

    match = re.search(r"soundcloud\.com/([^/?#]+/[^/?#]+)", clean_url)
    slug = match.group(1).replace("/", "_") if match else "sc_track"
    slug = _clean_filename(slug)[:40]

    temp_out_template = str(dest_dir / f"temp_sc_{slug}_%(id)s.%(ext)s")

    ydl_opts = {
        'format': 'original/bestaudio/best',
        'outtmpl': temp_out_template,
        'quiet': True,
        'no_warnings': True,
        'retries': 5,
        'fragment_retries': 5,
        'socket_timeout': 30,
        'noplaylist': True,
    }

    temp_downloaded: Optional[Path] = None
    info = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            if info:
                raw_filename = ydl.prepare_filename(info)
                p = Path(raw_filename)
                if p.exists():
                    temp_downloaded = p
                else:
                    matched = [
                        f for f in dest_dir.glob(f"temp_sc_{slug}_*")
                        if not f.name.endswith((".part", ".ytdl", ".temp"))
                    ]
                    if not matched:
                        matched = [
                            f for f in dest_dir.glob("temp_sc_*")
                            if not f.name.endswith((".part", ".ytdl", ".temp"))
                        ]
                    if matched:
                        matched.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                        temp_downloaded = matched[0]
    except Exception as e:
        print(f"[!] SoundCloud: Ошибка при скачивании через yt-dlp: {e}")
        return None

    if not temp_downloaded or not temp_downloaded.exists():
        print("[!] SoundCloud: Не удалось получить аудиофайл")
        return None

    # Восстановление исполнителя и названия из метаданных трека, если они не заданы
    final_artist = artist
    final_title = title
    if info:
        if not final_artist or final_artist == "Unknown Artist":
            uploader = info.get("uploader") or info.get("artist") or info.get("creator")
            if uploader:
                final_artist = uploader
        if not final_title or final_title == "Unknown Track":
            sc_title = info.get("title") or ""
            if " - " in sc_title and (not final_artist or final_artist == "Unknown Artist"):
                parts = sc_title.split(" - ", 1)
                final_artist = parts[0].strip()
                final_title = parts[1].strip()
            elif " by " in sc_title:
                final_title = sc_title.rsplit(" by ", 1)[0].strip()
            elif sc_title:
                final_title = sc_title

    safe_artist = _clean_filename(final_artist or "Unknown Artist")
    safe_title = _clean_filename(final_title or "Unknown Track")

    final_filename = f"{safe_artist} - {safe_title}.mp3"
    final_path = dest_dir / final_filename

    from sources.youtube import _detect_audio_info, _select_mp3_bitrate, _transcode_to_mp3

    # 1. Если скачанный файл уже MP3, сохраняем как есть без перекодирования (0% потерь)
    if temp_downloaded.suffix.lower() == ".mp3":
        try:
            if final_path.exists():
                final_path.unlink()
            shutil.move(str(temp_downloaded), str(final_path))
            print(f"[+] SoundCloud: Трек сохранен без перекодирования (нативный MP3): {final_path}")
            return final_path
        except Exception as e:
            print(f"[!] Ошибка сохранения MP3 файла SoundCloud: {e}")
            try:
                temp_downloaded.unlink()
            except Exception:
                pass
            return None

    # 2. Если скачался другой формат (.m4a, .opus, .aac и т.д.), конвертируем в MP3, сохраняя битрейт
    codec, abr = _detect_audio_info(temp_downloaded, info)
    target_bitrate = _select_mp3_bitrate(codec, abr)
    print(f"[*] SoundCloud: Исходный поток {codec or 'unknown'} ({abr or 'unknown'}kbps) -> конвертация в MP3 {target_bitrate} без раздувания...")

    success = _transcode_to_mp3(temp_downloaded, final_path, bitrate=target_bitrate)
    try:
        temp_downloaded.unlink()
    except Exception:
        pass

    if success and final_path.exists():
        print(f"[+] SoundCloud: Конвертация потока в MP3 ({target_bitrate}) завершена: {final_path}")
        return final_path
    else:
        print(f"[!] SoundCloud: Не удалось сконвертировать поток в MP3")
        return None
