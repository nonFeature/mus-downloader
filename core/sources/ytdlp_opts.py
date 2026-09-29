"""
core/sources/ytdlp_opts.py
Общие опции yt-dlp для YouTube/SoundCloud.

Современному yt-dlp для YouTube нужны:
1. JS-рантайм (по умолчанию включён только deno; node/bun/quickjs надо
   включать явно) — выбираем автоматически из установленных в системе;
2. EJS-скрипты решателя челленджей — их даёт пакет ``yt-dlp-ejs``
   (зависимость проекта), так что сеть для их загрузки не нужна.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("mus_bot.ytdlp")

# Порядок предпочтения: deno (рекомендует yt-dlp), затем node, bun, quickjs.
_JS_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("deno", "deno"),
    ("node", "node"),
    ("bun", "bun"),
    ("quickjs", "qjs"),
)

_warned_missing_runtime = False


def _find_runtime_binary(name: str, binary: str) -> Optional[str]:
    """
    Ищет бинарный файл JS-рантайма:
    1. В текущем PATH системы.
    2. В стандартных путях установки (~/.deno/bin, ~/.bun/bin, ~/.nvm, /root/.deno/bin).
       Это критично при запуске через systemd, где PATH может быть урезан.
    """
    found = shutil.which(binary)
    if found:
        return found

    candidates = []
    try:
        home = Path.home()
    except Exception:
        home = None

    if name == "deno":
        if home:
            candidates.extend([
                home / ".deno" / "bin" / "deno",
                home / ".deno" / "bin" / "deno.exe",
            ])
        candidates.extend([
            Path("/root/.deno/bin/deno"),
            Path("/usr/local/bin/deno"),
            Path("/usr/bin/deno"),
        ])
        try:
            for p in Path("/home").glob("*/.deno/bin/deno"):
                candidates.append(p)
        except Exception:
            pass

    elif name == "bun":
        if home:
            candidates.extend([
                home / ".bun" / "bin" / "bun",
                home / ".bun" / "bin" / "bun.exe",
            ])
        candidates.extend([
            Path("/root/.bun/bin/bun"),
            Path("/usr/local/bin/bun"),
            Path("/usr/bin/bun"),
        ])

    elif name == "node":
        candidates.extend([
            Path("/usr/local/bin/node"),
            Path("/usr/bin/node"),
        ])
        if home:
            try:
                for p in (home / ".nvm" / "versions" / "node").glob("*/bin/node"):
                    candidates.append(p)
            except Exception:
                pass

    for cand in candidates:
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except Exception:
            continue

    return None


def js_runtime_opts() -> Dict[str, Any]:
    """
    Возвращает ``{"js_runtimes": {name: {"path": path}}}`` для рантаймов, найденных в системе.

    Если не найден ни один — пустой dict: yt-dlp останется на дефолтном deno
    и сам предупредит, что часть YouTube-форматов может быть недоступна.
    """
    runtimes = {}
    for name, binary in _JS_RUNTIMES:
        path = _find_runtime_binary(name, binary)
        if path:
            runtimes[name] = {"path": path}

    if not runtimes:
        global _warned_missing_runtime
        if not _warned_missing_runtime:
            logger.warning(
                "JS-рантайм не найден (deno/node/bun/quickjs). "
                "YouTube-форматы могут быть неполными — установи Deno или Node.js."
            )
            _warned_missing_runtime = True
        return {}
    return {"js_runtimes": runtimes}
