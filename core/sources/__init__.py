from .doh_resolver import setup_doh_fallback
setup_doh_fallback()

from .deezer import download_deezer_track, search_deezer_track
from .soulseek import search_soulseek, download_soulseek_track
from .fallback import download_fallback_track
from .youtube import download_youtube_track
from .soundcloud import download_soundcloud_track
from .youtube_matcher import find_best_youtube_match
from .tidal import search_tidal_track, download_tidal_track
from .qobuz import search_qobuz_track, download_qobuz_track
from .amazon import search_amazon_track, download_amazon_track
from .beatport import search_beatport_track, download_beatport_track
from .lossless import download_lossless_zarz_track, download_lossy_zarz_track
from .race import run_flac_race, run_lossy_race

