"""
Guards for the test suite's own isolation.

These tests do not exercise product code; they protect the two global safety
nets installed in the root ``conftest.py``. Without them a future change can
silently re-introduce tests that read the developer's real ``.env`` and
download live music into the real ``downloads/`` folder.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

def test_download_dir_is_redirected_away_from_repo():
    """Tests must never resolve DOWNLOAD_DIR to a folder inside the repo."""
    import config

    resolved = Path(config.DOWNLOAD_DIR).resolve()
    assert REPO_ROOT not in resolved.parents, (
        f"config.DOWNLOAD_DIR points inside the repository ({resolved}); "
        f"tests would pollute the working tree"
    )
    assert resolved != REPO_ROOT / "downloads"


def test_live_credentials_are_neutralised():
    """No test may inherit real service credentials from the developer's .env."""
    import config

    assert config.SLSK_USER == ""
    assert config.SLSK_PASS == ""
    assert config.SLSKD_URL == ""
    assert config.SLSKD_USER == ""
    assert config.SLSKD_PASS == ""
    assert config.BOT_TOKEN == ""


def test_real_network_is_blocked_by_default():
    """An unmocked httpx call must fail loudly rather than reach the internet."""
    import httpx

    with pytest.raises(RuntimeError, match="Real network access is blocked"):
        httpx.get("https://api.deezer.com/search", params={"q": "test"}, timeout=1.0)


def test_yt_dlp_is_blocked_by_default():
    """
    yt-dlp ходит в сеть мимо httpx, поэтому блокировка транспорта его не
    покрывает. Без отдельной защиты тест, забывший замокать загрузчик,
    молча скачивает реальную музыку с YouTube.
    """
    yt_dlp = pytest.importorskip("yt_dlp")

    with pytest.raises(RuntimeError, match="Real network access is blocked"):
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


@pytest.mark.allow_network
def test_allow_network_marker_opts_back_in():
    """The escape hatch works, so the guard is not simply a hard lockout."""
    import httpx

    try:
        httpx.get("http://127.0.0.1:9/never-listening", timeout=0.5)
    except RuntimeError as exc:
        pytest.fail(f"allow_network marker did not lift the guard: {exc}")
    except Exception:
        # Connection error against a dead local port is the expected outcome:
        # the request was actually attempted, which is all this asserts.
        pass
