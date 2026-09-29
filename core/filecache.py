"""
Локальный кэш скачанных аудиофайлов.

Зачем: повторный ``dl "Artist - Track"`` сейчас заново ходит по всем
источникам, качает мегабайты и перекодирует. Для CLI это чистая потеря
времени: файл уже лежит на диске, но найти его можно только по имени,
а имена у разных источников разные ("Artist - Track.mp3",
"18 - Artist - Track.mp3", "Artist - Track (feat. X).flac").

Поэтому индекс хранится в SQLite: ключи те же, что у кэша метаданных
(ISRC / нормализованное название / хеш ссылки), а значение - путь к файлу
и его размер. Файл лежит в отдельной папке под каноническим именем, поэтому
индекс переживает переименования и переезды папки.

Кэш не является источником истины: запись, чей файл исчез или обрёзался,
считается промахом.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import config
from core.cache import normalize_isrc, normalize_query, url_key

CACHE_DIRNAME = ".cache_audio"
DAY_SECONDS = 86400.0

# Версия формата кэша. Поднимается вручную, когда меняется то, что
# записывается: правила канонического имени, набор полей, логика подбора
# источника. Иначе пользователь будет месяцами получать из кэша файлы,
# собранные по старым правилам, и не сможет понять почему.
CACHE_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_files (
    key        TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    quality    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    size       INTEGER,
    ext        TEXT,
    created_at REAL NOT NULL,
    used_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_quality ON audio_files(quality);
"""

# Символы, которые нельзя пускать в имя файла на Windows.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Хвосты, которые стоит выкинуть из названия: версии, ремиксы, feat.
# они делают имя длинным, но не различают треки для пользователя.
_NOISE_TAIL = re.compile(
    r"\s*[\(\[]\s*(?:feat|ft|featuring|remaster\w*|explicit|clean|"
    r"radio\s*edit|extended|mix|version|live|mono|stereo|"
    r"deluxe|bonus\s*track|instrumental)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


def sanitize_filename(value: str, max_length: int = 120) -> str:
    """Приводит строку к безопасному имени файла."""
    value = _UNSAFE.sub("_", (value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value or "Unknown"


def canonical_name(meta: dict, ext: str) -> str:
    """
    Каноническое имя трека в кэше: ``Artist - Title.ext``.

    Из хвоста названия выкидываются пометки версий и feat., потому что
    они не различают трек, но засоряют имя.
    """
    artist = sanitize_filename((meta or {}).get("artist") or "")
    title = _NOISE_TAIL.sub("", (meta or {}).get("title") or "")
    title = sanitize_filename(title)
    ext = (ext or ".mp3").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"{artist} - {title}{ext}"


def build_keys(meta: dict, quality: str, query: str = "", url: str = "") -> list[str]:
    quality = (quality or "MP3").upper()
    keys: list[str] = []

    isrc = normalize_isrc((meta or {}).get("isrc") or "")
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


class AudioFileCache:
    """
    Потокобезопасный кэш файлов. Любая ошибка деградирует в «кэш пуст» —
    скачивание обязано работать всегда.
    """

    def __init__(self, root: Optional[Path] = None, enabled: Optional[bool] = None):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._broken = False

        if enabled is None:
            flag = os.getenv("FILE_CACHE_ENABLED", "true").strip().lower()
            enabled = flag not in ("0", "false", "no", "off")
        self.enabled = bool(enabled)

        if root is None:
            env_root = os.getenv("FILE_CACHE_DIR", "").strip()
            root = (
                Path(env_root)
                if env_root
                else Path(config.DOWNLOAD_DIR) / CACHE_DIRNAME
            )
        self.root = Path(root)

        if self.enabled:
            self._ensure_schema()

    # -- БД --------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.root / "index.db"

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._broken or not self.enabled:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
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
            return
        self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """
        ``CREATE TABLE IF NOT EXISTS`` не добавляет новые колонки, поэтому
        база прошлой версии молча сломает INSERT. Пересоздаём при расхождении.
        Старые файлы при этом остаются на диске - чистить их должен
        ``clear()``, мы не знаем, нужны ли они.
        """
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(audio_files)")}
        except Exception:
            return
        if not columns or "version" in columns:
            return
        try:
            with self._lock:
                conn.execute("DROP TABLE IF EXISTS audio_files")
                conn.executescript(_SCHEMA)
        except Exception:
            self._broken = True

    def _is_inside_cache(self, path: Path) -> bool:
        try:
            return path.resolve().parent == self.root.resolve()
        except OSError:
            return False

    # -- лимит места ----------------------------------------------------

    def max_bytes(self) -> int:
        """Потолок кэша в байтах. 0 = без ограничения."""
        raw = os.getenv("FILE_CACHE_MAX_MB", "2048").strip()
        try:
            return int(float(raw) * 1024 * 1024)
        except (TypeError, ValueError):
            return 2048 * 1024 * 1024

    def max_age_days(self) -> int:
        """
        Сколько дней файл может лежать без запросов, прежде чем быть удалённым.

        Заменяет чистку вручную: то, что месяц никто не просил, мёртвый груз.
        0 = не удалять по возрасту (тогда работает только лимит по размеру).
        """
        raw = os.getenv("FILE_CACHE_MAX_AGE_DAYS", "30").strip()
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 30

    def prune_expired(self, days: Optional[int] = None) -> int:
        """
        Удаляет файлы, к которым не обращались дольше срока. Возвращает число
        удалённых файлов. Записи из индекса тоже удаляются, иначе они будут
        висеть вечно, попадая в проверки размера.
        """
        age_days = self.max_age_days() if days is None else days
        if age_days <= 0:
            return 0
        conn = self._connect()
        if conn is None:
            return 0

        cutoff = time.time() - age_days * DAY_SECONDS
        try:
            rows = conn.execute(
                "SELECT key, path FROM audio_files WHERE used_at < ?", (cutoff,)
            ).fetchall()
        except Exception:
            return 0

        removed = 0
        for key, raw_path in rows:
            try:
                target = Path(raw_path)
                if target.is_file():
                    target.unlink()
            except OSError:
                # Файла уже нет - запись всё равно чистим.
                pass
            try:
                with self._lock:
                    conn.execute("DELETE FROM audio_files WHERE key = ?", (key,))
                removed += 1
            except Exception:
                pass
        return removed

    def enforce_limit(self, keep_key: str = "") -> int:
        """
        Вытесняет наименее недавно использованные файлы, пока кэш не влез
        в лимит. Возвращает число удалённых файлов.

        Без этого кэш на VPS рано или поздно съест диск: FLAC - это
        30-100 МБ, и бот с ``-q FLAC`` растёт быстро.
        """
        limit = self.max_bytes()
        if limit <= 0:
            return 0
        conn = self._connect()
        if conn is None:
            return 0

        try:
            current = self.total_size_bytes()
        except Exception:
            return 0
        if current <= limit:
            return 0

        try:
            rows = conn.execute(
                "SELECT key, path, size FROM audio_files ORDER BY used_at ASC"
            ).fetchall()
        except Exception:
            return 0

        removed = 0
        freed = 0
        for key, raw_path, size in rows:
            if current - freed <= limit:
                break
            if keep_key and key == keep_key:
                # Только что скачанный файл не выгоняем: если он один и
                # больше лимита, пусть лежит - иначе пользователь только что
                # скачал трек и тут же потерял бы его.
                continue
            target = Path(raw_path)
            actual = 0
            try:
                if target.is_file():
                    actual = target.stat().st_size
                    target.unlink()
            except OSError:
                # Файла уже нет - запись всё равно чистим, иначе она будет
                # висеть в индексе, вечно упираясь в проверку размера.
                actual = size or 0
            freed += actual
            try:
                with self._lock:
                    conn.execute("DELETE FROM audio_files WHERE key = ?", (key,))
                removed += 1
            except Exception:
                pass
        return removed

    # -- чтение ----------------------------------------------------------

    def lookup(
        self, meta: dict, quality: str, query: str = "", url: str = ""
    ) -> Optional[Path]:
        """
        Возвращает путь к кэшированному файлу, если он есть и цел.

        Файл считается целым, если он существует и его размер совпадает с
        записанным: обрезанный файл после прерванной закачки нельзя отдавать.
        """
        if not self.enabled:
            return None
        conn = self._connect()
        if conn is None:
            return None

        for key in build_keys(meta, quality, query, url):
            try:
                row = conn.execute(
                    "SELECT path, size, version FROM audio_files WHERE key = ?", (key,)
                ).fetchone()
            except Exception:
                return None
            if not row:
                continue

            raw_path, recorded_size, version = row
            if version != CACHE_VERSION:
                # Формат записи изменился - старая запись недействительна.
                self.forget(key)
                continue

            candidate = Path(raw_path)
            if not candidate.is_file():
                self.forget(key)
                continue
            try:
                actual = candidate.stat().st_size
            except OSError:
                self.forget(key)
                continue
            if recorded_size and actual != recorded_size:
                self.forget(key)
                continue

            try:
                with self._lock:
                    conn.execute("UPDATE audio_files SET used_at = ? WHERE key = ?", (time.time(), key))
            except Exception:
                pass
            return candidate

        return None

    # -- запись ----------------------------------------------------------

    def store(
        self,
        source: Path,
        meta: dict,
        quality: str,
        query: str = "",
        url: str = "",
        move: bool = False,
    ) -> Optional[Path]:
        """
        Кладёт скачанный файл в кэш и записывает индекс.

        ``move=True`` переносит файл (используется ботом, которому файл
        всё равно не нужен на диске). По умолчанию копируется, чтобы
        исходный файл в папке пользователя остался на месте.
        """
        if not self.enabled or not source or not Path(source).is_file():
            return None
        source = Path(source)
        if self._is_inside_cache(source):
            # Уже внутри кэша — второй раз копировать незачем.
            return source

        conn = self._connect()
        if conn is None:
            return None

        try:
            size = source.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None

        target = self.root / canonical_name(meta, source.suffix)
        try:
            if target.resolve() == source.resolve():
                pass
            else:
                if move:
                    shutil.move(str(source), str(target))
                else:
                    shutil.copy2(str(source), str(target))
        except (OSError, shutil.Error):
            return None

        try:
            actual_size = target.stat().st_size
        except OSError:
            return None

        keys = build_keys(meta, quality, query, url)
        if not keys:
            return target

        now = time.time()
        try:
            with self._lock:
                conn.executemany(
                    "INSERT OR REPLACE INTO audio_files "
                    "(key, path, quality, version, size, ext, created_at, used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            k,
                            str(target),
                            (quality or "MP3").upper(),
                            CACHE_VERSION,
                            actual_size,
                            target.suffix,
                            now,
                            now,
                        )
                        for k in keys
                    ],
                )
        except Exception:
            return target
        # Первый записанный ключ - самый точный, по нему и защищаем файл
        # от вытеснения.
        self._maybe_prune()
        self.enforce_limit(keep_key=keys[0])
        return target

    _last_prune_at: float = 0.0

    def _maybe_prune(self) -> None:
        """
        Чистка по возрасту не на каждом сохранении: полный проход по индексу
        на каждом треке - лишняя работа. Раз в час достаточно.
        """
        now = time.time()
        if now - self._last_prune_at < 3600:
            return
        self._last_prune_at = now
        try:
            self.prune_expired()
        except Exception:
            pass

    def forget(self, key: str) -> None:
        conn = self._connect()
        if conn is None or not key:
            return
        try:
            with self._lock:
                conn.execute("DELETE FROM audio_files WHERE key = ?", (key,))
        except Exception:
            pass

    # -- обслуживание ----------------------------------------------------

    def clear(self, remove_files: bool = True) -> int:
        """Очищает индекс и (по умолчанию) сами файлы. Возвращает их число."""
        conn = self._connect()
        if remove_files and self.root.exists():
            removed = 0
            for item in self.root.iterdir():
                if item.name == "index.db" or item.name.startswith("index.db-"):
                    continue
                try:
                    if item.is_file():
                        item.unlink()
                        removed += 1
                except OSError:
                    continue
        else:
            removed = 0
        if conn is not None:
            try:
                with self._lock:
                    conn.execute("DELETE FROM audio_files")
            except Exception:
                pass
        return removed

    def total_size_bytes(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            row = conn.execute("SELECT SUM(size) FROM audio_files").fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    def stats(self) -> dict:
        conn = self._connect()
        if conn is None:
            return {"enabled": self.enabled, "healthy": False, "entries": 0, "bytes": 0}
        try:
            total = conn.execute("SELECT COUNT(*) FROM audio_files").fetchone()[0]
            return {
                "enabled": self.enabled,
                "healthy": not self._broken,
                "path": str(self.root),
                "entries": total,
                "bytes": self.total_size_bytes(),
            }
        except Exception:
            return {"enabled": self.enabled, "healthy": False, "entries": 0, "bytes": 0}

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


_cache: Optional[AudioFileCache] = None
_cache_lock = threading.Lock()


def get_file_cache() -> AudioFileCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = AudioFileCache()
    return _cache


def reset_file_cache() -> None:
    """Сбрасывает синглтон. Используется тестами."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            _cache.close()
        _cache = None
