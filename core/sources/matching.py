"""Общий скоринг кандидатов для источников без точного ISRC-пути.

Один и тот же подход для Tidal/Qobuz/Amazon/Beatport/JioSaavn: нормализация
названия/артиста, фильтр фичов и версионных маркеров (remix/live/...),
допуск по длительности. Точное совпадение ISRC всегда побеждает.
"""
import re
import unicodedata
from typing import Optional

from .deezer import is_junk_track
from .youtube_matcher import toks, _version_markers


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def norm_text(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_feat(s: str) -> str:
    """Убирает (feat. X), [ft. X], (with X) и хвостовые ' - feat ...'."""
    s = re.sub(r"(?i)[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^)\]]*[\)\]]", "", s or "")
    s = re.sub(r"(?i)\s+-\s+(?:feat\.?|ft\.?|featuring)\s+.*$", "", s)
    return s.strip()


def normalize_isrc(isrc: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", isrc or "").upper()


def _artist_score(target_artist: str, cand_artist: str) -> float:
    # \b вокруг feat/ft/and обязателен: без него "ft" сматчится внутри "Daft".
    split_re = r"(?i)\s*(?:,|&|\+|/|\bfeat\.?\b|\bft\.?\b|\band\b)\s*"
    target_parts = [p for p in re.split(split_re, target_artist or "") if p.strip()]
    cand_parts = [p for p in re.split(split_re, cand_artist or "") if p.strip()]
    if not target_parts or not cand_parts:
        return 0.0
    target_primary = norm_text(target_parts[0])
    cand_norm = norm_text(" ".join(cand_parts))
    cand_primary = norm_text(cand_parts[0])
    if target_primary and target_primary == cand_primary:
        return 25.0
    if target_primary and target_primary in cand_norm:
        return 25.0
    if cand_primary and cand_primary in norm_text(" ".join(target_parts)):
        return 20.0
    target_toks = toks(" ".join(target_parts))
    cand_toks = toks(" ".join(cand_parts))
    if target_toks and cand_toks:
        overlap = len(target_toks & cand_toks) / max(1, len(target_toks))
        if overlap >= 0.5:
            return 10.0
    return -30.0


def score_candidate(
    *,
    artist: str,
    title: str,
    duration: Optional[float],
    isrc: Optional[str],
    cand_artist: str,
    cand_title: str,
    cand_duration: Optional[float],
    cand_isrc: Optional[str] = None,
) -> float:
    """0 и меньше — кандидат не подходит."""
    if is_junk_track(cand_title, cand_artist, f"{artist} {title}"):
        return -1000.0

    if isrc and cand_isrc and normalize_isrc(isrc) == normalize_isrc(cand_isrc):
        return 1000.0

    target_norm = norm_text(strip_feat(title))
    cand_norm_full = norm_text(cand_title)
    cand_norm = norm_text(strip_feat(cand_title))
    if not target_norm or not cand_norm:
        return -1000.0

    if target_norm == cand_norm:
        score = 60.0
    else:
        t_toks, c_toks = toks(target_norm), toks(cand_norm)
        if t_toks and t_toks <= c_toks:
            score = 45.0
        elif t_toks and c_toks and len(t_toks & c_toks) / len(t_toks) >= 0.8:
            score = 35.0
        else:
            return -1000.0

    score += _artist_score(artist, cand_artist)

    # Версионные маркеры у кандидата, которых нет в запросе — почти всегда
    # чужая версия (remix/live/acoustic).
    target_vm = _version_markers(f"{artist} {title}")
    cand_vm = _version_markers(cand_title)
    extra_markers = cand_vm - target_vm
    if extra_markers:
        score -= 70.0

    if duration and cand_duration and duration > 0 and cand_duration > 0:
        diff = abs(float(duration) - float(cand_duration))
        if diff <= 2:
            score += 15.0
        elif diff <= 4:
            score += 10.0
        elif diff <= 8:
            score += 4.0
        elif diff <= 15:
            score += 0.0
        else:
            score -= 60.0

    return score


def pick_best(
    candidates: list[dict],
    *,
    artist: str,
    title: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    min_score: float = 40.0,
) -> Optional[dict]:
    """Выбирает лучшего кандидата из списков с ключами
    id/artist/title/duration/isrc (duration в секундах)."""
    best, best_score = None, min_score
    for cand in candidates:
        score = score_candidate(
            artist=artist,
            title=title,
            duration=duration,
            isrc=isrc,
            cand_artist=cand.get("artist") or "",
            cand_title=cand.get("title") or "",
            cand_duration=cand.get("duration"),
            cand_isrc=cand.get("isrc"),
        )
        if score > best_score:
            best, best_score = cand, score
    return best
