"""Пути данных для вендоренного zarz-клиента.

Замена ``mornye.core.paths``: сессии подписанных запросов (HMAC-секреты)
хранятся в каталоге ``ZARZ_DATA_DIR`` (по умолчанию ``./.zarz`` рядом с
проектом — так же, как ``DOWNLOAD_DIR``). Каталог добавлен в .gitignore:
внутри живые секреты сессий.
"""
import os
from pathlib import Path


def _data_dir() -> Path:
    # ВАЖНО: проверять сырую строку, а не str(Path(...)) — Path("") на
    # Windows превращается в "." и сессии уедут в корень проекта.
    raw = os.getenv("ZARZ_DATA_DIR", "").strip().strip("'\"")
    if raw:
        return Path(raw)
    try:
        import config

        raw_cfg = str(getattr(config, "ZARZ_DATA_DIR", "") or "").strip()
        if raw_cfg:
            return Path(raw_cfg)
    except Exception:
        pass
    return Path(".zarz")


def data_path(*parts: str) -> Path:
    base = _data_dir()
    return base.joinpath(*parts) if parts else base


def data_dir() -> str:
    return str(_data_dir())
