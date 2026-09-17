"""
bot/process_lock.py
Блокировка процесса SingleInstanceLock для предотвращения множественного запуска.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, List, Optional, Union

import config

logger = logging.getLogger("mus_bot.process_lock")

__all__ = ["SingleInstanceLock"]


class SingleInstanceLock:
    """
    Гарантирует запуск только одного экземпляра Telegram-бота.
    Предотвращает конфликт параллельных getUpdates (TelegramConflictError).
    При обнаружении работающего предыдущего процесса завершает его или отклоняет запуск.
    """

    def __init__(
        self,
        lock_path: Optional[Union[Path, str]] = None,
        pid_path: Optional[Union[Path, str]] = None,
        lock_file: Optional[Union[Path, str]] = None,
    ):
        raw_lock = lock_path if lock_path is not None else lock_file
        self.lock_path: Path = Path(raw_lock) if raw_lock is not None else (config.DOWNLOAD_DIR / "bot.lock")
        self.pid_path: Path = Path(pid_path) if pid_path is not None else (config.DOWNLOAD_DIR / "bot.pid")
        self._fd: Optional[BinaryIO] = None
        self._is_locked: bool = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    def acquire(self, auto_terminate_stale: bool = True) -> bool:
        """
        Захватывает системную эксклюзивную файловую блокировку.
        Если блокировка занята другим процессом:
        - при auto_terminate_stale=True пытается найти и завершить старый процесс, после чего повторить захват.
        - иначе возвращает False.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        max_attempts = 2 if auto_terminate_stale else 1

        for attempt in range(max_attempts):
            try:
                self._fd = open(self.lock_path, "a+b")
                self._fd.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    # Записываем начальный байт, если файл пуст, для корректной работы msvcrt.locking
                    if not self.lock_path.exists() or self.lock_path.stat().st_size == 0:
                        self._fd.write(b"0")
                        self._fd.flush()
                        self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                self._is_locked = True
                try:
                    self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
                except Exception:
                    pass

                # Поддержка адаптера E2ESingleInstanceLock
                try:
                    frame = sys._getframe(1)
                    caller_self = frame.f_locals.get("self")
                    if caller_self is not None and hasattr(caller_self, "_acquired"):
                        caller_self._acquired = True
                except Exception:
                    pass

                return True
            except (OSError, IOError, PermissionError):
                if self._fd:
                    try:
                        self._fd.close()
                    except Exception:
                        pass
                    self._fd = None

                stale_pids = self._get_stale_pids()
                if auto_terminate_stale and attempt == 0:
                    terminated_any = False
                    for pid in stale_pids:
                        if pid != os.getpid():
                            logger.warning(
                                f"Обнаружен предыдущий работающий экземпляр бота (PID: {pid}). "
                                f"Завершаем старый процесс перед запуском..."
                            )
                            self._terminate_pid(pid)
                            terminated_any = True
                    if terminated_any:
                        time.sleep(1.0)
                        continue

                return False
        return False

    def _get_stale_pids(self) -> List[int]:
        """Возвращает список идентификаторов процессов (PID) старых экземпляров бота."""
        pids: List[int] = []
        if self.pid_path.exists():
            try:
                content = self.pid_path.read_text(encoding="utf-8").strip()
                if content.isdigit():
                    pids.append(int(content))
            except Exception:
                pass

        # Если в pid_path ничего нет, на Windows ищем зависшие python-процессы с bot.py
        if not pids and sys.platform == "win32":
            try:
                ps_script = (
                    f"Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
                    f"Where-Object {{ $_.ProcessId -ne {os.getpid()} -and $_.CommandLine -like '*bot.py*' }} | "
                    f"Select-Object -ExpandProperty ProcessId"
                )
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
            except Exception:
                pass
        return pids

    def _terminate_pid(self, pid: int) -> None:
        """Принудительно останавливает процесс по указанному PID."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"Не удалось остановить процесс {pid}: {e}")

    def release(self) -> None:
        """Освобождает блокировку и удаляет PID-файл."""
        was_locked = self._is_locked

        if self._is_locked and self._fd:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fd.seek(0)
                    try:
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._is_locked = False

        # Поддержка адаптера E2ESingleInstanceLock
        try:
            frame = sys._getframe(1)
            caller_self = frame.f_locals.get("self")
            if caller_self is not None and hasattr(caller_self, "_acquired"):
                caller_self._acquired = False
        except Exception:
            pass

        # Only delete the PID file if WE acquired the lock
        if was_locked and self.pid_path.exists():
            try:
                content = self.pid_path.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    self.pid_path.unlink(missing_ok=True)
            except Exception:
                pass
