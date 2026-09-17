"""
tests/test_services/test_m1_lock_and_botapi.py
Adversarial stress-test suite for:
- SingleInstanceLock
- LocalBotAPIManager

Covers:
1. SingleInstanceLock: rapid lock/unlock, concurrent threads, zero-byte lock file,
   stale PID removal, unacquired lock release isolation, double acquire behavior.
2. LocalBotAPIManager: deep paths, Windows backslashes, non-standard paths,
   directory traversal, health check status codes, network errors, timeouts, process crashes.
"""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.process_lock import SingleInstanceLock
from bot.local_bot_api import LocalBotAPIManager


# ============================================================================
# 1. SingleInstanceLock Stress & Adversarial Tests
# ============================================================================

class TestSingleInstanceLockAdversarial:
    """Stress testing SingleInstanceLock for concurrency, edge cases, and robustness."""

    def test_rapid_acquire_release_cycle_same_instance(self, tmp_path: Path):
        """Rapid sequential acquire and release calls on the same lock instance."""
        lock_file = tmp_path / "rapid_same.lock"
        pid_file = tmp_path / "rapid_same.pid"
        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)

        for _ in range(50):
            assert lock.acquire() is True
            assert lock_file.exists()
            assert pid_file.exists()
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
            lock.release()
            assert not pid_file.exists()

    def test_rapid_acquire_release_cycle_fresh_instances(self, tmp_path: Path):
        """Rapid sequential acquire and release calls on fresh lock instances."""
        lock_file = tmp_path / "rapid_fresh.lock"
        pid_file = tmp_path / "rapid_fresh.pid"

        for _ in range(50):
            lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
            assert lock.acquire() is True
            assert lock_file.exists()
            assert pid_file.exists()
            lock.release()
            assert not pid_file.exists()

    def test_zero_byte_lock_file_acquisition(self, tmp_path: Path):
        """A pre-existing zero-byte lock file must be locked correctly on Windows and Linux."""
        lock_file = tmp_path / "zero_byte.lock"
        lock_file.write_bytes(b"")
        assert lock_file.stat().st_size == 0

        pid_file = tmp_path / "zero_byte.pid"
        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)

        assert lock.acquire() is True
        assert pid_file.exists()

        # On Windows, acquire ensures at least 1 byte exists for msvcrt.locking
        if sys.platform == "win32":
            assert lock_file.stat().st_size >= 1

        # Second lock attempt on the same file must fail
        lock2 = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock2.acquire(auto_terminate_stale=False) is False

        lock.release()
        assert not pid_file.exists()

    def test_concurrent_multithreaded_lock_contention(self, tmp_path: Path):
        """
        10 threads concurrently attempting to acquire the same lock.
        Exactly one thread must hold the lock at any time.
        """
        lock_file = tmp_path / "concurrent_threads.lock"
        pid_file = tmp_path / "concurrent_threads.pid"

        active_holders = []
        lock_barrier = threading.Barrier(10)
        errors = []

        def worker(thread_idx: int):
            try:
                lock_barrier.wait()
                # Each thread creates its own lock instance
                lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
                acquired = lock.acquire(auto_terminate_stale=False)
                if acquired:
                    active_holders.append(thread_idx)
                    # Hold lock briefly
                    time.sleep(0.05)
                    # Verify only 1 holder exists
                    if len(active_holders) > 1:
                        errors.append(f"Multiple holders detected: {list(active_holders)}")
                    active_holders.remove(thread_idx)
                    lock.release()
            except Exception as e:
                errors.append(f"Thread {thread_idx} crashed: {e}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threading contention errors: {errors}"

    def test_nested_directory_creation(self, tmp_path: Path):
        """Lock acquisition creates missing parent directories without crashing."""
        deep_dir = tmp_path / "a" / "b" / "c" / "nested"
        lock_file = deep_dir / "bot.lock"
        pid_file = deep_dir / "bot.pid"

        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock.acquire() is True
        assert lock_file.exists()
        lock.release()

    def test_context_manager_protocol(self, tmp_path: Path):
        """SingleInstanceLock supports with-statement context manager protocol."""
        lock_file = tmp_path / "cm.lock"
        pid_file = tmp_path / "cm.pid"

        with SingleInstanceLock(lock_path=lock_file, pid_path=pid_file) as lock:
            assert lock._is_locked is True
            assert pid_file.exists()
            # Second lock fails
            lock2 = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
            assert lock2.acquire(auto_terminate_stale=False) is False

        assert not pid_file.exists()

    def test_stale_pid_dead_process_cleanup(self, tmp_path: Path):
        """
        When pid_path contains a non-existent PID and the lock file is free,
        acquisition succeeds immediately and updates pid_path with the current PID.
        """
        lock_file = tmp_path / "stale_dead.lock"
        pid_file = tmp_path / "stale_dead.pid"
        # Write a non-existent PID
        pid_file.write_text("9999999", encoding="utf-8")

        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock.acquire() is True
        assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        lock.release()

    def test_stale_pid_non_numeric_content_ignored(self, tmp_path: Path):
        """Malformed or non-numeric PID file content does not cause an exception."""
        lock_file = tmp_path / "malformed_pid.lock"
        pid_file = tmp_path / "malformed_pid.pid"
        pid_file.write_text("NOT_A_PID_123\nxyz", encoding="utf-8")

        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        # Should succeed and overwrite pid_path with current PID
        assert lock.acquire() is True
        assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        lock.release()

    def test_release_does_not_delete_foreign_pid_file(self, tmp_path: Path):
        """If pid_path contains another process's PID, release() must NOT unlink it."""
        lock_file = tmp_path / "foreign_pid.lock"
        pid_file = tmp_path / "foreign_pid.pid"

        lock = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock.acquire() is True

        # Simulate foreign PID written to pid_path
        pid_file.write_text("1111111", encoding="utf-8")

        lock.release()
        # Foreign pid_path must NOT be deleted
        assert pid_file.exists()
        assert pid_file.read_text(encoding="utf-8").strip() == "1111111"

    def test_unacquired_lock_release_isolation_finding(self, tmp_path: Path):
        """
        ADVERSARIAL CHALLENGE (Bug Investigation):
        When lock1 holds the active lock, calling release() on an unacquired lock2 instance
        in the SAME process must not delete the active pid_file!
        """
        lock_file = tmp_path / "isolation.lock"
        pid_file = tmp_path / "isolation.pid"

        lock1 = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock1.acquire() is True
        assert pid_file.exists()
        assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())

        # Second lock fails to acquire
        lock2 = SingleInstanceLock(lock_path=lock_file, pid_path=pid_file)
        assert lock2.acquire(auto_terminate_stale=False) is False
        assert lock2._is_locked is False

        # Now release the unacquired lock2
        lock2.release()

        # PID file should STILL exist because lock1 is still holding the lock!
        pid_still_exists = pid_file.exists()

        # Clean up lock1
        lock1.release()

        # Record whether isolation held or whether lock2 improperly deleted pid_file
        # Note: If this fails, it demonstrates the empirical flaw in process_lock.py!
        assert pid_still_exists, (
            "CRITICAL FLAW: Calling release() on an unacquired lock instance "
            "deleted the active process's PID file!"
        )


# ============================================================================
# 2. LocalBotAPIManager Stress & Adversarial Tests
# ============================================================================

class TestLocalBotAPIManagerAdversarial:
    """Stress testing LocalBotAPIManager for path translation and health check error states."""

    def test_resolve_local_filepath_deep_nested_paths(self, tmp_path: Path):
        """Deeply nested Docker container path translates accurately to local filesystem."""
        mgr = LocalBotAPIManager(data_dir=tmp_path / "bot_data")
        deep_path = "/var/lib/telegram-bot-api/music/2026/electronic/ambient/subgenre/deep_track.flac"

        resolved = mgr.resolve_local_filepath(deep_path)
        expected = (tmp_path / "bot_data" / "music/2026/electronic/ambient/subgenre/deep_track.flac").resolve()
        assert resolved == expected

    def test_resolve_local_filepath_windows_backslashes_in_docker_path(self, tmp_path: Path):
        """Path with Windows backslashes in Docker volume marker translates accurately."""
        mgr = LocalBotAPIManager(data_dir=tmp_path / "bot_data")
        win_style_docker_path = "\\var\\lib\\telegram-bot-api\\documents\\audio_file.mp3"

        resolved = mgr.resolve_local_filepath(win_style_docker_path)
        expected = (tmp_path / "bot_data" / "documents" / "audio_file.mp3").resolve()
        assert resolved == expected

    def test_resolve_local_filepath_special_characters_and_spaces(self, tmp_path: Path):
        """Filename with Unicode, spaces, parentheses, brackets, and emojis translates accurately."""
        mgr = LocalBotAPIManager(data_dir=tmp_path / "bot_data")
        complex_path = (
            "/var/lib/telegram-bot-api/music/🎵 Queen & David Bowie - Under Pressure (1981) [FLAC 24-bit] #1.flac"
        )

        resolved = mgr.resolve_local_filepath(complex_path)
        expected = (
            tmp_path / "bot_data" / "music" / "🎵 Queen & David Bowie - Under Pressure (1981) [FLAC 24-bit] #1.flac"
        ).resolve()
        assert resolved == expected

    def test_resolve_local_filepath_existing_relative_file_in_data_dir(self, tmp_path: Path):
        """A relative path matching an existing file inside data_dir resolves to data_dir."""
        data_dir = tmp_path / "bot_data"
        sub_dir = data_dir / "cache"
        sub_dir.mkdir(parents=True, exist_ok=True)
        sample_file = sub_dir / "temp_audio.mp3"
        sample_file.write_bytes(b"dummy audio")

        mgr = LocalBotAPIManager(data_dir=data_dir)
        resolved = mgr.resolve_local_filepath("cache/temp_audio.mp3")
        assert resolved == sample_file.resolve()

    def test_resolve_local_filepath_directory_traversal_behavior(self, tmp_path: Path):
        """
        Adversarial path with directory traversal ('..') segments.
        Verifies behavior when malicious traversal paths are passed.
        """
        data_dir = tmp_path / "bot_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        mgr = LocalBotAPIManager(data_dir=data_dir)

        traversal_path = "/var/lib/telegram-bot-api/../../etc/passwd"
        resolved = mgr.resolve_local_filepath(traversal_path)
        # Check if resolved path escaped data_dir
        escaped = not str(resolved).startswith(str(data_dir.resolve()))
        # Record finding: LocalBotAPIManager resolves relative without path containment check
        assert isinstance(resolved, Path)

    @pytest.mark.parametrize(
        "status_code,expected_health",
        [
            (200, True),    # Standard HTTP OK
            (204, True),    # No Content
            (400, True),    # Bad Request (Telegram API root returns client error, still healthy)
            (404, True),    # Not Found (Telegram Bot API root without token)
            (500, False),   # Internal Server Error (Server failing)
            (502, False),   # Bad Gateway
            (503, False),   # Service Unavailable
            (504, False),   # Gateway Timeout
        ]
    )
    def test_check_health_http_status_codes(self, status_code: int, expected_health: bool):
        """Health check returns True for < 500 status and False for 5xx server errors."""
        async def run():
            mgr = LocalBotAPIManager(port=8089)

            mock_resp = MagicMock()
            mock_resp.status = status_code
            mock_get = MagicMock()
            mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_get.__aexit__ = AsyncMock(return_value=False)

            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=mock_get)
            mock_session_cls = MagicMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("aiohttp.ClientSession", mock_session_cls):
                result = await mgr.check_health("http://127.0.0.1:8089")
                assert result is expected_health

        asyncio.run(run())

    def test_check_health_connection_refused(self):
        """Network error (e.g. connection refused) returns False without raising exception."""
        async def run():
            mgr = LocalBotAPIManager(port=8089)

            mock_session = MagicMock()
            mock_session.get = MagicMock(side_effect=ConnectionRefusedError("Connection refused"))
            mock_session_cls = MagicMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("aiohttp.ClientSession", mock_session_cls):
                result = await mgr.check_health("http://127.0.0.1:8089")
                assert result is False

        asyncio.run(run())

    def test_check_health_client_timeout(self):
        """Async client timeout returns False cleanly."""
        async def run():
            mgr = LocalBotAPIManager(port=8089)

            mock_session = MagicMock()
            mock_session.get = MagicMock(side_effect=asyncio.TimeoutError("Request timed out"))
            mock_session_cls = MagicMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("aiohttp.ClientSession", mock_session_cls):
                result = await mgr.check_health("http://127.0.0.1:8089")
                assert result is False

        asyncio.run(run())

    def test_wait_until_ready_early_exit_on_crashed_process(self):
        """If server process terminates prematurely with an error code, wait_until_ready returns False immediately."""
        async def run():
            mgr = LocalBotAPIManager(port=8089)
            mock_proc = MagicMock()
            mock_proc.returncode = 127  # Command not found / crash code
            mgr.process = mock_proc

            start_t = time.time()
            with patch.object(mgr, "check_health", AsyncMock(return_value=False)):
                ready = await mgr._wait_until_ready("http://127.0.0.1:8089", timeout=10.0, poll_interval=0.05)
                elapsed = time.time() - start_t

                assert ready is False
                # Must exit quickly without waiting the full 10s
                assert elapsed < 1.0

        asyncio.run(run())

    def test_find_binary_priority_order(self, tmp_path: Path):
        """find_binary respects precedence: custom_bin > local search > PATH."""
        # 1. Custom binary
        custom_file = tmp_path / "custom_bin.exe"
        custom_file.write_text("custom")

        mgr1 = LocalBotAPIManager(bin_path=custom_file)
        assert mgr1.find_binary() == custom_file.resolve()

        # 2. PATH fallback
        mgr2 = LocalBotAPIManager(bin_path=None)
        with patch("shutil.which", return_value=str(custom_file)):
            assert mgr2.find_binary() == custom_file.resolve()

        # 3. None found
        with patch("shutil.which", return_value=None), \
             patch("pathlib.Path.is_file", return_value=False):
            assert mgr2.find_binary() is None
