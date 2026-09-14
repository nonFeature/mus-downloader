from .doh_resolver import setup_doh_fallback
setup_doh_fallback()

from .deezer import download_deezer_track, search_deezer_track
from .soulseek import search_soulseek, download_soulseek_track
from .fallback import download_fallback_track
from .youtube import download_youtube_track
from .soundcloud import download_soundcloud_track
from .youtube_matcher import find_best_youtube_match

