"""
Локальный кэш метаданных (SQLite).

Зачем: разрешение трека упирается в 5-10 сетевых запросов (iTunes, song.link,
MusicBrainz, Last.fm, Deezer, плюс каскад обложек). Повторный запрос того же
трека — это те же запросы по второму разу, плюс риск словить рейтлимит.

Ключи кэширования, от точных к размытым:
  * ``url``      — точная ссылка на трек
  * ``isrc``     — самый надёжный идентификатор релиза
  * ``query``    — нормализованный "Artist - Title"
  * ``release``  — пара (artist, album), для каскада обложек
  * ``deezer``   — пара (artist, title), для поиска deezer_id

Принципы:
  * Кэш не является источником истины. Любая ошибка БД деградирует в
    «кэш пуст» — скачивание обязано работать всегда.
  * Потокобезопасность: sqlite3-соединение нельзя шарить между потоками,
    поэтому на поток — своё соединение + WAL для параллельных читателей.
  * Нормализация ключей использует ту же ``norm_text``, что и скоринг
    в youtube_matcher, иначе "Daft Punk" и "daft  punk" дадут две записи.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import config

DAY_SECONDS = 86400.0
DEFAULT_TTL_DAYS = 30.0
# Обложки меняются крайне редко, держим их заметно дольше трековых записей.
ART_TTL_DAYS = 180.0

KIND_URL = "url"
KIND_ISRC = "isrc"
KIND_QUERY = "query"
KIND_RELEASE = "release"
KIND_DEEZER = "deezer"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);
CREATE INDEX IF NOT EXISTS idx_entries_expires ON entries(expires_at);
"""


# --------------------------------------------------------------------------
# Нормализация ключей
# --------------------------------------------------------------------------

def _norm_text(value: str) -> str:
    """Нормализация текста в том же смысле, что и в скоринге youtube_matcher."""
    try:
        from .sources.youtube_matcher import norm_text

        return norm_text(value or "")
    except Exception:
        return " ".join((value or "").lower().split())


_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
_BY_RE = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", re.IGNORECASE)
# "(feat. X)" — это кредит соучастника, а не другая запись, поэтому
# "Daft Punk - Get Lucky" и "Daft Punk - Get Lucky (feat. Pharrell)"
# должны попасть в одну запись кэша.
_FEAT_RE = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring)\.?\s+[^\)\]]+[\)\]]", re.IGNORECASE)


def normalize_query(query: str) -> str:
    """
    Приводит произвольный запрос к каноническому виду.

    'Daft Punk - Get Lucky', 'daft  punk - get lucky' и
    'Get Lucky by Daft Punk' должны попасть в одну запись кэша.

    Обратите внимание: 'Get Lucky - Daft Punk' — это семантически ДРУГОЙ
    запрос (артист=Get Lucky, название=Daft Punk), поэтому он намеренно
    даёт другой ключ, а не сливается с 'Daft Punk - Get Lucky'.
    """
    raw = (query or "").strip()
    if not raw:
        return ""

    text = _FEAT_RE.sub("", raw)
    text = _SEPARATOR_RE.sub(" - ", text).strip()

    match = _BY_RE.match(text)
    if match:
        return f"{_norm_text(match.group('title'))} {_norm_text(match.group('artist'))}"

    if " - " in text:
        artist, title = text.split(" - ", 1)
        return f"{_norm_text(title)} {_norm_text(artist)}"

    return _norm_text(text)


def normalize_isrc(isrc: str) -> str:
    return (isrc or "").replace("-", "").replace(" ", "").strip().upper()


def url_key(url: str) -> str:
    return hashlib.sha1((url or "").strip().lower().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Кэш
# --------------------------------------------------------------------------

class MetadataCache:
    """
    Потокобезопасный SQLite-кэш. При любой неисправности молча выключается.
    """

    def __init__(self, path: Optional[Path] = None, enabled: Optional[bool] = None):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._broken = False
        self._closed = False

        if enabled is None:
            env_flag = os.getenv("CACHE_ENABLED", "true").strip().lower()
            enabled = env_flag not in ("0", "false", "no", "off")
        self.enabled = bool(enabled)

        if path is None:
            env_path = os.getenv("CACHE_PATH", "").strip()
            path = Path(env_path) if env_path else Path(config.DOWNLOAD_DIR) / ".metadata_cache.db"
        self.path = Path(path)

        if self.enabled:
            self._ensure_schema()

    # -- соединения ------------------------------------------------------

    def _connect(self) -> Optional[sqlite3.Connection]:
        """Соединение на текущий поток. None -> кэш не работает."""
        if self._broken or not self.enabled:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path),
                timeout=5.0,
                # isolation_level=None -> autocommit, мы сами решаем транзакции
                isolation_level=None,
            )
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

    # -- базовые операции ------------------------------------------------

    def get(self, kind: str, key: str) -> Optional[Any]:
        if not self.enabled or not key:
            return None
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT payload, expires_at FROM entries WHERE key = ?",
                (f"{kind}:{key}",),
            ).fetchone()
        except Exception:
            return None

        if not row:
            return None
        payload, expires_at = row
        if expires_at and expires_at < time.time():
            self.delete(kind, key)
            return None
        try:
            return json.loads(payload)
        except Exception:
            self.delete(kind, key)
            return None

    def set(self, kind: str, key: str, value: Any, ttl_days: float = DEFAULT_TTL_DAYS) -> bool:
        if not self.enabled or not key or value is None:
            return False
        conn = self._connect()
        if conn is None:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            # Не всё сериализуется (например, сюрприз-объекты в meta) — не кэшируем.
            return False

        now = time.time()
        expires_at = now + ttl_days * DAY_SECONDS if ttl_days else 0.0
        try:
            with self._lock:
                conn.execute(
                    "INSERT OR REPLACE INTO entries (key, kind, payload, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"{kind}:{key}", kind, payload, now, expires_at),
                )
            return True
        except Exception:
            return False

    def delete(self, kind: str, key: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.execute("DELETE FROM entries WHERE key = ?", (f"{kind}:{key}",))
        except Exception:
            pass

    # -- специализированные хелперы --------------------------------------

    def get_track(self, query: str = "", url: str = "", isrc: str = "") -> Optional[dict]:
        """
        Ищет метаданные трека от точного ключа к размытому.

        Порядок: url -> isrc -> query. Возвращает dict с полем
        ``_cache_key``, чтобы вызывающая сторона знала, чем именно попала
        в кэш (и не положила туда же значение, найденное по другому ключу).
        """
        if url:
            hit = self.get(KIND_URL, url_key(url))
            if hit:
                return hit
        if isrc:
            hit = self.get(KIND_ISRC, normalize_isrc(isrc))
            if hit:
                return hit
        if query:
            hit = self.get(KIND_QUERY, normalize_query(query))
            if hit:
                return hit
        return None

    def set_track(
        self,
        meta: dict,
        query: str = "",
        url: str = "",
        extra_isrc: str = "",
    ) -> None:
        if not isinstance(meta, dict) or not meta:
            return
        isrc = normalize_isrc(meta.get("isrc") or extra_isrc)
        if url:
            self.set(KIND_URL, url_key(url), meta)
        if isrc:
            self.set(KIND_ISRC, isrc, meta)
        if query:
            self.set(KIND_QUERY, normalize_query(query), meta)

    def get_release_art(self, artist: str, album: str, release_id: str = "") -> Optional[str]:
        key = self._release_key(artist, album, release_id)
        if key is None:
            return None
        return self.get(KIND_RELEASE, key)

    def set_release_art(self, artist: str, album: str, art_url: str, release_id: str = "") -> None:
        if not art_url:
            return
        key = self._release_key(artist, album, release_id)
        if key is None:
            return
        self.set(KIND_RELEASE, key, art_url, ttl_days=ART_TTL_DAYS)

    @staticmethod
    def _release_key(artist: str, album: str, release_id: str = "") -> Optional[str]:
        """
        release_id — более специфичный ключ, чем (artist, album): конкретный
        релиз MusicBrainz. Смешивать их в одну запись нельзя, иначе
        альбомный результат перекроет точный, и наоборот.
        """
        if release_id:
            return f"rel:{release_id.strip().lower()}"
        if artist and album:
            return f"al:{_norm_text(artist)}|{_norm_text(album)}"
        return None

    def get_deezer_id(self, artist: str, title: str, album: str = "") -> Optional[str]:
        key = f"{_norm_text(artist)}|{_norm_text(title)}|{_norm_text(album)}"
        return self.get(KIND_DEEZER, key)

    def set_deezer_id(self, artist: str, title: str, album: str, track_id: str) -> None:
        if not track_id:
            return
        key = f"{_norm_text(artist)}|{_norm_text(title)}|{_norm_text(album)}"
        self.set(KIND_DEEZER, key, track_id)

    # -- обслуживание ----------------------------------------------------

    def prune(self) -> int:
        """Удаляет протухшие записи. Возвращает количество удалённых."""
        conn = self._connect()
        if conn is None:
            return 0
        try:
            with self._lock:
                cursor = conn.execute("DELETE FROM entries WHERE expires_at > 0 AND expires_at < ?", (time.time(),))
                return cursor.rowcount or 0
        except Exception:
            return 0

    def clear(self) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with self._lock:
                conn.execute("DELETE FROM entries")
        except Exception:
            pass

    def stats(self) -> dict:
        conn = self._connect()
        if conn is None:
            return {"enabled": self.enabled, "entries": 0, "by_kind": {}, "healthy": False}
        try:
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            rows = conn.execute("SELECT kind, COUNT(*) FROM entries GROUP BY kind").fetchall()
            return {
                "enabled": self.enabled,
                "healthy": not self._broken,
                "path": str(self.path),
                "entries": total,
                "by_kind": {kind: count for kind, count in rows},
            }
        except Exception:
            return {"enabled": self.enabled, "healthy": False, "entries": 0, "by_kind": {}}

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


# Глобальный синглтон. Создаётся лениво, чтобы тесты могли подменить
# DOWNLOAD_DIR до первого обращения.
_cache: Optional[MetadataCache] = None
_cache_lock = threading.Lock()


def get_cache() -> MetadataCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = MetadataCache()
    return _cache


def reset_cache() -> None:
    """Сбрасывает синглтон. Используется тестами и при смене DOWNLOAD_DIR."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            _cache.close()
        _cache = None
