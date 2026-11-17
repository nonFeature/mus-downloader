import os
from pathlib import Path

# Поиск и чтение .env файла при его наличии
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            os.environ.setdefault(key.strip(), val)

# Конфигурация встроенного Soulseek P2P (через aioslsk)
SLSK_USER = os.getenv("SLSK_USER") or os.getenv("SLSKD_USER", "")
SLSK_PASS = os.getenv("SLSK_PASS") or os.getenv("SLSKD_PASS", "")

# Опциональная обратная совместимость со внешним slskd
SLSKD_URL = os.getenv("SLSKD_URL", "").rstrip("/")
SLSKD_USER = os.getenv("SLSKD_USER", "") or SLSK_USER
SLSKD_PASS = os.getenv("SLSKD_PASS", "") or SLSK_PASS
SLSKD_DOWNLOADS_PATH = os.getenv("SLSKD_DOWNLOADS_PATH", "")

def is_soulseek_configured() -> bool:
    """Проверяет, настроен ли встроенный Soulseek или внешний slskd."""
    return bool((SLSK_USER and SLSK_PASS) or SLSKD_URL)

# Опциональные базы метаданных
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "")
SONGLINK_API_KEY = (os.getenv("SONGLINK_API_KEY") or os.getenv("ODESLI_API_KEY", "")).strip().strip("'\"")
VK_TOKEN = (os.getenv("VK_TOKEN") or os.getenv("VK_ACCESS_TOKEN", "")).strip().strip("'\"")

# User-Agent для внешних API.
#
# Два разных UA, потому что требования противоположны:
#
# BROWSER_USER_AGENT - для CDN и агрегаторов (Deezer, iTunes, обложки,
#   VK, SoundCloud). Многие из них режут запросы с не-браузерным UA или
#   отдают урезанные ответы, поэтому там нужен вид нормального браузера.
#
# USER_AGENT - идентифицирующий, для MusicBrainz и Cover Art Archive.
#   Там явным текстом просят: имя приложения, версию и КОНТАКТНЫЙ адрес.
#   Браузерный UA там считается маскировкой и приводит к блокировке.
#
# Подставлять чужой адрес нельзя: жалобы и блокировки прилетят к человеку,
# который об этом не знает. Укажи свою почту через CONTACT_EMAIL.
BROWSER_USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
).strip()

USER_AGENT = os.getenv("USER_AGENT", "mus-downloader/0.1 ( https://github.com/ )").strip()
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "").strip().strip("'\"")


# ── Дополнительные метаданные из публичных web-API ────────────────────
#
# METADATA_EXTRAS — общий выключатель. TIDAL даёт ISRC, ℗-копирайт, номера
# диска/трека и их общие, UPC, BPM, тональность, ReplayGain и обложку;
# Apple Music — композиторов, лейбл и жанры. Оба работают анонимно, без
# аккаунтов и подписок; запросы идут только на tidal.com и apple.com.
#
# SPOTIFY_METADATA — третий источник: ISRC, полный список авторов, лейбл,
# UPC, копирайты через анонимную сессию web-плеера (TOTP + client-token).
# Хрупкий канал: при изменении внутреннего API Spotify источник просто
# выключается до следующего обновления TOTP-секрета.
def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().strip("'\"").lower() in ("1", "true", "yes", "on")


METADATA_EXTRAS: bool = _env_flag("METADATA_EXTRAS")
TIDAL_METADATA: bool = _env_flag("TIDAL_METADATA")
APPLE_METADATA: bool = _env_flag("APPLE_METADATA")
SPOTIFY_METADATA: bool = _env_flag("SPOTIFY_METADATA")
# Регион каталога для обоих API: от него зависит, что вообще отдадут.
TIDAL_COUNTRY_CODE: str = os.getenv("TIDAL_COUNTRY_CODE", "US").strip() or "US"
APPLE_STOREFRONT: str = os.getenv("APPLE_STOREFRONT", "US").strip() or "US"


# ── Zarz-шлюз: lossless-источники из экосистемы Mornye ─────────────────
#
# Tidal / Amazon Music / Qobuz / Beatport отдают FLAC через шлюз
# api.zarz.moe (пул платных аккаунтов на стороне сервера). Первая авторизация
# решает Cloudflare Turnstile в реальном Chrome (нужны pydoll и установленный
# Chrome/Chromium); дальше сессия хранится на диске в ZARZ_DATA_DIR и запросы
# подписываются HMAC. Секреты сессий живые — каталог в .gitignore.
#
# Поиск у Tidal (публичный web-API) и Amazon (анонимный mesk-API) работает
# без шлюза; Qobuz и Beatport ищут уже через Zarz.
ZARZ_ENABLED: bool = _env_flag("ZARZ_ENABLED")
ZARZ_API_BASE: str = (
    os.getenv("ZARZ_API_BASE", "https://api.zarz.moe/v2").strip().strip("'\"").rstrip("/")
)
ZARZ_DATA_DIR: str = os.getenv("ZARZ_DATA_DIR", ".zarz").strip().strip("'\"") or ".zarz"

ZARZ_TIDAL: bool = _env_flag("ZARZ_TIDAL")
ZARZ_QOBUZ: bool = _env_flag("ZARZ_QOBUZ")
ZARZ_AMAZON: bool = _env_flag("ZARZ_AMAZON")
ZARZ_BEATPORT: bool = _env_flag("ZARZ_BEATPORT")

# Качество по умолчанию для каждого провайдера:
# Tidal: HI_RES_LOSSLESS (24-bit, с откатом на LOSSLESS 16/44.1);
# Qobuz: hi-res-max (24/192, с откатом на hi-res и cd);
# Amazon: flac (или opus/eac3/ac4 — Atmos/lossy, в FLAC-ветке не считается);
# Beatport: lossless (FLAC) или high (AAC 256).
# В гонке источников побеждает фактически самый жирный файл, поэтому
# дефолты запрашивают максимум — откатные цепочки отработают сами.
ZARZ_TIDAL_QUALITY: str = os.getenv("ZARZ_TIDAL_QUALITY", "HI_RES_LOSSLESS").strip() or "HI_RES_LOSSLESS"
ZARZ_QOBUZ_QUALITY: str = os.getenv("ZARZ_QOBUZ_QUALITY", "hi-res-max").strip() or "hi-res-max"
ZARZ_AMAZON_CODEC: str = os.getenv("ZARZ_AMAZON_CODEC", "flac").strip() or "flac"
ZARZ_BEATPORT_QUALITY: str = os.getenv("ZARZ_BEATPORT_QUALITY", "lossless").strip() or "lossless"


# ── Гонка источников (PARALLEL_SOURCES) ───────────────────────────────
#
# Все источники ветки стартуют одновременно; победитель выбирается по
# фактическому качеству файла:
# - FLAC: самый жирный по битности/семплрейту (максимум 24/192 отменяет остальных);
# - MP3: первый, кто отдал ≥ RACE_MIN_BITRATE kbps (lossless от Soulseek —
#   безусловная победа), иначе лучший по битрейту из доехавших.
# Проигравшие отменяются (где поддерживается) и удаляются.
# RACE_TIMEOUT — общий потолок гонки в секундах.
PARALLEL_SOURCES: bool = _env_flag("PARALLEL_SOURCES")
RACE_TIMEOUT: int = int(os.getenv("RACE_TIMEOUT", "240").strip().strip("'\"") or 240)
RACE_MIN_BITRATE: int = int(os.getenv("RACE_MIN_BITRATE", "320").strip().strip("'\"") or 320)



def build_user_agent(extra: str = "") -> str:
    """
    Собирает идентифицирующий User-Agent в формате, который ждут
    MusicBrainz и Cover Art Archive: ``<name>/<version> ( <contact> )``.
    """
    base = USER_AGENT
    if CONTACT_EMAIL and CONTACT_EMAIL not in base:
        base = f"{base} {CONTACT_EMAIL}"
    return f"{base} {extra}".strip() if extra else base

# Путь для сохранения скачанных треков
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Конфигурация Telegram-бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip("'\"")

BOT_API_SERVER_URL = os.getenv("BOT_API_SERVER_URL", "").strip().strip("'\"").rstrip("/")
BOT_TEMP_DIR = Path(os.getenv("BOT_TEMP_DIR", str(DOWNLOAD_DIR / "temp_bot")))
BOT_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Кастомные (премиум) Telegram-эмодзи в сообщениях и кнопках.
# Выключи (CUSTOM_EMOJI=false), если Telegram отклоняет такие сообщения.
CUSTOM_EMOJI: bool = os.getenv("CUSTOM_EMOJI", "true").strip().strip("'\"").lower() in ("1", "true", "yes", "on")

# Конфигурация локального Telegram Bot API Server
_raw_api_id = (os.getenv("TELEGRAM_API_ID") or os.getenv("TG_API_ID", "")).strip().strip("'\"")
TELEGRAM_API_ID: int | str = int(_raw_api_id) if _raw_api_id.isdigit() else _raw_api_id
TG_API_ID = TELEGRAM_API_ID

TELEGRAM_API_HASH: str = (os.getenv("TELEGRAM_API_HASH") or os.getenv("TG_API_HASH", "")).strip().strip("'\"")
TG_API_HASH = TELEGRAM_API_HASH

_local_bot_api_env = os.getenv("LOCAL_BOT_API")
if _local_bot_api_env is not None:
    LOCAL_BOT_API: bool = _local_bot_api_env.strip().strip("'\"").lower() in ("1", "true", "yes", "on")
else:
    LOCAL_BOT_API: bool = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)

TELEGRAM_BOT_API_BIN: str = os.getenv("TELEGRAM_BOT_API_BIN", "").strip().strip("'\"")

try:
    BOT_API_PORT: int = int(os.getenv("BOT_API_PORT", "8081").strip().strip("'\""))
except ValueError:
    BOT_API_PORT = 8081

BOT_API_DATA_DIR: Path = Path(os.getenv("BOT_API_DATA_DIR", str(DOWNLOAD_DIR / "bot_api_data")))
BOT_API_DATA_DIR.mkdir(parents=True, exist_ok=True)


def is_local_bot_api_enabled() -> bool:
    """Проверяет, включено ли автоматическое управление локальным Telegram Bot API Server."""
    return bool(LOCAL_BOT_API)


