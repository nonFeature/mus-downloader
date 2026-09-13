import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple, Set, Literal
from ytmusicapi import YTMusic

CONFIDENCE_MIN = 0.6
SEARCH_LIMIT = 10
RATE_LIMIT_S = 0.35
DURATION_TOLERANCE_RATIO = 0.10
SHORT_TRACK_MAX_SECONDS = 45
SHORT_TRACK_TOLERANCE_SECONDS = 8
SEARCH_RETRY_COUNT = 2
SEARCH_RETRY_SLEEP_S = 0.9

_PENALTY_TERMS = {
    "cover", "sped", "slowed", "nightcore", "8d", "reverb",
    "mashup", "karaoke", "instrumental", "demo", "tribute", "soundalike",
    "remx", "rmx"
}

_VERSION_PATTERNS = {
    "live": r"\blive\b",
    "acoustic": r"\bacoustic\b",
    "extended": r"\bextended\b|\blong version\b",
    "remix": r"\bremix(?:ed)?\b|\bmix\b|\bremx\b|\brmx\b",
    "edit": r"\bedit\b|\bradio version\b",
    "single": r"\bsingle version\b|\boriginal single\b|\bu\.?s\.? single\b",
    "remaster": r"\bremaster(?:ed)?\b",
    "album": r"\balbum version\b",
}

_CAST_PENALTY_TERMS = {"cast", "original cast", "tribute band", "musical", "orchestra"}

def norm_text(s: str) -> str:
    """Нормализует строку: Unicode NFKC, casefold, сжатие пробелов."""
    text = unicodedata.normalize("NFKC", (s or "").casefold())
    return re.sub(r"\s+", " ", text).strip()

def toks(s: str) -> Set[str]:
    """Извлекает набор буквенно-цифровых токенов."""
    text = norm_text(s)
    return {tok for tok in re.findall(r"\w+", text, flags=re.UNICODE) if any(ch.isalnum() for ch in tok)}

def _candidate_artist_text(cand: Dict) -> str:
    if cand.get("artists"):
        try:
            return ", ".join(a.get("name", "") for a in cand["artists"] if isinstance(a, dict))
        except Exception:
            pass
    return cand.get("author", "") or cand.get("channel", "") or ""

def _candidate_album_text(cand: Dict) -> str:
    album = cand.get("album")
    if isinstance(album, dict):
        return str(album.get("name") or album.get("title") or "").strip()
    if isinstance(album, str):
        return album.strip()
    return ""

def _version_markers(value: str) -> Set[str]:
    text = norm_text(value)
    return {marker for marker, pattern in _VERSION_PATTERNS.items() if re.search(pattern, text, flags=re.I)}

def _candidate_version_markers(cand: Dict) -> Set[str]:
    return _version_markers(" ".join((str(cand.get("title") or ""), _candidate_album_text(cand))))

def _duration_s(d: Optional[int | float | str]) -> int:
    try:
        if d is None:
            return 0
        if isinstance(d, (int, float)):
            return int(d)
        text = str(d).strip()
        if not text:
            return 0
        if ":" in text:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return total
        return int(float(text))
    except Exception:
        return 0

def _duration_within_tolerance(track_duration_s: int, cand_duration_s: int) -> bool:
    if track_duration_s <= 0 or cand_duration_s <= 0:
        return True
    if track_duration_s <= SHORT_TRACK_MAX_SECONDS:
        return abs(track_duration_s - cand_duration_s) <= SHORT_TRACK_TOLERANCE_SECONDS
    return abs(track_duration_s - cand_duration_s) <= max(1.0, track_duration_s * DURATION_TOLERANCE_RATIO)

def _overlap_ratio(needle: Set[str], haystack: Set[str]) -> float:
    return len(needle & haystack) / max(1, len(needle))

def score_candidate(track: Dict, cand: Dict) -> float:
    """
    Рассчитывает оценку совпадения (0.0 .. 1.0) кандидата с оригинальным треком.
    """
    track_title_tokens = toks(track.get("title", ""))
    track_artist_tokens = toks(track.get("artist", "") or track.get("artists", ""))
    cand_title = cand.get("title") or ""
    cand_art = _candidate_artist_text(cand)
    cand_title_tokens = toks(cand_title)
    cand_artist_tokens = toks(cand_art)

    title_overlap = _overlap_ratio(track_title_tokens, cand_title_tokens)
    artist_overlap = _overlap_ratio(track_artist_tokens, cand_artist_tokens)

    # Оценка длительности
    track_s = _duration_s(track.get("duration") or track.get("duration_seconds") or (track.get("duration_ms", 0) / 1000 if track.get("duration_ms") else 0))
    cand_s = _duration_s(cand.get("duration_seconds") or cand.get("duration"))

    if track_s > 0 and cand_s > 0:
        delta = abs(track_s - cand_s)
        if delta <= 6:
            d_score = 1.0
        elif delta <= 12:
            d_score = 0.9
        elif delta <= 20:
            d_score = 0.78
        elif delta <= 30:
            d_score = 0.62
        else:
            d_score = 0.45
    else:
        d_score = 0.7  # нейтральный базовый уровень, если длительность неизвестна

    # Буст официального канала
    channel = norm_text(cand.get("author") or cand.get("channel") or "")
    ch_boost = 0.15 if ("topic" in channel or "official" in channel) else 0.0

    # Штрафы за нежелательные версии
    titleblob = norm_text(cand_title + " " + cand_art)
    requested_title = norm_text(track.get("title") or "")
    requested_versions = _version_markers(requested_title)
    candidate_versions = _candidate_version_markers(cand)

    p_pen = 0.0
    for t in _PENALTY_TERMS:
        if t in titleblob and t not in requested_title:
            p_pen += 0.20

    if requested_versions:
        for marker in requested_versions - candidate_versions:
            p_pen += 0.08 if marker == "remaster" else 0.34
        for marker in candidate_versions - requested_versions:
            p_pen += 0.03 if marker == "remaster" else 0.26
    else:
        # Если в запросе не было маркеров версий, штрафуем live, remix и т.д.
        p_pen += 0.30 * len(candidate_versions - {"remaster"})

    if artist_overlap == 0.0:
        for t in _CAST_PENALTY_TERMS:
            if t in titleblob:
                p_pen += 0.18
    if "tribute" in titleblob and artist_overlap < 0.5:
        p_pen += 0.25

    version_boost = 0.14 if requested_versions and requested_versions == candidate_versions else 0.0
    total = max(0.0, d_score * 0.35 + title_overlap * 0.35 + artist_overlap * 0.25 + ch_boost + version_boost - p_pen)
    return min(total, 0.99)

def _strip_noise(title: str) -> str:
    s = re.sub(r"[\(\[][^\)\]]*[Ff]eat[^\)\]]*[\)\]]", " ", title)
    s = re.sub(r"[\(\[][Oo]fficial[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\[][Ll]ive[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\[][Rr]emix[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\)\[\]]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _query_variants(track: Dict) -> List[str]:
    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or track.get("artists") or "").strip()
    isrc = track.get("isrc")

    clean_title = _strip_noise(title)
    base = f"{artist} - {title}".strip()
    clean = f"{artist} - {clean_title}".strip()

    variants: List[str] = []
    if isrc:
        variants.append(f"{isrc} {clean}")
    variants.append(base)
    if clean != base:
        variants.append(clean)
    if "&" in base:
        variants.append(base.replace("&", "and"))
    if " and " in base.lower():
        variants.append(re.sub(r"\band\b", "&", base, flags=re.I))

    # Убираем дубликаты сохраняя порядок
    seen = set()
    out = []
    for q in variants:
        qq = re.sub(r"\s+", " ", q).strip()
        if qq and qq not in seen:
            seen.add(qq)
            out.append(qq)
    return out

def _search_ytmusic(yt: YTMusic, query: str, search_filter: str, limit: int) -> List[Dict]:
    try:
        res = yt.search(query, filter=search_filter, limit=limit) or []
    except Exception:
        return []

    cands = []
    source = "music" if search_filter == "songs" else "videos"
    for r in res:
        vid = r.get("videoId")
        if not vid:
            continue
        artists = r.get("artists")
        author = ""
        if artists and isinstance(artists, list):
            author = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
        if not author:
            for k in ("author", "channel", "uploader", "name"):
                val = r.get(k)
                if isinstance(val, str) and val.strip():
                    author = val.strip()
                    break

        duration_val = r.get("duration_seconds") or r.get("duration")
        cands.append({
            "videoId": vid,
            "title": r.get("title", ""),
            "artists": artists if search_filter == "songs" else None,
            "author": author,
            "channel": author,
            "duration_seconds": _duration_s(duration_val),
            "album": r.get("album"),
            "source": source,
        })
    return cands

def find_best_youtube_match(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    album: Optional[str] = None,
    isrc: Optional[str] = None,
    yt_instance: Optional[YTMusic] = None,
) -> Tuple[Optional[Dict], float, List[Dict]]:
    """
    Основная точка входа для умного поиска YouTube Music.
    Возвращает: (best_candidate, best_score, all_scored_candidates)
    Если лучший кандидат имеет score < CONFIDENCE_MIN, best_candidate = None.
    """
    track = {
        "artist": artist,
        "title": title,
        "duration": duration,
        "album": album,
        "isrc": isrc,
    }

    yt = yt_instance or YTMusic()
    track_duration_s = _duration_s(duration)

    seen_vids = set()
    all_cands = []

    for q in _query_variants(track):
        # 1. Поиск по песням (YouTube Music songs)
        songs = _search_ytmusic(yt, q, "songs", limit=6)
        # 2. Поиск по видео (YouTube Music videos)
        videos = _search_ytmusic(yt, q, "videos", limit=4)
        for cand in songs + videos:
            vid = cand["videoId"]
            if vid not in seen_vids:
                seen_vids.add(vid)
                all_cands.append(cand)
        if len(all_cands) >= 12:
            break

    if not all_cands:
        return None, 0.0, []

    scored = []
    for cand in all_cands:
        cand_dur = cand.get("duration_seconds", 0)
        # Если длительность оригинала известна, отбрасываем треки с сильным расхождением
        if track_duration_s > 0 and cand_dur > 0:
            if not _duration_within_tolerance(track_duration_s, cand_dur):
                continue

        score = score_candidate(track, cand)
        cand_copy = dict(cand)
        cand_copy["score"] = score
        scored.append(cand_copy)

    if not scored:
        return None, 0.0, []

    # Сортируем: сначала те, у кого score >= CONFIDENCE_MIN, предпочтение "music" источнику, затем по score
    def sort_key(c):
        s = c["score"]
        is_music = 1 if c.get("source") == "music" else 0
        conf_ok = 1 if s >= CONFIDENCE_MIN else 0
        return (conf_ok, is_music if conf_ok else 0, s)

    scored.sort(key=sort_key, reverse=True)
    best = scored[0]
    best_score = best["score"]

    if best_score >= CONFIDENCE_MIN:
        return best, best_score, scored
    else:
        return None, best_score, scored
