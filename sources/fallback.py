import yt_dlp
import re
from pathlib import Path
from typing import Optional

def download_fallback_track(url: str, dest_dir: Path, artist: str = "", title: str = "") -> Optional[Path]:
    """
    Скачивает аудио с YouTube Music, YouTube или SoundCloud через yt-dlp.
    Если передана ссылка Spotify или другого неподдерживаемого сервиса,
    ищет трек по тексту на YouTube Music.
    Конвертирует в MP3 с качеством 320kbps (или лучшим доступным).
    """
    # Определяем, является ли URL скачиваемым напрямую через yt-dlp
    non_downloadable = any(d in url for d in [
        'spotify.com', 'music.apple.com', 'tidal.com',
        'deezer.com', 'zvuk.com', 'music.yandex'
    ])

    if non_downloadable and (artist or title):
        # Ищем трек на YouTube Music по тексту вместо прямого URL
        query = f"{artist} - {title}" if artist and title else (title or artist)
        search_url = f"https://music.youtube.com/search?q={query}"
        yt_search = f"ytsearch1:{query}"
        print(f"[*] Fallback: Spotify/неподдерживаемый URL, ищем на YouTube Music: '{query}'")
        return _do_yt_download(yt_search, dest_dir)
    else:
        print(f"[*] Fallback: Скачивание по ссылке через yt-dlp: {url}")
        return _do_yt_download(url, dest_dir)


def _do_yt_download(url_or_search: str, dest_dir: Path) -> Optional[Path]:
    """Внутренний метод: запускает yt-dlp для скачивания/поиска."""
    temp_template = str(dest_dir / "temp_fallback_%(id)s.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio[abr>=96]/bestaudio/best',
        'outtmpl': temp_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_or_search, download=True)
            if not info:
                return None

            # Если это результат поиска, берём первый элемент
            if 'entries' in info:
                info = info['entries'][0]

            temp_path = Path(ydl.prepare_filename(info))
            final_temp_path = temp_path.with_suffix(".mp3")

            if not final_temp_path.exists():
                temp_files = list(dest_dir.glob(f"temp_fallback_{info['id']}.mp3"))
                if temp_files:
                    final_temp_path = temp_files[0]
                else:
                    print("[!] Fallback: Не удалось найти скачанный файл после конвертации")
                    return None

            title = info.get("title", "Unknown Track")
            uploader = info.get("uploader", info.get("channel", "Unknown Artist"))

            clean_title = re.sub(r'[\\/*?"<>|]', "_", title)
            clean_uploader = re.sub(r'[\\/*?"<>|]', "_", uploader)

            output_path = dest_dir / f"{clean_uploader} - {clean_title}.mp3"
            final_temp_path.replace(output_path)
            print(f"[+] Fallback: Скачивание завершено: {output_path}")
            return output_path

    except Exception as e:
        print(f"[!] Fallback: Ошибка при скачивании по ссылке: {e}")

    return None
