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
import shutil
from typing import Any, Dict

logger = logging.getLogger("mus_bot.ytdlp")

# Порядок предпочтения: deno (рекомендует yt-dlp), затем node, bun, quickjs.
_JS_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("deno", "deno"),
    ("node", "node"),
    ("bun", "bun"),
    ("quickjs", "qjs"),
)

_warned_missing_runtime = False


def js_runtime_opts() -> Dict[str, Any]:
    """
    Возвращает ``{"js_runtimes": {name: {}}}`` для рантаймов, найденных в PATH.

    Если не найден ни один — пустой dict: yt-dlp останется на дефолтном deno
    и сам предупредит, что часть YouTube-форматов может быть недоступна.
    """
    runtimes = {
        name: {}
        for name, binary in _JS_RUNTIMES
        if shutil.which(binary)
    }
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
