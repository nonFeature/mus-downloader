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
            os.environ.setdefault(key.strip(), val.strip())

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
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "b25b959554ed7605872275b2bd96f24d")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "")

# Путь для сохранения скачанных треков
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
