"""
Pytest bootstrap for mus-downloader.

Importing ``core`` registers backward-compatible sys.modules aliases for the
modules that moved into the downloader core package (``core.metadata``,
``core.tagger``, ``core.sources.*``). This keeps legacy import paths such as
``import metadata`` / ``from sources.youtube import ...`` working during test
collection.

Two global safety nets are installed here:

1. Real credentials and paths from the developer's ``.env`` are neutralised
   and ``DOWNLOAD_DIR`` / ``BOT_TEMP_DIR`` are redirected to a throwaway
   session directory *before* ``config`` is imported. ``config.py`` resolves
   env vars with ``os.environ.setdefault``, so values set here win over
   ``.env``. Without this a test that forgets to stub a source silently
   downloads real music into the real ``downloads/`` folder, or connects to
   Soulseek with the developer's live account.
2. The httpx transport is blocked for every test that is not explicitly marked
   ``@pytest.mark.allow_network``. Tests that stub the API at the
   ``httpx.get`` / ``httpx.Client`` level are unaffected; tests that would
   otherwise reach a real server fail loudly instead of silently downloading
   live audio into the working tree.
"""

import os
import tempfile
from pathlib import Path

import pytest

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="mus-downloader-tests-"))

# Neutralise every credential/path that would let a test reach a live service.
# Individual tests that exercise a given source patch these explicitly.
for _var in (
    "SLSK_USER",
    "SLSK_PASS",
    "SLSKD_USER",
    "SLSKD_PASS",
    "SLSKD_URL",
    "SLSKD_DOWNLOADS_PATH",
    "VK_TOKEN",
    "VK_ACCESS_TOKEN",
    "SONGLINK_API_KEY",
    "ODESLI_API_KEY",
    "LASTFM_API_KEY",
    "DISCOGS_TOKEN",
    "BOT_TOKEN",
):
    os.environ[_var] = ""

os.environ.setdefault("DOWNLOAD_DIR", str(_SESSION_DIR / "downloads"))
os.environ.setdefault("BOT_TEMP_DIR", str(_SESSION_DIR / "bot_temp"))
os.environ["LOCAL_BOT_API"] = "0"

# Кэш метаданных выключен по умолчанию: иначе запись из одного теста
# изменит результат другого, тесты станут зависеть от порядка запуска, и
# каждый тест будет платить за создание файла SQLite. Тесты самого кэша
# создают собственные инстансы MetadataCache.
os.environ["CACHE_ENABLED"] = "0"

# Кэш Telegram file_id тоже выключен: он делает отправку мгновенной и
# полностью пропускает скачивание, поэтому одна сохранённая запись
# сломала бы тесты, проверяющие путь скачивания.
os.environ["FILEID_CACHE_ENABLED"] = "0"

# Дисковый кэш аудиофайлов выключен по той же причине: он возвращает
# ранее скачанный файл вместо скачивания, что ломает тесты пайплайна.
os.environ["FILE_CACHE_ENABLED"] = "0"

import core  # noqa: E402,F401


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: test is allowed to open real network connections",
    )


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail fast instead of silently hitting the internet."""
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    import httpx

    def _blocked(self, request, *args, **kwargs):  # noqa: A002
        raise RuntimeError(
            f"Real network access is blocked in tests (while performing "
            f"{getattr(request, 'url', '?')}). Mock the API at the httpx.get / "
            f"httpx.Client level, or mark the test with @pytest.mark.allow_network."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=True)

    # yt-dlp ходит в сеть мимо httpx, поэтому блокировка транспорта его не
    # покрывает. Без этого тест, забывший замокать загрузчик, молча уедет
    # качать реальную музыку с YouTube.
    yt_dlp = pytest.importorskip("yt_dlp", reason="yt-dlp not installed")

    def _blocked_extract(self, url, *args, **kwargs):
        raise RuntimeError(
            f"Real network access is blocked in tests (yt-dlp tried to extract "
            f"{url!r}). Mock core.download_track_by_link / the source downloaders, "
            f"or mark the test with @pytest.mark.allow_network."
        )

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", _blocked_extract, raising=True)
    yield
