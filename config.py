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

# Конфигурация Soulseek (через slskd)
SLSKD_URL = os.getenv("SLSKD_URL", "").rstrip("/")
SLSKD_USER = os.getenv("SLSKD_USER", "")
SLSKD_PASS = os.getenv("SLSKD_PASS", "")
SLSKD_DOWNLOADS_PATH = os.getenv("SLSKD_DOWNLOADS_PATH", "")

# Опциональные базы метаданных
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "b25b959554ed7605872275b2bd96f24d")
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "")

# Путь для сохранения скачанных треков
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
