"""
Разрешение обложек с учётом целевого качества аудио.

Политика размеров:
  * FLAC -> 1400x1400 (нативный мастер Deezer, лучшее реально доступное)
  * MP3  -> 768x768  (то, что серверы отдают без интерполяции)

Вместо того чтобы доверять URL, модуль меряет реальные пиксели ответа
(``probe_image_size``) и умеет переписывать размер прямо в ссылке у CDN,
которые это умеют (Deezer, iTunes, Cover Art Archive) — это даёт ровно
нужное разрешение без апскейла и без лишнего места в тегах.
"""

from __future__ import annotations

import io
import re
from typing import Optional

import httpx

# Целевые размеры обложки по качеству аудиофайла.
COVER_SIZE_LOSSLESS = 1400
COVER_SIZE_LOSSY = 768

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Реально достижимые размеры по источникам. Используются для ранжирования,
# когда скачать и промерить кандидата не удалось.
# iTunes формально отдаёт любой размер вплоть до 3000, но выше ~1200 это
# интерполяция, поэтому в ранжировании он стоит ниже нативного мастера Deezer.
SOURCE_MAX_SIZE = {
    "deezer": 1400,
    "itunes": 1200,
    "coverartarchive": 1200,
    "lastfm": 800,
    "youtube": 1280,
    "spotify": 640,
    "soundcloud": 500,
}


def estimate_max_size(url: str) -> int:
    """
    Оценка максимально достижимого разрешения обложки по CDN-ссылке.

    Нужна, чтобы ранжировать кандидатов, которые ещё не скачаны: ссылка
    сама по себе не гарантирует разрешение, но hostname — надёжный признак.
    """
    if not url:
        return 0
    if "dzcdn.net" in url:
        return SOURCE_MAX_SIZE["deezer"]
    if "mzstatic.com" in url:
        return SOURCE_MAX_SIZE["itunes"]
    if "coverartarchive.org" in url:
        return SOURCE_MAX_SIZE["coverartarchive"]
    if "ytimg.com" in url:
        return SOURCE_MAX_SIZE["youtube"]
    if "lastfm" in url or "freetls.fastly.net" in url:
        return SOURCE_MAX_SIZE["lastfm"]
    if "sndcdn.com" in url:
        return SOURCE_MAX_SIZE["soundcloud"]
    if "scdn.co" in url:
        return SOURCE_MAX_SIZE["spotify"]
    return 300


def target_size_for(quality: str) -> int:
    """Целевой размер обложки для указанного качества аудио."""
    return COVER_SIZE_LOSSLESS if (quality or "").upper() == "FLAC" else COVER_SIZE_LOSSY


# --------------------------------------------------------------------------
# Определение реальных размеров изображения без Pillow
# --------------------------------------------------------------------------

def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    """Размер JPEG по маркеру SOFn (не требует декодирования картинки)."""
    i = 2
    n = len(data)
    if n < 4 or data[0:2] != b"\xff\xd8":
        return None
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        # SOF0..SOF15, кроме не-кадровых DHT(c4)/JPG(c8)/DAC(cc)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            if width and height:
                return width, height
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _webp_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    return None


def probe_image_size(data: bytes) -> Optional[tuple[int, int]]:
    """
    Возвращает реальный размер (w, h) изображения по его заголовку.

    Нужна, потому что CDN регулярно отдаёт не то, что написано в URL:
    SoundCloud молча отдаёт 500x500, а Spotify не поддерживает ``?size=``.
    """
    if not data:
        return None
    for parser in (_jpeg_size, _png_size, _gif_size, _webp_size):
        try:
            size = parser(data)
        except Exception:
            size = None
        if size:
            return size
    return None


# --------------------------------------------------------------------------
# Переписывание размера прямо в URL у CDN, которые это умеют
# --------------------------------------------------------------------------

_DEEZER_SIZE_RE = re.compile(r"/\d{2,4}x\d{2,4}(-[0-9a-f]{6}(-\d{1,3}){0,3}\.\w+)?$")
_ITUNES_SIZE_RE = re.compile(r"/\d{2,5}x\d{2,5}(bb)?\.\w+$")


def resize_dzcdn_url(url: str, size: int) -> str:
    """
    Deezer: ``.../images/cover/<hash>/56x56-000000-80-0-0.jpg``
    -> ``.../images/cover/<hash>/1400x1400-000000-80-0-0.jpg``

    Это нативный мастер лейбла, а не интерполяция.
    """
    if not url:
        return url
    return _DEEZER_SIZE_RE.sub(f"/{size}x{size}-000000-80-0-0.jpg", url)


def resize_itunes_url(url: str, size: int) -> str:
    """iTunes: ``.../<hash>/100x100bb.jpg`` -> ``.../<hash>/768x768bb.jpg``."""
    if not url:
        return url
    return _ITUNES_SIZE_RE.sub(f"/{size}x{size}bb.jpg", url)


def resize_coverartarchive_url(url: str, size: int) -> str:
    """
    Cover Art Archive отдаёт только 250 / 500 / 1200, произвольный размер — нет.

    Берём наименьший доступный размер, который не меньше запрошенного, чтобы
    не качать 1200px там, где хватило бы 500px. Если ничего не хватает —
    отдаём максимум.
    """
    if not url:
        return url
    for candidate in (250, 500, 1200):
        if size <= candidate:
            return re.sub(r"-(?:front-)?\d{3,4}$", f"-{candidate}", url)
    return re.sub(r"-(?:front-)?\d{3,4}$", "-1200", url)


def resize_url_for_source(url: str, size: int) -> str:
    """
    Приводит ссылку к желаемому размеру там, где CDN это умеет.

    Для источников без поддержки (YouTube, Last.fm, Spotify, SoundCloud)
    ссылка возвращается как есть — их реальный размер будет измерен позже.
    """
    if not url or not size:
        return url
    if "dzcdn.net" in url:
        return resize_dzcdn_url(url, size)
    if "mzstatic.com" in url:
        return resize_itunes_url(url, size)
    if "coverartarchive.org" in url:
        return resize_coverartarchive_url(url, size)
    return url


def upscale_youtube_thumbnail(url: str) -> list[str]:
    """Варианты превью YouTube от большего к меньшему."""
    if not url or ("i.ytimg.com" not in url and "ytimg.com" not in url):
        return [url] if url else []
    base = url.rsplit("/", 1)[0]
    candidates = [url]
    for name in ("maxresdefault.jpg", "sddefault.jpg", "hq720.jpg", "hqdefault.jpg"):
        variant = f"{base}/{name}"
        if variant not in candidates:
            candidates.append(variant)
    return candidates


# --------------------------------------------------------------------------
# Приведение байтов к квадрату нужного размера
# --------------------------------------------------------------------------

def fit_cover_art(raw_bytes: bytes, target_size: int = 0) -> tuple[bytes, str]:
    """
    Кадрирует обложку в квадрат 1:1 и (при ``target_size`` > 0) масштабирует
    до нужного разрешения.

    Сначала срезаются однородные чёрные поля (letterbox/pillarbox), затем
    выполняется центральный кроп, и только после этого — ресайз. Так
    превью YouTube 16:9 не получает чёрных полос, а SoundCloud 500x500
    не вшивается в тег «как есть».
    """
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw_bytes))
        w, h = im.size

        # Уже квадрат и ресайз не нужен — отдаём как есть.
        square_enough = abs(w - h) <= max(2, int(0.02 * max(w, h)))
        if square_enough and (not target_size or max(w, h) == target_size):
            if im.format in ("JPEG", "JPG"):
                return raw_bytes, "image/jpeg"
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=95)
            return buf.getvalue(), "image/jpeg"

        gray = im.convert("L")

        left = 0
        step_y = max(1, h // 20)
        for x in range(w // 3):
            if max(gray.getpixel((x, py)) for py in range(0, h, step_y)) < 25:
                left = x + 1
            else:
                break

        right = w
        for x in range(w - 1, w - w // 3, -1):
            if max(gray.getpixel((x, py)) for py in range(0, h, step_y)) < 25:
                right = x
            else:
                break

        step_x = max(1, w // 20)
        top = 0
        for y in range(h // 3):
            if max(gray.getpixel((px, y)) for px in range(0, w, step_x)) < 25:
                top = y + 1
            else:
                break

        bottom = h
        for y in range(h - 1, h - h // 3, -1):
            if max(gray.getpixel((px, y)) for px in range(0, w, step_x)) < 25:
                bottom = y
            else:
                break

        if left > 0 or right < w or top > 0 or bottom < h:
            if right - left > 50 and bottom - top > 50:
                im = im.crop((left, top, right, bottom))
                w, h = im.size

        if w != h:
            side = min(w, h)
            im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
            w = h = side

        if target_size and w != target_size:
            # Апскейл делаем только если он небольшой: брать 500 -> 1400
            # бессмысленно, зато 1200 -> 1400 ещё несёт детали.
            if target_size <= w * 2:
                im = im.resize((target_size, target_size), Image.LANCZOS)

        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw_bytes, "image/jpeg"


def download_image(url: str, timeout: float = 12.0) -> tuple[Optional[bytes], Optional[str]]:
    """Скачивает изображение, возвращая (bytes, mime)."""
    if not url:
        return None, None
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=timeout)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content, resp.headers.get("content-type", "image/jpeg")
    except Exception:
        pass
    return None, None
