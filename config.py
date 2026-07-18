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

# Конфигурация Яндекс Музыки
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "")

# Конфигурация Сбер Звук (zvuk.com)
ZVUK_TOKEN = os.getenv("ZVUK_TOKEN", "")

# Конфигурация Monochrome
MONOCHROME_HIFI_URL = os.getenv("MONOCHROME_HIFI_URL", "https://api.monochrome.tf").rstrip("/")
MONOCHROME_QOBUZ_PROXY_URL = os.getenv("MONOCHROME_QOBUZ_PROXY_URL", "https://qobuz.kennyy.com.br").rstrip("/")
MONOCHROME_AMAZON_API_URL = os.getenv("MONOCHROME_AMAZON_API_URL", "https://amz.geeked.wtf").rstrip("/")
MONOCHROME_AMAZON_JWT = os.getenv("MONOCHROME_AMAZON_JWT", "")
MONOCHROME_AMAZON_BYPASS_TOKEN = os.getenv("MONOCHROME_AMAZON_BYPASS_TOKEN", "")

# Путь для сохранения скачанных треков
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
