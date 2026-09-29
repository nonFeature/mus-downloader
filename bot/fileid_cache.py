"""
Кэш Telegram ``file_id``: мгновенная повторная отдача уже отправленного трека.

Зачем: повторный запрос одного и того же трека другим пользователем сейчас
стоит полного цикла — поиск метаданных, скачивание, перекодирование ffmpeg и
загрузка десятков мегабайт в Telegram. При этом Telegram хранит файл у себя
навсегда и готов отдать его по ``file_id`` бесплатно и мгновенно.

Ключи (от точного к размытому, как в ``core.cache``):
  * ``isrc:<ISRC>:<quality>``             — самый надёжный идентификатор релиза
  * ``track:<norm artist - title>:<q>``  — переживает смену ссылки на трек
  * ``url:<sha1(url)>:<quality>``         — для прямых ссылок

Качество входит в ключ обязательно: FLAC и MP3 — разные файлы, и отдавать
MP3 тому, кто просил FLAC, нельзя.

Кэш не является источником истины: Telegram в любой момент может признать
``file_id`` недействительным (сменился токен бота, файл удалён). Тогда запись
удаляется и происходит прозрачный переход к обычному скачиванию.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import config
from core.cache import normalize_isrc, normalize_query, url_key

DAY_SECONDS = 86400.0
# file_id на серверах Telegram живёт очень долго, но не вечно.
DEFAULT_TTL_DAYS = 365.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_ids (
    key        TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    quality    TEXT NOT NULL,
    performer  TEXT,
    title      TEXT,
    duration   INTEGER,
    created_at REAL NOT NULL,
    used_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_ids_quality ON file_ids(quality);
"""


def build_keys(meta: dict, quality: str, query: str = "", url: str = "") -> list[str]:
    """
    Строит ключи от сильного к слабому.

    Возвращается список, потому что при сохранении хочется записать трек сразу
    по нескольким ключам, а при поиске — проверить их по порядку.
    """
    quality = (quality or "MP3").upper()
    isrc = normalize_isrc((meta or {}).get("isrc") or "")
    keys: list[str] = []

    if isrc:
        keys.append(f"isrc:{isrc}:{quality}")

    artist = (meta or {}).get("artist") or ""
    title = (meta or {}).get("title") or ""
    if artist and title:
        keys.append(f"track:{normalize_query(f'{artist} - {title}')}:{quality}")
    elif query:
        keys.append(f"track:{normalize_query(query)}:{quality}")

    if url:
        keys.append(f"url:{url_key(url)}:{quality}")
    elif (meta or {}).get("source_url"):
        keys.append(f"url:{url_key(meta['source_url'])}:{quality}")

    return list(dict.fromkeys(k for k in keys if not k.endswith(":")))


class FileIdCache:
    """
    Потокобезопасное хранилище ``file_id``. Любая ошибка БД деградирует
    в «кэш пуст» — бот обязан работать и без кэша.
    """

    def __init__(self, path: Optional[Path] = None, enabled: Optional[bool] = None):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._broken = False

        if enabled is None:
            flag = os.getenv("FILEID_CACHE_ENABLED", "true").strip().lower()
            enabled = flag not in ("0", "false", "no", "off")
        self.enabled = bool(enabled)

        if path is None:
            env_path = os.getenv("FILEID_CACHE_PATH", "").strip()
            path = (
                Path(env_path)
                if env_path
                else Path(config.DOWNLOAD_DIR) / ".fileid_cache.db"
            )
        self.path = Path(path)

        if self.enabled:
            self._ensure_schema()

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._broken or not self.enabled:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            return conn
        except Exception:
            self._broken = True
            return None

    def _ensure_schema(self) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.executescript(_SCHEMA)
        except Exception:
            self._broken = True

    # -- чтение / запись -------------------------------------------------

    def lookup(self, meta: dict, quality: str, query: str = "", url: str = "") -> Optional[dict]:
        """Возвращает запись кэша по первому подошедшему ключу, иначе None."""
        if not self.enabled:
            return None
        conn = self._connect()
        if conn is None:
            return None

        for key in build_keys(meta, quality, query, url):
            try:
                row = conn.execute(
                    "SELECT file_id, performer, title, duration, used_at "
                    "FROM file_ids WHERE key = ?",
                    (key,),
                ).fetchone()
            except Exception:
                return None
            if not row:
                continue

            file_id, performer, song_title, duration, used_at = row
            # Продлеваем жизнь записи: регулярно используемый file_id
            # не должен выпасть из кэша.
            try:
                with self._lock:
                    conn.execute("UPDATE file_ids SET used_at = ? WHERE key = ?", (time.time(), key))
            except Exception:
                pass

            return {
                "file_id": file_id,
                "performer": performer or "",
                "title": song_title or "",
                "duration": int(duration or 0),
                "key": key,
            }
        return None

    def save(
        self,
        file_id: str,
        meta: dict,
        quality: str,
        query: str = "",
        url: str = "",
        performer: str = "",
        title: str = "",
        duration: int = 0,
        ttl_days: float = DEFAULT_TTL_DAYS,
    ) -> int:
        """Записывает file_id по всем подходящим ключам. Возвращает их число."""
        if not self.enabled or not file_id:
            return 0
        conn = self._connect()
        if conn is None:
            return 0

        now = time.time()
        performer = performer or (meta or {}).get("artist") or ""
        title = title or (meta or {}).get("title") or ""
        try:
            duration = int(duration or (meta or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0

        keys = build_keys(meta, quality, query, url)
        if not keys:
            return 0

        try:
            with self._lock:
                conn.executemany(
                    "INSERT OR REPLACE INTO file_ids "
                    "(key, file_id, quality, performer, title, duration, created_at, used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            key,
                            file_id,
                            (quality or "MP3").upper(),
                            performer,
                            title,
                            duration,
                            now,
                            now,
                        )
                        for key in keys
                    ],
                )
        except Exception:
            return 0
        return len(keys)

    def invalidate(self, meta: dict, quality: str, query: str = "", url: str = "") -> int:
        """Удаляет записи по всем ключам трека. Вызывается, когда Telegram
        признал file_id недействительным."""
        conn = self._connect()
        if conn is None:
            return 0
        keys = build_keys(meta, quality, query, url)
        if not keys:
            return 0
        try:
            with self._lock:
                cursor = conn.executemany(
                    "DELETE FROM file_ids WHERE key = ?", [(k,) for k in keys]
                )
                return cursor.rowcount or 0
        except Exception:
            return 0

    def invalidate_key(self, key: str) -> None:
        conn = self._connect()
        if conn is None or not key:
            return
        try:
            with self._lock:
                conn.execute("DELETE FROM file_ids WHERE key = ?", (key,))
        except Exception:
            pass

    def prune(self, ttl_days: float = DEFAULT_TTL_DAYS) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        cutoff = time.time() - ttl_days * DAY_SECONDS
        try:
            with self._lock:
                cursor = conn.execute("DELETE FROM file_ids WHERE used_at < ?", (cutoff,))
                return cursor.rowcount or 0
        except Exception:
            return 0

    def clear(self) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.execute("DELETE FROM file_ids")
        except Exception:
            pass

    def stats(self) -> dict:
        conn = self._connect()
        if conn is None:
            return {"enabled": self.enabled, "healthy": False, "entries": 0, "by_quality": {}}
        try:
            total = conn.execute("SELECT COUNT(*) FROM file_ids").fetchone()[0]
            rows = conn.execute(
                "SELECT quality, COUNT(*) FROM file_ids GROUP BY quality"
            ).fetchall()
            return {
                "enabled": self.enabled,
                "healthy": not self._broken,
                "path": str(self.path),
                "entries": total,
                "by_quality": {q: c for q, c in rows},
            }
        except Exception:
            return {"enabled": self.enabled, "healthy": False, "entries": 0, "by_quality": {}}

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


_cache: Optional[FileIdCache] = None
_cache_lock = threading.Lock()


def get_file_id_cache() -> FileIdCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = FileIdCache()
    return _cache


def reset_file_id_cache() -> None:
    """Сбрасывает синглтон. Используется тестами."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            _cache.close()
        _cache = None


# --------------------------------------------------------------------------
# Диагностика ошибок Telegram
# --------------------------------------------------------------------------

# Признаки того, что Telegram отверг именно file_id, а не что-то другое.
# Флуд-контрол, «chat not found» и прочие сюда НЕ входят: они не делают
# запись в кэше бессмысленной.
_INVALID_FILE_ID_MARKERS = (
    "file_id",
    "file reference",
    "wrong file identifier",
    "invalid file",
    "file not found",
    "media is not found",
    "type of file mismatch",
    "failed to get http url content",
)


def is_invalid_file_id_error(error: BaseException) -> bool:
    """
    Отличает «file_id протух» от прочих ошибок отправки.

    Важно: при неверном file_id нужно молча перекачать и обновить кэш,
    а не показывать пользователю ошибку и не удалять запись на ровном месте.
    """
    text = str(error or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _INVALID_FILE_ID_MARKERS)
