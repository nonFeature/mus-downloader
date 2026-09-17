from pathlib import Path
from typing import Optional
from .youtube import download_youtube_track
from .soundcloud import download_soundcloud_track

def download_fallback_track(
    url: str,
    dest_dir: Path,
    artist: str = "",
    title: str = "",
    duration: Optional[float] = None,
    album: Optional[str] = None,
    isrc: Optional[str] = None
) -> Optional[Path]:
    """
    Фолбек-загрузчик для YouTube / SoundCloud.
    - Для SoundCloud скачивает трек в оригинале без раздувания битрейта.
    - Для YouTube скачивает через YouTube Music и конвертирует в MP3 (256k для Opus).
    """
    if url and ("soundcloud.com" in url.lower() or "on.soundcloud.com" in url.lower()):
        return download_soundcloud_track(
            url=url,
            dest_dir=dest_dir,
            artist=artist,
            title=title,
            duration=duration,
            album=album
        )

    direct_url = url if ("youtube.com" in url or "youtu.be" in url) else None
    return download_youtube_track(
        artist=artist,
        title=title,
        dest_dir=dest_dir,
        duration=duration,
        album=album,
        isrc=isrc,
        direct_url=direct_url
    )
