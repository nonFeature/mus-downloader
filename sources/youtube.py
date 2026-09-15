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

def _detect_audio_info(src_path: Path, ytdl_info: Optional[dict] = None) -> tuple[str, Optional[int]]:
    """
    Определяет аудиокодек и примерный битрейт (в kbps) исходного файла.
    Возвращает кортеж (codec, abr).
    """
    codec = ""
    abr = None
    if ytdl_info:
        codec = (ytdl_info.get("acodec") or "").lower()
        abr_val = ytdl_info.get("abr")
        if abr_val is not None:
            try:
                abr = round(float(abr_val))
            except (ValueError, TypeError):
                abr = None
        if (not codec or codec == "none") and "requested_downloads" in ytdl_info:
            reqs = ytdl_info.get("requested_downloads")
            if reqs and isinstance(reqs, list) and len(reqs) > 0:
                codec = (reqs[0].get("acodec") or "").lower()
                if not abr and reqs[0].get("abr") is not None:
                    try:
                        abr = round(float(reqs[0].get("abr")))
                    except (ValueError, TypeError):
                        pass

    # Пробуем ffprobe для точного определения фактического файла
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bit_rate",
            "-of", "default=noprint_wrappers=1",
            str(src_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.strip().splitlines():
                if line.startswith("codec_name="):
                    val = line.split("=", 1)[1].strip().lower()
                    if val and val != "none":
                        codec = val
                elif line.startswith("bit_rate="):
                    val = line.split("=", 1)[1].strip()
                    if val.isdigit():
                        abr = round(int(val) / 1000)
    except Exception:
        pass

    # Фолбек по расширению файла
    if not codec or codec == "none":
        suffix = src_path.suffix.lower()
        if suffix in [".opus", ".webm"]:
            codec = "opus"
        elif suffix in [".m4a", ".aac"]:
            codec = "aac"
        elif suffix == ".mp3":
            codec = "mp3"
        elif suffix == ".flac":
            codec = "flac"
        else:
            codec = "unknown"

    return codec, abr

def _select_mp3_bitrate(codec: str, abr: Optional[int] = None) -> str:
    """
    Выбирает целевой битрейт MP3 при транскодировании:
    - Для Opus (обычно ~160k на YouTube): переводим в 256 kbps (диапазон 250-280 kbps).
    - Для AAC <= 96k: переводим в 128 kbps.
    - Для AAC <= 128k: переводим в 192 kbps.
    - Для AAC > 128k: переводим в 256 kbps.
    - Для очень высокого битрейта (> 280k): сохраняем до 320 kbps.
    """
    codec_lower = (codec or "").lower()
    abr_int = None
    if abr is not None:
        try:
            abr_int = int(round(float(abr)))
        except (ValueError, TypeError):
            abr_int = None

    if abr_int is not None and abr_int > 280:
        return "320k"

    if "opus" in codec_lower:
        if abr_int is not None and abr_int < 90:
            return "128k"
        return "256k"

    if "aac" in codec_lower or "mp4a" in codec_lower:
        if abr_int is not None and abr_int <= 96:
            return "128k"
        if abr_int is not None and abr_int <= 128:
            return "192k"
        return "256k"

    return "256k"

def _transcode_to_mp3(src_path: Path, dst_path: Path, bitrate: str = "256k") -> bool:
    """
    Транскодирует исходный аудиофайл в MP3 с заданным битрейтом (LAME).
    Поддерживает как фиксированный битрейт (например, '256k', '192k'),
    так и VBR режим ('vbr_v0' / 'v0' для диапазона 250-280 kbps).
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
        ]
        if bitrate in ["vbr_v0", "v0", "vbr"]:
            cmd.extend(["-q:a", "0"])
        else:
            cmd.extend(["-b:a", bitrate])
        cmd.append(str(dst_path))

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and dst_path.exists() and dst_path.stat().st_size > 1000:
            return True
    except Exception as e:
        print(f"[!] Ошибка вызова системного ffmpeg: {e}")
    return False

def _transcode_to_mp3_320(src_path: Path, dst_path: Path) -> bool:
    """Обратная совместимость: транскодирует в MP3 320 kbps."""
    return _transcode_to_mp3(src_path, dst_path, bitrate="320k")


def download_youtube_track(
    artist: str,
    title: str,
    dest_dir: Path,
    duration: Optional[float] = None,
    album: Optional[str] = None,
    isrc: Optional[str] = None,
    direct_url: Optional[str] = None,
    explicit: Optional[bool] = None
) -> Optional[Path]:
    """
    Ищет и скачивает трек с YouTube Music по алгоритму CSVMusic (с валидацией длительности
    и отсевом каверов/live/clean) и конвертирует в MP3 320 kbps (LAME).
    Если уверенность низкая (< 0.6), возвращает None для перехода к Soulseek.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    video_id = None
    target_url = None

    # 0. Если передана прямая ссылка SoundCloud, скачиваем оригинал через модуль soundcloud
    if direct_url and ("soundcloud.com" in direct_url.lower() or "on.soundcloud.com" in direct_url.lower()):
        from sources.soundcloud import download_soundcloud_track
        return download_soundcloud_track(
            url=direct_url,
            dest_dir=dest_dir,
            artist=artist,
            title=title,
            duration=duration,
            album=album
        )

    # 1. Если передан прямой URL YouTube
    if direct_url and ("youtube.com" in direct_url or "youtu.be" in direct_url):
        target_url = direct_url
        print("[*] YouTube: прямая ссылка...")
        v_match = re.search(r"(?:v=|youtu\.be/|embed/|watch\?v=)([\w-]{11})", direct_url)
        if v_match:
            video_id = v_match.group(1)
        else:
            video_id = "direct"
    else:
        # 2. Умный поиск через YouTube Music (CSVMusic алгоритм)
        print(f"[*] YouTube Music: поиск '{artist} - {title}'...")
        best_match, score, options = find_best_youtube_match(
            artist=artist,
            title=title,
            duration=duration,
            album=album,
            isrc=isrc,
            explicit=explicit
        )

        if not best_match:
            if options:
                print(f"[!] YouTube Music: кандидат '{options[0]['title']}' отклонен ({score:.2f} < 0.60)")
            else:
                print("[!] YouTube Music: нет подходящих результатов")
            return None

        video_id = best_match["videoId"]
        target_url = f"https://music.youtube.com/watch?v={video_id}"
        print(f"[+] YouTube Music: выбран '{best_match['title']}' ({int(score*100)}%)")

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

    # 4. Определение параметров потока и адаптивный выбор битрейта MP3
    codec, abr = _detect_audio_info(temp_downloaded, info)
    target_bitrate = _select_mp3_bitrate(codec, abr)
    print(f"[*] YouTube: конвертация {codec}/{abr or '?'}k -> MP3 {target_bitrate} CBR...")
    success = _transcode_to_mp3(temp_downloaded, final_path, bitrate=target_bitrate)

    # Очищаем временный файл
    try:
        temp_downloaded.unlink()
    except Exception:
        pass

    if success and final_path.exists():
        print(f"[+] YouTube: готов {final_path.name}")
        return final_path
    else:
        print("[!] YouTube: Не удалось сконвертировать поток в MP3")
        return None
