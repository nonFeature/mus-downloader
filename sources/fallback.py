from pathlib import Path
from typing import Optional
from sources.youtube import download_youtube_track

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
    Использует умный подбор треков с YouTube Music (CSVMusic matcher)
    и транскодирование в MP3 320 kbps.
    """
    direct_url = url if ("youtube.com" in url or "youtu.be" in url or "soundcloud.com" in url) else None
    return download_youtube_track(
        artist=artist,
        title=title,
        dest_dir=dest_dir,
        duration=duration,
        album=album,
        isrc=isrc,
        direct_url=direct_url
    )
