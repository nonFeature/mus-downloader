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

# Путь для сохранения скачанных треков
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Конфигурация Telegram-бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip("'\"")

def _parse_allowed_users(raw: str) -> set[int]:
    """Парсит строку с ID пользователей из ALLOWED_USERS (разделитель - запятая)."""
    users: set[int] = set()
    if not raw:
        return users
    raw = raw.strip().strip("'\"")
    for item in raw.split(","):
        item = item.strip().strip("'\"")
        if not item:
            continue
        try:
            users.add(int(item))
        except ValueError:
            pass
    return users

ALLOWED_USERS: set[int] = _parse_allowed_users(os.getenv("ALLOWED_USERS", ""))

def is_user_allowed(user_id: int | None, allowed_users: set[int] | None = None) -> bool:
    """Проверяет, разрешен ли доступ пользователю с указанным Telegram ID."""
    if user_id is None:
        return False
    target_users = allowed_users if allowed_users is not None else ALLOWED_USERS
    return user_id in target_users

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


