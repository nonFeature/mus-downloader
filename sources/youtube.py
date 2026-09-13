import os
import re
import subprocess
import shutil
from pathlib import Path
from typing import Optional, List, Dict
import yt_dlp
from sources.youtube_matcher import find_best_youtube_match

YOUTUBE_CLIENTS = ["web_embedded", None, "ios", "tv", "android_vr"]

def _clean_filename(name: str) -> str:
    """Удаляет запрещенные символы для создания безопасного имени файла."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:120]

def _transcode_to_mp3_320(src_path: Path, dst_path: Path) -> bool:
    """
    Транскодирует исходный аудиофайл (Opus/AAC/WebM) в честный MP3 320 kbps (LAME CBR)
    с защитой от клиппинга.
    """
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(src_path),
            "-vn",
            "-sn",
            "-codec:a", "libmp3lame",
            "-b:a", "320k",
            str(dst_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and dst_path.exists() and dst_path.stat().st_size > 1000:
            return True
    except Exception as e:
        print(f"[!] Ошибка вызова системного ffmpeg: {e}")
    return False

def download_youtube_track(
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    album: Optional[str] = None,
    isrc: Optional[str] = None,
    direct_url: Optional[str] = None
) -> Optional[Path]:
    """
    Ищет и скачивает трек с YouTube Music по алгоритму CSVMusic (с валидацией длительности
    и отсевом каверов/live) и конвертирует в MP3 320 kbps (LAME).
    Если уверенность низкая (< 0.6), возвращает None для перехода к Soulseek.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    video_id = None
    target_url = None

    # 1. Если передан прямой URL YouTube или SoundCloud
    if direct_url and ("youtube.com" in direct_url or "youtu.be" in direct_url or "soundcloud.com" in direct_url):
        target_url = direct_url
        print(f"[*] YouTube: Использование прямой ссылки: {direct_url}")
        v_match = re.search(r"(?:v=|youtu\.be/|embed/|watch\?v=)([\w-]{11})", direct_url)
        if v_match:
            video_id = v_match.group(1)
        else:
            video_id = "direct"
    else:
        # 2. Умный поиск через YouTube Music (CSVMusic алгоритм)
        print(f"[*] YouTube Music: Умный поиск трека '{artist} - {title}' (CSVMusic matcher)...")
        best_match, score, options = find_best_youtube_match(
            artist=artist,
            title=title,
            duration=duration,
            album=album,
            isrc=isrc
        )

        if not best_match:
            if options:
                print(f"[!] YouTube Music: Лучший кандидат '{options[0]['title']}' имеет низкую оценку ({score:.2f} < 0.60). Отклонено.")
            else:
                print("[!] YouTube Music: Не найдено подходящих результатов.")
            return None

        video_id = best_match["videoId"]
        target_url = f"https://music.youtube.com/watch?v={video_id}"
        print(f"[+] YouTube Music: Выбран кандидат '{best_match['title']}' ({best_match['author']}) с уверенностью {score:.2f}")

    safe_artist = _clean_filename(artist or "Unknown Artist")
    safe_title = _clean_filename(title or "Unknown Track")
    final_filename = f"{safe_artist} - {safe_title}.mp3"
    final_path = dest_dir / final_filename

    temp_out_template = str(dest_dir / f"temp_{video_id}_%(id)s.%(ext)s")

    # 3. Скачивание лучшего аудиопотока через yt-dlp
    ydl_opts = {
        'format': 'bestaudio[abr>=128]/bestaudio/best',
        'outtmpl': temp_out_template,
        'quiet': True,
        'no_warnings': True,
        'retries': 5,
        'fragment_retries': 5,
        'socket_timeout': 30,
        'noplaylist': True,
    }

    temp_downloaded: Optional[Path] = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if info:
                # Находим скачанный временный файл
                raw_filename = ydl.prepare_filename(info)
                p = Path(raw_filename)
                if p.exists():
                    temp_downloaded = p
                else:
                    # Поиск по шаблону
                    matched = list(dest_dir.glob(f"temp_{video_id}_*"))
                    if matched:
                        temp_downloaded = matched[0]
    except Exception as e:
        print(f"[!] YouTube: Ошибка при скачивании через yt-dlp: {e}")
        return None

    if not temp_downloaded or not temp_downloaded.exists():
        print("[!] YouTube: Не удалось получить временный аудиофайл")
        return None

    # 4. Транскодирование в честный MP3 320 kbps через FFmpeg
    print(f"[*] YouTube: Конвертация потока в честный MP3 320 kbps (LAME CBR)...")
    success = _transcode_to_mp3_320(temp_downloaded, final_path)

    # Очищаем временный файл
    try:
        temp_downloaded.unlink()
    except Exception:
        pass

    if success and final_path.exists():
        print(f"[+] YouTube: Скачивание и конвертация завершены: {final_path}")
        return final_path
    else:
        print("[!] YouTube: Не удалось сконвертировать поток в MP3")
        return None
