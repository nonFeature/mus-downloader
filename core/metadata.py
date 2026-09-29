import html
import httpx
import re
import urllib.parse
from typing import Optional
import config

from . import coverart
from . import cache as cache_mod

USER_AGENT = config.BROWSER_USER_AGENT
# MusicBrainz и Cover Art Archive (проект MetaBrainz) требуют
# идентифицирующий User-Agent с контактом и режут браузерные.
MB_USER_AGENT = config.build_user_agent("(+metadata)")

def resolve_spotify_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека напрямую со страницы Spotify."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=3.5)
        if r.status_code != 200:
            return None
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', r.text)
        image_m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        
        title = html.unescape(title_m.group(1)) if title_m else ""
        desc = html.unescape(desc_m.group(1)) if desc_m else ""
        art = image_m.group(1) if image_m else ""
        
        parts = [p.strip() for p in desc.split("·")]
        artist = html.unescape(parts[0]) if parts else ""
        album = html.unescape(parts[1]) if len(parts) > 2 else ""
        year = parts[-1] if len(parts) > 1 and parts[-1].isdigit() else None
        
        if title and artist:
            return {
                "title": title,
                "artist": artist,
                "album": album,
                "year": year,
                "album_art": art,
                "spotify_url": str(r.url)
            }
    except Exception as e:
        print(f"[!] Spotify: {e}")
    return None

def resolve_apple_music_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека напрямую из Apple Music / iTunes."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=4.0)
        final_url = str(r.url)
        m = re.search(r"[?&]i=(\d+)", final_url)
        track_id = m.group(1) if m else None
        if not track_id:
            m2 = re.search(r"/album/[^/]+/(\d+)", final_url)
            track_id = m2.group(1) if m2 else None
        if track_id:
            lookup = httpx.get(f"https://itunes.apple.com/lookup?id={track_id}", timeout=4.0)
            if lookup.status_code == 200:
                res = lookup.json().get("results", [])
                if res:
                    item = res[0]
                    art = item.get("artworkUrl100", "").replace("100x100bb.jpg", "1000x1000bb.jpg")
                    return {
                        "title": item.get("trackName") or item.get("collectionName"),
                        "artist": item.get("artistName"),
                        "album": item.get("collectionName"),
                        "year": (item.get("releaseDate") or "")[:4],
                        "album_art": art,
                        "track_number": item.get("trackNumber"),
                        "track_total": item.get("trackCount"),
                        "duration": (item.get("trackTimeMillis", 0) / 1000.0) if item.get("trackTimeMillis") else None
                    }
    except Exception as e:
        print(f"[!] Apple Music: {e}")
    return None

def resolve_deezer_track(url: str) -> Optional[dict]:
    """Извлекает метаданные напрямую из Deezer API."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=3.5)
        m = re.search(r"track/(\d+)", str(r.url))
        if m:
            track_id = m.group(1)
            meta = httpx.get(f"https://api.deezer.com/track/{track_id}", timeout=3.5).json()
            if "error" not in meta:
                album = meta.get("album", {})
                return {
                    "title": meta.get("title"),
                    "artist": meta.get("artist", {}).get("name"),
                    "album": album.get("title"),
                    "year": (meta.get("release_date") or "")[:4],
                    "album_art": album.get("cover_xl") or album.get("cover_big"),
                    "track_number": meta.get("track_position"),
                    "duration": meta.get("duration"),
                    "isrc": meta.get("isrc"),
                    "deezer_id": track_id
                }
    except Exception:
        pass
    return None

def resolve_yandex_music_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека со страницы Яндекс.Музыки."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=4.0)
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        art_m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        musician_m = re.search(r'<meta name="music:musician_description" content="([^"]+)"', r.text)
        title = title_m.group(1) if title_m else ""
        artist = musician_m.group(1) if musician_m else ""
        art = art_m.group(1) if art_m else ""
        if " — " in title and not artist:
            parts = title.split(" — ", 1)
            title = parts[0].strip()
            artist = parts[1].strip()
        if title:
            return {
                "title": title,
                "artist": artist,
                "album_art": art
            }
    except Exception as e:
        print(f"[!] Яндекс Музыка: {e}")
    return None

def unshorten_url(url: str) -> str:
    """Разворачивает сокращенные ссылки (on.soundcloud.com, spotify.link, deezer.page.link и т.д.)."""
    short_domains = ["on.soundcloud.com", "spotify.link", "deezer.page.link", "t.co", "bit.ly", "tinyurl.com"]
    if any(d in url.lower() for d in short_domains):
        try:
            with httpx.Client(follow_redirects=True, timeout=3.0) as client:
                r = client.head(url)
                return str(r.url)
        except Exception:
            pass
    return url

# resolve_soundcloud_track реализована ниже вместе с resolve_youtube_track

def resolve_direct_streaming_link(url: str) -> Optional[dict]:
    """Разрешает метаданные напрямую из ссылки на стриминговый сервис."""
    url = unshorten_url(url)
    url_lower = url.lower()
    if "spotify.com" in url_lower:
        res = resolve_spotify_track(url)
        if res: return res
    elif "apple.com" in url_lower:
        res = resolve_apple_music_track(url)
        if res: return res
    elif "deezer.com" in url_lower or "deezer.page.link" in url_lower:
        res = resolve_deezer_track(url)
        if res: return res
    elif "music.yandex" in url_lower or "yandex.ru/album" in url_lower:
        res = resolve_yandex_music_track(url)
        if res: return res
    elif "soundcloud.com" in url_lower:
        res = resolve_soundcloud_track(url)
        if res: return res
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        res = resolve_youtube_track(url)
        if res: return res
    elif "last.fm" in url_lower:
        res = resolve_lastfm_track(url)
        if res: return res
    elif "vk.com" in url_lower or "vk.ru" in url_lower:
        res = resolve_vk_track(url)
        if res: return res
    return None

def _parse_odesli_api_data(data: dict) -> Optional[dict]:
    """Парсит ответ официального API Odesli/Songlink."""
    entities = data.get("entitiesByUniqueId", {})
    if not entities:
        return None

    entity_id = data.get("entityUniqueId")
    entity = entities.get(entity_id) if entity_id else next(iter(entities.values()))
    if not entity:
        return None

    result = {
        "title": entity.get("title", ""),
        "artist": entity.get("artistName", ""),
        "album": entity.get("albumName"),
        "album_art": entity.get("thumbnailUrl", ""),
        "year": None,
        "isrc": None,
        "duration": None,
        "track_number": None,
        "explicit": False,
        "deezer_id": None,
        "spotify_url": None,
        "youtube_music_url": None,
        "apple_music_url": None,
        "soundcloud_url": None,
        "yandex_url": None,
    }

    for uid, ent in entities.items():
        prov = ent.get("apiProvider")
        if ent.get("type") == "song" and prov == "deezer" and ent.get("id"):
            result["deezer_id"] = str(ent.get("id"))
            break

    links_by_plat = data.get("linksByPlatform", {})
    for plat, p_info in links_by_plat.items():
        p_url = p_info.get("url") if isinstance(p_info, dict) else None
        if not p_url:
            continue
        if plat == "deezer" and not result.get("deezer_id"):
            m_dz = re.search(r"track/(\d+)", p_url)
            if m_dz:
                result["deezer_id"] = m_dz.group(1)
        elif plat == "spotify" and not result.get("spotify_url"):
            result["spotify_url"] = p_url
        elif plat in ("youtubeMusic", "youtube") and not result.get("youtube_music_url"):
            result["youtube_music_url"] = p_url
        elif plat in ("appleMusic", "itunes") and not result.get("apple_music_url"):
            result["apple_music_url"] = p_url
        elif plat == "soundcloud" and not result.get("soundcloud_url"):
            result["soundcloud_url"] = p_url
        elif plat == "yandex" and not result.get("yandex_url"):
            result["yandex_url"] = p_url

    return result if (result.get("title") and result.get("artist")) else None


def resolve_song_link(url: str) -> Optional[dict]:
    """
    Запрашивает song.link (Odesli) и извлекает связи между платформами и метаданные.
    Поддерживает ссылки с Яндекс.Музыки, SoundCloud, Spotify, Apple Music, Deezer, Tidal, YouTube и др.
    
    1. При наличии SONGLINK_API_KEY использует официальный REST API Odesli.
    2. Если ключ не задан, использует веб-скрейпинг Next.js payload (__NEXT_DATA__),
       что позволяет работать бесплатно без регистрации и ограничений API.
    """
    url = unshorten_url(url)

    # 1. При наличии API-ключа используем официальный REST API Odesli
    songlink_key = getattr(config, "SONGLINK_API_KEY", "") or ""
    if songlink_key:
        try:
            encoded_url = urllib.parse.quote(url)
            api_url = f"https://api.song.link/v1-alpha.1/links?url={encoded_url}&key={songlink_key}"
            resp = httpx.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=8.0)
            if resp.status_code == 200:
                parsed = _parse_odesli_api_data(resp.json())
                if parsed:
                    return parsed
        except Exception as e:
            logger.debug(f"Ошибка запроса к Odesli REST API: {e}")

    # 2. Бесплатный веб-скрейпинг song.link (не требует API ключа)
    try:
        songlink_url = f"https://song.link/{url}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = httpx.get(songlink_url, headers=headers, follow_redirects=True, timeout=12)
        if resp.status_code == 200:
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">\s*(\{.*?\})\s*</script>', resp.text, re.DOTALL)
            if m:
                import json
                data = json.loads(m.group(1))
                page_data = data.get("props", {}).get("pageProps", {}).get("pageData", {})
                entity = page_data.get("entityData", {})
                if entity:
                    release_date = entity.get("releaseDate", {})
                    year = None
                    if isinstance(release_date, dict) and release_date.get("year"):
                        year = str(release_date.get("year"))
                    elif isinstance(release_date, str) and len(release_date) >= 4:
                        year = release_date[:4]
                    
                    duration_ms = entity.get("duration")
                    duration_s = duration_ms / 1000.0 if duration_ms else None
                    
                    result = {
                        "title": entity.get("title", ""),
                        "artist": entity.get("artistName", ""),
                        "album": entity.get("albumName") or entity.get("collectionName"),
                        "album_art": entity.get("thumbnailUrl", ""),
                        "year": year,
                        "isrc": entity.get("isrc"),
                        "duration": duration_s,
                        "track_number": entity.get("trackNumber"),
                        "explicit": entity.get("explicitness") == "explicit",
                        "deezer_id": None,
                        "spotify_url": None,
                        "youtube_music_url": None,
                        "apple_music_url": None,
                        "soundcloud_url": None,
                        "yandex_url": None,
                    }
                    
                    for sec in page_data.get("sections", []):
                        links = sec.get("links") or sec.get("items") or []
                        for l in links:
                            plat = l.get("platform")
                            item_url = l.get("url")
                            uniq = l.get("uniqueId", "")
                            if not item_url:
                                continue
                            if plat == "deezer" or "deezer" in uniq:
                                m_dz = re.search(r"track/(\d+)", item_url or uniq)
                                if m_dz:
                                    result["deezer_id"] = m_dz.group(1)
                            elif plat == "spotify":
                                result["spotify_url"] = item_url
                            elif plat in ("youtubeMusic", "youtube"):
                                if not result.get("youtube_music_url"):
                                    result["youtube_music_url"] = item_url
                            elif plat in ("appleMusic", "itunes"):
                                if not result.get("apple_music_url"):
                                    result["apple_music_url"] = item_url
                            elif plat == "soundcloud":
                                result["soundcloud_url"] = item_url
                            elif plat == "yandex":
                                result["yandex_url"] = item_url

                    # Также проверяем linksByPlatform (иногда ссылки есть только там)
                    links_by_plat = page_data.get("linksByPlatform", {})
                    for plat, p_info in links_by_plat.items():
                        p_url = p_info.get("url") if isinstance(p_info, dict) else None
                        if not p_url:
                            continue
                        if plat == "deezer" and not result.get("deezer_id"):
                            m_dz = re.search(r"track/(\d+)", p_url)
                            if m_dz: result["deezer_id"] = m_dz.group(1)
                        elif plat == "spotify" and not result.get("spotify_url"):
                            result["spotify_url"] = p_url
                        elif plat in ("youtubeMusic", "youtube") and not result.get("youtube_music_url"):
                            result["youtube_music_url"] = p_url
                        elif plat in ("appleMusic", "itunes") and not result.get("apple_music_url"):
                            result["apple_music_url"] = p_url
                        elif plat == "soundcloud" and not result.get("soundcloud_url"):
                            result["soundcloud_url"] = p_url
                        elif plat == "yandex" and not result.get("yandex_url"):
                            result["yandex_url"] = p_url
                                
                    if result.get("title") and result.get("artist"):
                        return result
    except Exception as e:
        pass

    # 3. Фолбек на публичный api.song.link (если когда-либо доступ снова откроют)
    try:
        encoded_url = urllib.parse.quote(url)
        api_url = f"https://api.song.link/v1-alpha.1/links?url={encoded_url}"
        resp = httpx.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=5.0)
        if resp.status_code == 200:
            parsed = _parse_odesli_api_data(resp.json())
            if parsed:
                return parsed
    except Exception:
        pass
        
    return None

def fetch_deezer_metadata(deezer_id: str) -> Optional[dict]:
    """
    Запрашивает публичный API Deezer для получения полной информации о треке.
    """
    if not deezer_id:
        return None
    url = f"https://api.deezer.com/track/{deezer_id}"
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            if "error" in data:
                return None
                
            album_info = data.get("album", {})
            artist_info = data.get("artist", {})
            
            year = None
            release_date = data.get("release_date") or album_info.get("release_date")
            if release_date:
                match = re.match(r"(\d{4})", release_date)
                if match:
                    year = match.group(1)
                    
            explicit = bool(data.get("explicit_lyrics") or data.get("explicit_content_lyrics") == 1)
            return {
                "title": data.get("title"),
                "artist": artist_info.get("name"),
                "isrc": data.get("isrc"),
                "album": album_info.get("title"),
                "year": year,
                "track_number": data.get("track_position"),
                "album_artist": artist_info.get("name"),
                "album_art": album_info.get("cover_xl") or album_info.get("cover_big") or album_info.get("cover_medium"),
                "duration": data.get("duration"),
                "explicit": explicit,
            }
    except Exception as e:
        print(f"[!] Deezer: {e}")
    return None

def validate_deezer_track(
    deezer_id: str | int,
    target_title: str,
    target_artist: str,
    target_duration: Optional[float] = None,
) -> Optional[dict]:
    """
    Умная валидация Deezer трека (по ID) против целевого названия, исполнителя и длительности.
    - Запрашивает метаданные из Deezer API.
    - Отсеивает караоке, трибьюты, левые каверы и инструменталки.
    - Проверяет пересечение токенов названия трека (core title match).
    - Проверяет аффинити артиста (не строгим '==', а пересечением токенов и исключением имитаций).
    - Проверяет близость длительности.
    Возвращает dict с метаданными Deezer при успехе, иначе None.
    """
    if not deezer_id:
        return None
    dz = fetch_deezer_metadata(str(deezer_id))
    if not dz:
        return None

    c_title = dz.get("title", "")
    c_artist = dz.get("artist") or dz.get("album_artist") or ""
    c_dur = dz.get("duration")

    if not c_title:
        # Если title отсутствует в ответе (например, в моках тестов), проверяем ISRC или альбом
        if dz.get("isrc") or dz.get("album"):
            return dz
        return None

    target_query = f"{target_artist} {target_title}".strip()

    try:
        from sources.deezer import is_junk_track
        if is_junk_track(c_title, c_artist, target_query):
            return None
    except Exception:
        pass

    try:
        from sources.youtube_matcher import toks
        c_title_toks = toks(c_title)
        t_title_toks = toks(target_title)

        # Кандидат ОБЯЗАН иметь хотя бы одно значимое слово из целевого названия!
        if t_title_toks and not (t_title_toks & c_title_toks):
            return None

        title_overlap = len(t_title_toks & c_title_toks) / max(1, len(t_title_toks))
        if title_overlap < 0.35:
            return None

        # Проверка длительности: если разница колоссальная (> 45s и > 20%), отсеиваем
        if target_duration and c_dur:
            dur_diff = abs(float(target_duration) - float(c_dur))
            if dur_diff > 45 and (dur_diff / max(1.0, float(target_duration)) > 0.20):
                return None

        # Проверка автора
        t_artist_toks = toks(target_artist)
        c_artist_toks = toks(c_artist)
        if t_artist_toks and c_artist_toks and not (t_artist_toks & c_artist_toks):
            # 0 общих токенов в авторе. Это допустимо только при близкой длительности и хорошем названии
            title_exact = (title_overlap >= 0.75)
            dur_close = (not target_duration or not c_dur or abs(float(target_duration) - float(c_dur)) <= 8)
            if not (title_exact and dur_close):
                return None
    except Exception:
        pass

    return dz

def fetch_musicbrainz_by_id(recording_id: str) -> Optional[dict]:
    """
    Запрашивает детальную информацию о записи по её ID (включая все релизы).
    """
    url = f"https://musicbrainz.org/ws/2/recording/{recording_id}"
    params = {
        "inc": "releases release-groups artist-credits",
        "fmt": "json"
    }
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": MB_USER_AGENT}, timeout=httpx.Timeout(2.5, connect=2.0))
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[!] MusicBrainz: {e}")
    return None

def fetch_musicbrainz_by_isrc(isrc: str, expected_artist: str = "") -> Optional[dict]:
    """
    Ищет метаданные трека в MusicBrainz по его ISRC в два шага.
    """
    if not isrc:
        return None
    url = f"https://musicbrainz.org/ws/2/isrc/{isrc}"
    params = {
        "inc": "artist-credits",
        "fmt": "json"
    }
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": MB_USER_AGENT}, timeout=httpx.Timeout(2.5, connect=2.0))
        if resp.status_code == 200:
            data = resp.json()
            recordings = data.get("recordings", [])
            if recordings:
                recording_id = recordings[0].get("id")
                if recording_id:
                    # Шаг 2: Получаем детальную запись со списком релизов
                    full_rec = fetch_musicbrainz_by_id(recording_id)
                    if full_rec:
                        return parse_mb_recording(full_rec, expected_artist)
    except Exception as e:
        print(f"[!] MusicBrainz (ISRC): {e}")
    return None

def is_compilation_album(album_title: Optional[str], album_artist: Optional[str] = "") -> bool:
    """
    Определяет, является ли альбом сборником / компиляцией / Various Artists.
    """
    if not album_title:
        return False
    alb_low = album_title.lower()
    art_low = (album_artist or "").lower()
    if art_low in ("various artists", "various", "va"):
        return True
    compilation_markers = [
        "various", "now that's what", "hits ", "greatest hits", "best of",
        "collection", "the very best", "compilation", "super raccolta",
        "dance mania", "rare rnb", "vol.", "volume ", "soundtrack", "ost",
        "tribute", "karaoke", "top 100", "top 50", "party hits", "summer hits",
        "club hits", "dance hits", "extracts", "sampler"
    ]
    return any(marker in alb_low for marker in compilation_markers)

def score_release(rel: dict, rec_artist: str, expected_artist: str) -> int:
    """
    Вычисляет оценку соответствия релиза оригинальному студийному альбому.
    Снимает баллы за сборники, концертные записи, Various Artists и т.д.
    """
    rg = rel.get("release-group", {})
    primary = (rg.get("primary-type") or "").lower()
    secondary = [t.lower() for t in (rg.get("secondary-types") or [])]
    title = (rel.get("title") or "").lower()
    
    score = 0
    if primary == "album":
        score += 10
    elif primary == "single":
        score += 4
    elif primary == "ep":
        score += 3
        
    # Штраф за сборники, концертные записи и т.д.
    if "compilation" in secondary or "live" in secondary:
        score -= 8
        
    # Штраф за маркеры сборников в названии релиза
    if is_compilation_album(title):
        score -= 15
        
    # Проверяем исполнителя релиза
    rel_credits = rel.get("artist-credit", [])
    has_artist_match = False
    for ac in rel_credits:
        if isinstance(ac, dict):
            rel_art_name = (ac.get("name") or ac.get("artist", {}).get("name") or "").lower()
            if "various" in rel_art_name:
                score -= 15
            elif expected_artist and expected_artist.lower() in rel_art_name:
                has_artist_match = True
            elif rec_artist and rec_artist.lower() in rel_art_name:
                has_artist_match = True
                
    if has_artist_match:
        score += 10
        
    return score

def parse_mb_recording(recording: dict, expected_artist: str = "") -> dict:
    """
    Парсит JSON-запись из MusicBrainz и возвращает структурированный словарь метаданных.
    """
    metadata = {
        "title": recording.get("title"),
        "artist": None,
        "album": None,
        "year": None,
        "track_number": None,
        "track_total": None,
        "album_artist": None,
        "album_art": None,
        "_mb_release_id": None,
        "duration": (recording.get("length") / 1000.0) if recording.get("length") else None,
        "_score": -999,
    }
    
    # Исполнитель записи
    credits = recording.get("artist-credit", [])
    rec_artist = ""
    if credits:
        rec_artist = "".join(
            c.get("name", "") + c.get("joinphrase", "") for c in credits if isinstance(c, dict)
        ).strip()
        metadata["artist"] = rec_artist
        
    # Ищем альбом
    releases = recording.get("releases", [])
    if releases:
        # Выбираем лучший релиз на основе оценки
        best_release = None
        best_score = -999
        for rel in releases:
            score = score_release(rel, rec_artist, expected_artist)
            if score > best_score:
                best_score = score
                best_release = rel
                
        if best_release:
            metadata["album"] = best_release.get("title")
            metadata["_score"] = best_score
            rel_id = best_release.get("id")
            if rel_id:
                metadata["_mb_release_id"] = rel_id
                caa_art = fetch_coverartarchive_art(rel_id)
                if caa_art:
                    metadata["album_art"] = caa_art
            
            # Получаем год релиза
            date = best_release.get("date")
            if date:
                match = re.match(r"(\d{4})", date)
                if match:
                    metadata["year"] = match.group(1)
                    
            # Получаем номер трека и общее число треков
            media = best_release.get("media", [])
            if media:
                medium = media[0]
                metadata["track_total"] = medium.get("track-count")
                tracks = medium.get("tracks", [])
                if tracks:
                    metadata["track_number"] = tracks[0].get("number")
                    try:
                        metadata["track_number"] = int(metadata["track_number"])
                    except (ValueError, TypeError):
                        pass
                    
            # Исполнитель альбома
            rel_credits = best_release.get("artist-credit", [])
            if rel_credits:
                metadata["album_artist"] = "".join(
                    c.get("name", "") + c.get("joinphrase", "") for c in rel_credits if isinstance(c, dict)
                ).strip()
                
    if not metadata["album_artist"] and metadata["artist"]:
        metadata["album_artist"] = metadata["artist"]
        
    return metadata

def search_musicbrainz_by_text(artist: str, title: str) -> Optional[dict]:
    """
    Ищет метаданные трека в MusicBrainz по текстовому запросу.
    Выбирает лучший релиз по всем найденным записям.
    """
    url = "https://musicbrainz.org/ws/2/recording/"
    query = f'artist:"{artist}" AND recording:"{title}"'
    params = {
        "query": query,
        "fmt": "json",
        "limit": 5
    }
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": MB_USER_AGENT}, timeout=httpx.Timeout(4.5, connect=3.0))
        if resp.status_code == 200:
            data = resp.json()
            recordings = data.get("recordings", [])
            if recordings:
                best_metadata = None
                best_score = -999
                
                # Ищем лучший релиз среди всех найденных записей (оценка score >= 80)
                for rec in recordings:
                    rec_score = int(rec.get("score", 0))
                    if rec_score >= 80:
                        parsed = parse_mb_recording(rec, artist)
                        if parsed and parsed.get("_score", -999) > best_score:
                            best_score = parsed["_score"]
                            best_metadata = parsed
                            
                if best_metadata:
                    best_metadata.pop("_score", None)
                    return best_metadata
    except Exception as e:
        print(f"[!] MusicBrainz (Search): {e}")
    return None

def score_text_candidate(cand_title: str, cand_artist: str, target_query: str, target_title: str = "") -> float:
    """
    Оценивает совпадение кандидата с целевым запросом по токенам и штрафует
    за лишние маркеры версий (ремиксы, каверы, караоке, лайвы).
    """
    from sources.youtube_matcher import toks, _version_markers
    q_toks = toks(target_query)
    c_toks = toks(f"{cand_artist} {cand_title}")
    if not q_toks:
        return 0.0

    # Кандидат ОБЯЗАТЕЛЬНО должен иметь пересечение по названию трека!
    # Иначе другая песня того же исполнителя получает >60% совпадения только за счет имени артиста.
    if target_title:
        t_tit_toks = toks(target_title)
        c_tit_toks = toks(cand_title)
        if t_tit_toks and not (t_tit_toks & c_tit_toks):
            return 0.0
        
    overlap = len(q_toks & c_toks) / len(q_toks)
    
    q_vm = _version_markers(target_query)
    c_vm = _version_markers(cand_title)
    extra_vm = c_vm - q_vm
    
    score = overlap
    CRITICAL_MARKERS = {"remix", "cover", "karaoke", "instrumental", "tribute", "parody", "live"}
    if extra_vm & CRITICAL_MARKERS:
        score -= 0.6 * len(extra_vm & CRITICAL_MARKERS)
    elif extra_vm:
        score -= 0.3 * len(extra_vm)
        
    extra_words = len(c_toks - q_toks)
    score -= 0.02 * min(extra_words, 10)
    
    return max(0.0, score)

def fetch_itunes_metadata(artist: str = "", title: str = "", isrc: str = "") -> Optional[dict]:
    """
    Получает метаданные трека из iTunes Search/Lookup API с валидацией кандидатов.
    """
    params = {}
    if isrc:
        url = "https://itunes.apple.com/lookup"
        params = {"isrc": isrc}
    else:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist} {title}".strip(),
            "media": "music",
            "entity": "musicTrack",
            "limit": 5
        }
    try:
        r = httpx.get(url, params=params, timeout=6)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if not results:
                return None
                
            best_track = None
            if isrc:
                best_track = results[0]
            else:
                target_query = f"{artist} {title}"
                best_score = -1.0
                for track in results:
                    s = score_text_candidate(track.get("trackName", ""), track.get("artistName", ""), target_query, target_title=title)
                    if s > best_score:
                        best_score = s
                        best_track = track
                if best_score < 0.5:
                    best_track = None
                    
            if best_track:
                art_url = best_track.get("artworkUrl100", "")
                if art_url:
                    # Извлекаем картинку в высоком разрешении
                    art_url = art_url.replace("100x100bb.jpg", "1000x1000bb.jpg")
                
                release_date = best_track.get("releaseDate", "")
                year = release_date[:4] if release_date else None
                
                duration = (best_track.get("trackTimeMillis", 0) / 1000.0) if best_track.get("trackTimeMillis") else None
                return {
                    "title": best_track.get("trackName"),
                    "artist": best_track.get("artistName"),
                    "album": best_track.get("collectionName"),
                    "year": year,
                    "track_number": best_track.get("trackNumber"),
                    "track_total": best_track.get("trackCount"),
                    "album_artist": best_track.get("artistName"),
                    "album_art": art_url,
                    "duration": duration,
                    "apple_music_url": best_track.get("trackViewUrl"),
                    "isrc": best_track.get("isrc"),
                }
    except Exception as e:
        print(f"[!] iTunes: Ошибка получения метаданных: {e}")
    return None

def fetch_discogs_metadata(artist: str, title: str) -> Optional[dict]:
    """
    Получает метаданные трека из Discogs API.
    """
    if not config.DISCOGS_TOKEN:
        return None
    url = "https://api.discogs.com/database/search"
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Discogs token={config.DISCOGS_TOKEN}"
    }
    params = {
        "q": f"{artist} - {title}",
        "type": "release",
        "limit": 1
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                release = results[0]
                year = release.get("year")
                if not year and "year" in release:
                    year = str(release["year"])
                
                genres = release.get("genre", [])
                styles = release.get("style", [])
                all_tags = genres + styles
                genre_str = ", ".join(all_tags[:3]) if all_tags else None
                
                return {
                    "album": release.get("title"),
                    "year": year,
                    "genre": genre_str,
                    "album_art": release.get("cover_image")
                }
    except Exception as e:
        print(f"[!] Discogs: Ошибка получения метаданных: {e}")
    return None

_LASTFM_PATTERN = re.compile(
    r"^https?://(?:www\.)?last\.fm/(?:[a-z]{2}/)?music/([^/?#]+)(?:/(?:_/?([^/?#]+)|([^/?#]+)/([^/?#]+)))?",
    re.I
)

def resolve_lastfm_track(url: str) -> Optional[dict]:
    """Извлекает артиста и трек из ссылки Last.fm и находит официальные метаданные."""
    m = _LASTFM_PATTERN.search(url)
    if not m:
        return None
    raw_artist = m.group(1)
    raw_track = m.group(2) or m.group(4)
    raw_album = m.group(3) if m.group(4) else None

    if not raw_artist or not raw_track:
        return None

    artist = urllib.parse.unquote_plus(raw_artist).strip()
    title = urllib.parse.unquote_plus(raw_track).strip()
    album = urllib.parse.unquote_plus(raw_album).strip() if raw_album else None

    if not artist or not title:
        return None

    # Ищем официальные метаданные по артисту и названию (iTunes, Deezer, MusicBrainz)
    itunes_meta = fetch_itunes_metadata(artist=artist, title=title)
    deezer_id = None
    dz_meta = None
    try:
        from sources.deezer import search_deezer_track
        deezer_id = search_deezer_track(artist, title)
        if deezer_id:
            dz_meta = fetch_deezer_metadata(deezer_id)
    except Exception:
        pass

    meta = {
        "title": title,
        "artist": artist,
        "album": album or (dz_meta.get("album") if dz_meta else None) or (itunes_meta.get("album") if itunes_meta else None),
        "year": (dz_meta.get("year") if dz_meta else None) or (itunes_meta.get("year") if itunes_meta else None),
        "album_art": (dz_meta.get("album_art") if dz_meta else None) or (itunes_meta.get("album_art") if itunes_meta else None),
        "duration": (dz_meta.get("duration") if dz_meta else None) or (itunes_meta.get("duration") if itunes_meta else None),
        "deezer_id": deezer_id,
        "isrc": (dz_meta.get("isrc") if dz_meta else None) or (itunes_meta.get("isrc") if itunes_meta else None),
        "lastfm_url": url,
    }
    return sanitize_metadata_strings(meta)


_VK_AUDIO_ID_PATTERN = re.compile(r"audio(-?\d+_\d+)", re.I)
_VK_TRACK_PATH_PATTERN = re.compile(r"/track/(-?\d+_\d+)", re.I)

def resolve_vk_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека из ссылки VK Музыки (vk.com, vk.ru, m.vk.com)."""
    parsed = urllib.parse.urlparse(url)
    audio_id_match = _VK_AUDIO_ID_PATTERN.search(url) or _VK_TRACK_PATH_PATTERN.search(url)
    audio_id = audio_id_match.group(1) if audio_id_match else None

    artist = None
    title = None
    duration = None
    album = None
    album_art = None
    direct_mp3 = None

    # 1. Если задан VK_TOKEN, используем официальный audio.getById
    if config.VK_TOKEN and audio_id:
        try:
            r = httpx.get(
                "https://api.vk.com/method/audio.getById",
                params={"audios": audio_id, "access_token": config.VK_TOKEN, "v": "5.131"},
                timeout=5.0
            )
            if r.status_code == 200:
                data = r.json()
                items = data.get("response", [])
                if items and isinstance(items, list) and isinstance(items[0], dict):
                    item = items[0]
                    artist = item.get("artist")
                    title = item.get("title")
                    duration = item.get("duration")
                    direct_mp3 = item.get("url")
                    alb = item.get("album", {})
                    if isinstance(alb, dict):
                        album = alb.get("title")
                        thumb = alb.get("thumb", {})
                        if isinstance(thumb, dict):
                            album_art = thumb.get("photo_600") or thumb.get("photo_300")
        except Exception as e:
            print(f"[!] VK API error: {e}")

    # 2. Если аудио ID есть, но токен не задан:
    if not artist and audio_id and not config.VK_TOKEN:
        print("[!] VK Музыка: Для скачивания по прямым ID (audio-XXX_YYY) укажите VK_TOKEN в .env")

    # 3. Извлечение из query параметров (например, ?q=Artist+-+Title)
    if not artist:
        qs = urllib.parse.parse_qs(parsed.query)
        if "q" in qs and qs["q"]:
            raw_q = qs["q"][0].strip()
            if " - " in raw_q:
                artist, title = raw_q.split(" - ", 1)
            else:
                title = raw_q
                artist = "Unknown Artist"

    if not title:
        return None

    # Ищем кроссплатформенные метаданные (Deezer, iTunes, MusicBrainz)
    itunes_meta = fetch_itunes_metadata(artist=artist, title=title) if artist else None
    deezer_id = None
    dz_meta = None
    if artist and title:
        try:
            from sources.deezer import search_deezer_track
            deezer_id = search_deezer_track(artist, title)
            if deezer_id:
                dz_meta = fetch_deezer_metadata(deezer_id)
        except Exception:
            pass

    meta = {
        "title": title,
        "artist": artist or "Unknown Artist",
        "album": album or (dz_meta.get("album") if dz_meta else None) or (itunes_meta.get("album") if itunes_meta else None),
        "year": (dz_meta.get("year") if dz_meta else None) or (itunes_meta.get("year") if itunes_meta else None),
        "album_art": album_art or (dz_meta.get("album_art") if dz_meta else None) or (itunes_meta.get("album_art") if itunes_meta else None),
        "duration": duration or (dz_meta.get("duration") if dz_meta else None) or (itunes_meta.get("duration") if itunes_meta else None),
        "deezer_id": deezer_id,
        "isrc": (dz_meta.get("isrc") if dz_meta else None) or (itunes_meta.get("isrc") if itunes_meta else None),
        "direct_url": direct_mp3,
        "vk_url": url,
    }
    return sanitize_metadata_strings(meta)


def fetch_lastfm_genres(artist: str, title: str) -> Optional[str]:
    """
    Получает теги (жанры) трека или исполнителя из Last.fm API.
    """
    if not config.LASTFM_API_KEY:
        return None
    url = "https://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "track.gettoptags",
        "artist": artist,
        "track": title,
        "api_key": config.LASTFM_API_KEY,
        "format": "json"
    }
    try:
        r = httpx.get(url, params=params, timeout=3.0)
        if r.status_code in (401, 403):
            return None
        tags = []
        if r.status_code == 200:
            data = r.json()
            tags_list = data.get("toptags", {}).get("tag", [])
            if isinstance(tags_list, list):
                tags = [t.get("name") for t in tags_list if t.get("name")]
                
        # Если для трека тегов нет, пробуем теги исполнителя
        if not tags:
            params["method"] = "artist.gettoptags"
            params.pop("track", None)
            r = httpx.get(url, params=params, timeout=3.0)
            if r.status_code in (401, 403):
                return None
            if r.status_code == 200:
                data = r.json()
                tags_list = data.get("toptags", {}).get("tag", [])
                if isinstance(tags_list, list):
                    tags = [t.get("name") for t in tags_list if t.get("name")]
                    
        # Фильтруем общие не-жанровые теги
        excluded = {"seen live", "favorites", "awesome", "cool", "beautiful", "love", "favorite", "favourite"}
        genres = [t.title() for t in tags if t.lower() not in excluded]
        if genres:
            return ", ".join(genres[:3])
    except Exception:
        pass
    return None

def fetch_coverartarchive_art(release_id: str, size: int = 0) -> Optional[str]:
    """
    Ищет официальную обложку релиза в MusicBrainz Cover Art Archive.

    CAA отдаёт только 250 / 500 / 1200, поэтому при ``size`` > 1200 берётся
    максимум. Раньше здесь жёстко стоял ``front-500`` — то есть в самом верху
    каскада стоял минимальный размер, и он побеждал все 1000px-источники.
    """
    if not release_id:
        return None
    # Берём наименьший доступный размер, покрывающий запрос; если ничего не
    # хватает — максимум.
    candidates = [c for c in (250, 500, 1200) if size <= c] or [1200]
    for candidate in candidates:
        url = f"https://coverartarchive.org/release/{release_id}/front-{candidate}"
        try:
            r = httpx.head(url, headers={"User-Agent": MB_USER_AGENT}, follow_redirects=True, timeout=4.0)
            if r.status_code == 200:
                return str(r.url)
        except Exception:
            pass
    return None

def fetch_lastfm_album_art(artist: str, album: str = "", title: str = "") -> Optional[str]:
    """Ищет студийную квадратную обложку в Last.fm API по альбому или названию трека."""
    if not artist or (not album and not title):
        return None
    api_key = config.LASTFM_API_KEY or "b25b959554ed76058ac220b7b2e0a026"
    # 1. Поиск по альбому
    if album:
        try:
            params = {
                "method": "album.getinfo",
                "api_key": api_key,
                "artist": artist,
                "album": album,
                "format": "json",
                "autocorrect": "1"
            }
            r = httpx.get("https://ws.audioscrobbler.com/2.0/", params=params, headers={"User-Agent": USER_AGENT}, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                images = data.get("album", {}).get("image", [])
                for size_key in ("mega", "extralarge", "large"):
                    for img in reversed(images):
                        if img.get("size") == size_key and img.get("#text"):
                            raw_url = img["#text"]
                            if "/300x300/" in raw_url:
                                raw_url = raw_url.replace("/300x300/", "/_/")
                            return raw_url
        except Exception:
            pass

    # 2. Фолбек: поиск по треку (track.getinfo)
    if title:
        try:
            params = {
                "method": "track.getinfo",
                "api_key": api_key,
                "artist": artist,
                "track": title,
                "format": "json",
                "autocorrect": "1"
            }
            r = httpx.get("https://ws.audioscrobbler.com/2.0/", params=params, headers={"User-Agent": USER_AGENT}, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                images = data.get("track", {}).get("album", {}).get("image", [])
                for size_key in ("mega", "extralarge", "large"):
                    for img in reversed(images):
                        if img.get("size") == size_key and img.get("#text"):
                            raw_url = img["#text"]
                            if "/300x300/" in raw_url:
                                raw_url = raw_url.replace("/300x300/", "/_/")
                            return raw_url
        except Exception:
            pass
    return None

def fetch_itunes_album_art(artist: str, album: str, size: int = 0) -> Optional[str]:
    """Ищет квадратную обложку альбома в iTunes (по умолчанию 1000x1000)."""
    if not artist or not album:
        return None
    try:
        r = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {album}", "entity": "album", "limit": 3},
            headers={"User-Agent": USER_AGENT},
            timeout=5.0
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            for alb in results:
                art = alb.get("artworkUrl100")
                if art:
                    return coverart.resize_itunes_url(art, size or 1000)
    except Exception:
        pass
    return None

def fetch_deezer_album_art(artist: str, album: str, size: int = 0) -> Optional[str]:
    """Ищет студийную обложку альбома в Deezer (нативный мастер до 1400x1400)."""
    if not artist or not album:
        return None
    try:
        r = httpx.get(
            "https://api.deezer.com/search/album",
            params={"q": f"{artist} {album}", "limit": 3},
            headers={"User-Agent": USER_AGENT},
            timeout=5.0
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                art = data[0].get("cover_xl") or data[0].get("cover_big")
                if art and size:
                    return coverart.resize_dzcdn_url(art, size)
                return art
    except Exception:
        pass
    return None

def fetch_best_cover_art(
    artist: str,
    album: str = "",
    title: str = "",
    release_id: str = "",
    target_size: int = 0,
) -> Optional[str]:
    """
    Каскадный поиск официальной студийной обложки.

    Источники: Cover Art Archive -> Last.fm -> iTunes -> Deezer.

    Раньше побеждал «первый ответивший», из-за чего обложка на 300px из
    Last.fm легко обгоняла iTunes на 1000px. Теперь все кандидаты собираются,
    приводятся к целевому размеру (где CDN это умеет) и ранжируются по
    реально достижимому разрешению.

    ``target_size`` — 1400 для FLAC, 768 для MP3, 0 = не переписывать.
    """
    cache = cache_mod.get_cache()

    # Обложка меняется крайне редко, поэтому на неё отдельный долгий TTL.
    # Приоритет у release_id: конкретный релиз точнее, чем пара (artist, album).
    if release_id or (artist and album):
        cached_art = cache.get_release_art(artist, album, release_id=release_id)
        if cached_art:
            return coverart.resize_url_for_source(cached_art, target_size) if target_size else cached_art

    candidates = []

    if release_id:
        art = fetch_coverartarchive_art(release_id, size=target_size)
        if art:
            candidates.append(art)

    if artist and (album or title):
        art = fetch_lastfm_album_art(artist, album=album, title=title)
        if art:
            candidates.append(art)

    if artist and album:
        art = fetch_itunes_album_art(artist, album, size=target_size)
        if art:
            candidates.append(art)
        art = fetch_deezer_album_art(artist, album, size=target_size)
        if art:
            candidates.append(art)

    if not candidates:
        return None

    if target_size:
        candidates = [coverart.resize_url_for_source(c, target_size) for c in candidates]

    # Убираем дубли, сохраняя порядок.
    unique = list(dict.fromkeys(candidates))
    unique.sort(key=coverart.estimate_max_size, reverse=True)
    best = unique[0]

    if release_id or (artist and album):
        cache.set_release_art(artist, album, best, release_id=release_id)
    return best

_VIDEO_JUNK_PATTERNS = [
    r"(?i)\s*[\(\[]\s*official\s+(?:music\s+)?video\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*official\s+audio\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*official\s+visualizer\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*visualizer\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*official\s+hd\s+(?:music\s+)?video\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*official\s+video\s+remaster(?:ed)?\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*remaster(?:ed)?\s*(?:\d{4})?\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*4k(?:\s+remaster(?:ed)?|\s+uhd)?\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*hd\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*audio\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*lyric\s+video\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*lyrics?\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*clip\s+officiel\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*video\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*music\s+video\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*клип\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*премьера\s+(?:клипа|трека)?(?:\s+\d{4})?\s*[\)\]]",
    r"(?i)\s*[\(\[]\s*mood\s+video\s*[\)\]]",
    r"(?i)\s*\|?\s*official\s+(?:music\s+)?video\s*$",
    r"(?i)\s*\|?\s*official\s+audio\s*$",
]

def clean_youtube_title_and_artist(raw_title: str, channel_name: str = "") -> tuple[str, str]:
    """
    Очищает заголовок YouTube-видео от мусора (Official Video, 4K, Remastered и т.д.)
    и разделяет на исполнителя и название трека.
    """
    title = raw_title or ""
    channel = channel_name or ""
    if channel.endswith(" - Topic"):
        channel = channel[:-8].strip()
    if channel.endswith("VEVO") and len(channel) > 4:
        channel = channel[:-4].strip()
    if channel.endswith(" Official"):
        channel = channel[:-9].strip()

    for pat in _VIDEO_JUNK_PATTERNS:
        title = re.sub(pat, "", title)
    title = re.sub(r"\s+", " ", title).strip()

    artist = channel
    for sep in (" - ", " – ", " — ", " | ", " : "):
        if sep in title:
            left, right = title.split(sep, 1)
            left = left.strip()
            right = right.strip()
            if left:
                artist = left
                title = right
                break

    title = title.strip("'\"“”«»`")
    if (title.startswith("(") and title.endswith(")")) or (title.startswith("[") and title.endswith("]")):
        inner = title[1:-1].strip()
        if "(" not in inner and ")" not in inner and "[" not in inner and "]" not in inner:
            title = inner

    if artist and title.lower().startswith(artist.lower() + " - "):
        title = title[len(artist) + 3:].strip()
    elif artist and title.lower().startswith(artist.lower() + ": "):
        title = title[len(artist) + 2:].strip()

    return artist, title

def resolve_youtube_track(url: str) -> Optional[dict]:
    """
    Извлекает название и исполнителя из ссылки на YouTube и ищет
    официальные метаданные по стриминговым платформам (iTunes, Deezer, Odesli, MusicBrainz, Last.fm).
    """
    from sources.youtube import extract_youtube_video_id
    url = unshorten_url(url)
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None

    raw_title = ""
    channel_name = ""
    duration = None
    thumb_url = None

    # 1. Быстрый и официальный YouTube oEmbed (отражает реальный заголовок видео на странице)
    oembed_title = ""
    oembed_channel = ""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        r = httpx.get(oembed_url, headers={"User-Agent": USER_AGENT}, timeout=4.0)
        if r.status_code == 200:
            data = r.json()
            oembed_title = data.get("title", "")
            oembed_channel = data.get("author_name", "")
            thumb_url = data.get("thumbnail_url")
    except Exception:
        pass

    # 2. YouTube Music API (получение точной длительности, обложки высокого разрешения и метаданных)
    yt_title = ""
    yt_channel = ""
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        song_data = yt.get_song(video_id)
        if song_data and "videoDetails" in song_data:
            vd = song_data["videoDetails"]
            yt_title = vd.get("title", "")
            yt_channel = vd.get("author", "")
            if vd.get("lengthSeconds"):
                try:
                    duration = float(vd["lengthSeconds"])
                except Exception:
                    pass
            thumbs = vd.get("thumbnail", {}).get("thumbnails", [])
            if thumbs:
                thumb_url = thumbs[-1].get("url")
    except Exception:
        pass

    # 3. Фолбек на yt-dlp metadata если oEmbed и YTMusic не дали результатов
    if not oembed_title and not yt_title:
        try:
            import yt_dlp
            from sources.ytdlp_opts import js_runtime_opts
            opts = {"quiet": True, "skip_download": True, "noplaylist": True}
            opts.update(js_runtime_opts())
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if info:
                    oembed_title = info.get("track") or info.get("title") or ""
                    oembed_channel = info.get("artist") or info.get("uploader") or info.get("channel") or ""
                    duration = info.get("duration")
                    thumb_url = info.get("thumbnail")
        except Exception:
            pass

    # Фильтрация ложных стем/версионных меток:
    # YouTube Music Content ID иногда подменяет аудиодорожку видео на внутренний стем/ассет (например, "Takyon (vocal)" вместо оригинального трека)
    STEM_MARKERS = r"(?i)\s*[\(\[]\s*(?:vocal|vocals|acapella|a\s*cappella|instrumental|karaoke|stems?|isolated\s+vocal|backing\s+track)\s*[\)\]]"
    if yt_title and re.search(STEM_MARKERS, yt_title):
        if not (oembed_title and re.search(STEM_MARKERS, oembed_title)):
            yt_title = re.sub(STEM_MARKERS, "", yt_title).strip()

    # Выбор наилучшего заголовка и артиста
    if oembed_title:
        has_artist_separator = any(sep in oembed_title for sep in (" - ", " – ", " — ", " | "))
        o_art, o_tit = clean_youtube_title_and_artist(oembed_title, oembed_channel)
        
        if has_artist_separator and o_art and o_tit and not o_art.lower().endswith(" - topic"):
            raw_title = oembed_title
            channel_name = o_art
        elif yt_title and yt_channel:
            raw_title = yt_title
            channel_name = yt_channel
        else:
            raw_title = oembed_title
            channel_name = yt_channel or oembed_channel
    else:
        raw_title = yt_title
        channel_name = yt_channel

    if not raw_title:
        return None

    artist, clean_title = clean_youtube_title_and_artist(raw_title, channel_name)
    if not clean_title:
        clean_title = raw_title
    if not artist:
        artist = channel_name or "Unknown Artist"

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    meta = {
        "title": clean_title,
        "artist": artist,
        "album": None,
        "album_artist": artist,
        "album_art": thumb_url,
        "year": None,
        "duration": duration,
        "track_number": None,
        "track_total": None,
        "isrc": None,
        "explicit": False,
        "deezer_id": None,
        "spotify_url": None,
        "youtube_music_url": canonical_url,
        "apple_music_url": None,
        "soundcloud_url": None,
        "yandex_url": None,
        "direct_url": canonical_url,
        "youtube_video_id": video_id,
    }

    # Поиск по другим платформам (iTunes -> Songlink -> Deezer -> MusicBrainz -> Last.fm)
    itunes_meta = fetch_itunes_metadata(artist=artist, title=clean_title)
    if not itunes_meta and re.search(r"[\(\[]", clean_title):
        title_no_brackets = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", clean_title).strip()
        if title_no_brackets and title_no_brackets.lower() != clean_title.lower():
            itunes_meta = fetch_itunes_metadata(artist=artist, title=title_no_brackets)
    elif not itunes_meta and (" feat." in clean_title.lower() or " ft." in clean_title.lower()):
        title_no_feat = re.sub(r"(?i)\s*[\(\[]?\s*f(?:ea)?t\.?[^\)\]]*[\)\]]?", "", clean_title).strip()
        itunes_meta = fetch_itunes_metadata(artist=artist, title=title_no_feat)

    if itunes_meta:
        for k in ("title", "artist", "album", "album_artist", "year", "track_number", "track_total", "album_art", "duration"):
            if itunes_meta.get(k):
                meta[k] = itunes_meta[k]
        if itunes_meta.get("apple_music_url"):
            meta["apple_music_url"] = itunes_meta["apple_music_url"]
            sl_meta = resolve_song_link(itunes_meta["apple_music_url"])
            if sl_meta:
                sl_dz = sl_meta.get("deezer_id")
                if sl_dz and validate_deezer_track(sl_dz, meta.get("title") or clean_title, meta.get("artist") or artist, meta.get("duration") or duration):
                    meta["deezer_id"] = sl_dz
                for k in ("spotify_url", "isrc"):
                    if sl_meta.get(k) and not meta.get(k):
                        meta[k] = sl_meta[k]

    # Если Deezer ID ещё не найден через Odesli/iTunes:
    if not meta.get("deezer_id"):
        from sources.deezer import search_deezer_track
        search_art = meta.get("artist") or artist
        search_tit = meta.get("title") or clean_title
        dz_id = search_deezer_track(
            search_art,
            search_tit,
            duration=meta.get("duration") or duration,
            album=meta.get("album"),
        )
        if not dz_id and re.search(r"[\(\[]", search_tit):
            search_tit_clean = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", search_tit).strip()
            if search_tit_clean and search_tit_clean.lower() != search_tit.lower():
                dz_id = search_deezer_track(
                    search_art,
                    search_tit_clean,
                    duration=meta.get("duration") or duration,
                    album=meta.get("album"),
                )
        if not dz_id and search_art != artist:
            dz_id = search_deezer_track(
                artist,
                clean_title,
                duration=meta.get("duration") or duration,
                album=meta.get("album"),
            )
        if dz_id:
            meta["deezer_id"] = dz_id

    if meta.get("deezer_id"):
        dz = fetch_deezer_metadata(meta["deezer_id"])
        if dz:
            for k, v in dz.items():
                if v and not meta.get(k):
                    meta[k] = v

    album = meta.get("album")
    album_artist = meta.get("album_artist") or meta.get("artist") or ""
    if is_compilation_album(album, album_artist) or not album:
        isrc = meta.get("isrc")
        mb_data = None
        if isrc:
            mb_data = fetch_musicbrainz_by_isrc(isrc, meta.get("artist", ""))
        if not mb_data and meta.get("artist") and meta.get("title"):
            mb_data = search_musicbrainz_by_text(meta["artist"], meta["title"])
            if not mb_data and re.search(r"[\(\[]", meta["title"]):
                tit_clean = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", meta["title"]).strip()
                if tit_clean and tit_clean.lower() != meta["title"].lower():
                    mb_data = search_musicbrainz_by_text(meta["artist"], tit_clean)
        if mb_data:
            for key in ("album", "year", "track_number", "track_total", "album_artist", "album_art", "_mb_release_id"):
                if mb_data.get(key):
                    meta[key] = mb_data[key]

    # Каскадный поиск студийной квадратной обложки высокого разрешения,
    # если обложка отсутствует или осталась превьюшкой с YouTube (i.ytimg.com)
    curr_art = meta.get("album_art") or ""
    if not curr_art or "ytimg.com" in curr_art:
        best_art = fetch_best_cover_art(
            artist=meta.get("artist") or artist,
            album=meta.get("album") or "",
            title=meta.get("title") or clean_title,
            release_id=meta.get("_mb_release_id") or ""
        )
        if best_art:
            meta["album_art"] = best_art

    try:
        genres = fetch_lastfm_genres(meta.get("artist", ""), meta.get("title", ""))
        if genres:
            meta["genre"] = genres
    except Exception:
        pass

    return sanitize_metadata_strings(meta)

def resolve_soundcloud_track(url: str) -> Optional[dict]:
    """
    Первоклассное извлечение метаданных для треков с SoundCloud:
    1. Быстрое получение oEmbed (заголовок, автор, обложка)
    2. Очистка промо-тегов ([FREE DL], [OUT NOW] и т.д.) и разделение Артист - Название
    3. Кроссплатформенный поиск студийного релиза (iTunes -> Songlink -> Deezer -> MusicBrainz)
    4. Каскадный подбор студийной Ultra-HD обложки либо оригинальной обложки SoundCloud (-original.jpg)
    5. Тегирование жанров через Last.fm
    """
    clean_url = unshorten_url(url.strip())
    raw_title = ""
    author_name = ""
    thumb_url = None

    try:
        r = httpx.get(
            "https://soundcloud.com/oembed",
            params={"format": "json", "url": clean_url},
            headers={"User-Agent": USER_AGENT},
            timeout=4.0
        )
        if r.status_code == 200:
            data = r.json()
            raw_title = data.get("title", "") or ""
            author_name = data.get("author_name", "") or ""
            thumb_url = data.get("thumbnail_url")
    except Exception:
        pass

    if not raw_title and not author_name:
        m = re.search(r"soundcloud\.com/([^/?#]+)/([^/?#]+)", clean_url)
        if m:
            author_name = m.group(1).replace("-", " ").title()
            raw_title = m.group(2).replace("-", " ")

    if not raw_title:
        return None

    if author_name and raw_title.lower().endswith(f" by {author_name.lower()}"):
        raw_title = raw_title[:-len(f" by {author_name}")].strip()

    SC_JUNK_PATTERNS = [
        r"(?i)\s*[\(\[]\s*(?:free\s+(?:dl|download)|out\s+now|buy\s+link|stream\s+now|clip|preview|teaser|premiere|première|exclusive|original\s+mix|free\s+track)\s*[\)\]]",
        r"(?i)\s*\|?\s*(?:free\s+(?:dl|download)|out\s+now)\s*$",
    ]
    for pat in SC_JUNK_PATTERNS:
        raw_title = re.sub(pat, "", raw_title)
    raw_title = re.sub(r"\s+", " ", raw_title).strip()

    artist, clean_title = clean_youtube_title_and_artist(raw_title, author_name)
    if not clean_title:
        clean_title = raw_title
    if not artist:
        artist = author_name or "Unknown Artist"

    best_sc_thumb = thumb_url
    if thumb_url and "-t500x500." in thumb_url:
        best_sc_thumb = thumb_url.replace("-t500x500.", "-original.")

    meta = {
        "title": clean_title,
        "artist": artist,
        "album": None,
        "album_artist": artist,
        "album_art": best_sc_thumb,
        "year": None,
        "track_number": None,
        "track_total": None,
        "duration": None,
        "explicit": False,
        "deezer_id": None,
        "isrc": None,
        "spotify_url": None,
        "apple_music_url": None,
        "soundcloud_url": clean_url,
        "direct_url": clean_url,
    }

    itunes_meta = fetch_itunes_metadata(artist=artist, title=clean_title)
    if not itunes_meta and re.search(r"[\(\[]", clean_title):
        clean_no_br = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", clean_title).strip()
        if clean_no_br and clean_no_br.lower() != clean_title.lower():
            itunes_meta = fetch_itunes_metadata(artist=artist, title=clean_no_br)

    if itunes_meta:
        for k in ("title", "artist", "album", "album_artist", "year", "track_number", "track_total", "album_art", "duration"):
            if itunes_meta.get(k):
                meta[k] = itunes_meta[k]
        if itunes_meta.get("apple_music_url"):
            meta["apple_music_url"] = itunes_meta["apple_music_url"]
            sl_meta = resolve_song_link(itunes_meta["apple_music_url"])
            if sl_meta:
                sl_dz = sl_meta.get("deezer_id")
                if sl_dz and validate_deezer_track(sl_dz, meta.get("title") or clean_title, meta.get("artist") or artist, meta.get("duration")):
                    meta["deezer_id"] = sl_dz
                for k in ("spotify_url", "isrc"):
                    if sl_meta.get(k) and not meta.get(k):
                        meta[k] = sl_meta[k]

    if not meta.get("deezer_id"):
        from sources.deezer import search_deezer_track
        search_art = meta.get("artist") or artist
        search_tit = meta.get("title") or clean_title
        dz_id = search_deezer_track(search_art, search_tit, duration=meta.get("duration"), album=meta.get("album"))
        if not dz_id and re.search(r"[\(\[]", search_tit):
            search_tit_clean = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", search_tit).strip()
            if search_tit_clean and search_tit_clean.lower() != search_tit.lower():
                dz_id = search_deezer_track(search_art, search_tit_clean, duration=meta.get("duration"), album=meta.get("album"))
        if not dz_id and search_art != artist:
            dz_id = search_deezer_track(artist, clean_title, duration=meta.get("duration"), album=meta.get("album"))
        if dz_id:
            meta["deezer_id"] = dz_id

    if meta.get("deezer_id"):
        dz = fetch_deezer_metadata(meta["deezer_id"])
        if dz:
            for k, v in dz.items():
                if v and not meta.get(k):
                    meta[k] = v

    album = meta.get("album")
    album_artist = meta.get("album_artist") or meta.get("artist") or ""
    if is_compilation_album(album, album_artist) or not album:
        isrc = meta.get("isrc")
        mb_data = None
        if isrc:
            mb_data = fetch_musicbrainz_by_isrc(isrc, meta.get("artist", ""))
        if not mb_data and meta.get("artist") and meta.get("title"):
            mb_data = search_musicbrainz_by_text(meta["artist"], meta["title"])
        if mb_data:
            for key in ("album", "year", "track_number", "track_total", "album_artist", "album_art", "_mb_release_id"):
                if mb_data.get(key):
                    meta[key] = mb_data[key]

    curr_art = meta.get("album_art") or ""
    if not curr_art or "sndcdn.com" in curr_art:
        best_art = fetch_best_cover_art(
            artist=meta.get("artist") or artist,
            album=meta.get("album") or "",
            title=meta.get("title") or clean_title,
            release_id=meta.get("_mb_release_id") or ""
        )
        if best_art:
            meta["album_art"] = best_art

    try:
        genres = fetch_lastfm_genres(meta.get("artist", ""), meta.get("title", ""))
        if genres:
            meta["genre"] = genres
    except Exception:
        pass

    return sanitize_metadata_strings(meta)

def sanitize_metadata_strings(meta: Optional[dict]) -> Optional[dict]:
    """Декодирует HTML-сущности (например, &#x27; -> ') и очищает строки метаданных."""
    if not meta or not isinstance(meta, dict):
        return meta
    for k in ("title", "artist", "album", "album_artist", "genre"):
        v = meta.get(k)
        if isinstance(v, str):
            meta[k] = html.unescape(v).strip()
    return meta

def get_track_metadata(url: str) -> dict:
    """
    Полный цикл извлечения метаданных:
    1. Для YouTube: специализированный разбор, поиск официального трека на других платформах.
    2. Запрос в song.link.
    3. Запрос в API Deezer и iTunes.
    4. При необходимости - поиск оригинального альбома через MusicBrainz.
    5. Получение жанров из Last.fm.
    """
    print(f"[*] Метаданные: {url}...")
    url = unshorten_url(url)
    url_lower = url.lower()

    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        yt_meta = resolve_youtube_track(url)
        if yt_meta and yt_meta.get("title") and yt_meta.get("artist"):
            return sanitize_metadata_strings(yt_meta)

    if "soundcloud.com" in url_lower or "on.soundcloud.com" in url_lower:
        sc_meta = resolve_soundcloud_track(url)
        if sc_meta and sc_meta.get("title") and sc_meta.get("artist"):
            return sanitize_metadata_strings(sc_meta)

    if "last.fm" in url_lower:
        lfm_meta = resolve_lastfm_track(url)
        if lfm_meta and lfm_meta.get("title") and lfm_meta.get("artist"):
            return sanitize_metadata_strings(lfm_meta)

    if "vk.com" in url_lower or "vk.ru" in url_lower:
        vk_meta = resolve_vk_track(url)
        if vk_meta and vk_meta.get("title") and vk_meta.get("artist"):
            return sanitize_metadata_strings(vk_meta)

    info = resolve_song_link(url)
    if not info or not info.get("title") or not info.get("artist"):
        direct_info = resolve_direct_streaming_link(url)
        if direct_info:
            if not info:
                info = direct_info
            else:
                for k, v in direct_info.items():
                    if v and not info.get(k):
                        info[k] = v
    if not info:
        return {"title": "Unknown Track", "artist": "Unknown Artist", "spotify_url": url if "spotify" in url else None}
        
    artist_query = info.get("artist") or ""
    title_query = info.get("title") or ""
    
    # Валидируем Deezer ID, полученный от song.link (отсеиваем мусорные / чужие треки)
    if info.get("deezer_id"):
        if not validate_deezer_track(info["deezer_id"], title_query, artist_query, info.get("duration")):
            info["deezer_id"] = None

    # Если Deezer ID не найден или не прошел валидацию, пробуем прямой поиск по артисту и названию
    if not info.get("deezer_id") and artist_query and title_query:
        try:
            from sources.deezer import search_deezer_track
            found_dz_id = search_deezer_track(
                artist_query,
                title_query,
                duration=info.get("duration"),
                album=info.get("album"),
            )
            if found_dz_id:
                info["deezer_id"] = found_dz_id
        except Exception:
            pass

    dz_meta = None
    if info.get("deezer_id"):
        dz_meta = fetch_deezer_metadata(info["deezer_id"])
        
    isrc_query = info.get("isrc") or (dz_meta.get("isrc") if dz_meta else None)
    
    itunes_meta = fetch_itunes_metadata(artist=artist_query, title=title_query, isrc=isrc_query)
    
    # Если song.link не связал трек со стримингами, а в iTunes нашелся трек:
    if not info.get("deezer_id") and itunes_meta and itunes_meta.get("apple_music_url"):
        apple_sl = resolve_song_link(itunes_meta["apple_music_url"])
        if apple_sl and apple_sl.get("deezer_id"):
            cand_dz = apple_sl["deezer_id"]
            if validate_deezer_track(cand_dz, info.get("title") or title_query, info.get("artist") or artist_query, info.get("duration")):
                info["deezer_id"] = cand_dz
                dz_meta = fetch_deezer_metadata(info["deezer_id"])
    
    discogs_meta = None
    if config.DISCOGS_TOKEN and artist_query and title_query:
        discogs_meta = fetch_discogs_metadata(artist_query, title_query)

    # Объединяем информацию
    if dz_meta:
        for k, v in dz_meta.items():
            if v and not info.get(k):
                info[k] = v
                
    if itunes_meta:
        for k, v in itunes_meta.items():
            if v and not info.get(k):
                info[k] = v

    if discogs_meta:
        for k, v in discogs_meta.items():
            if v and not info.get(k):
                info[k] = v
                
    isrc = info.get("isrc")
    album = info.get("album")
    album_artist = info.get("album_artist") or ""
    
    # Проверяем, не сборник ли это
    is_compilation = is_compilation_album(album, album_artist)
            
    # Если это сборник или отсутствуют метаданные, ищем в MusicBrainz
    mb_data = None
    if is_compilation or not album:
        if isrc:
            mb_data = fetch_musicbrainz_by_isrc(isrc, info.get("artist", ""))
        if not mb_data and info.get("artist") and info.get("title"):
            mb_data = search_musicbrainz_by_text(info["artist"], info["title"])
            
    if mb_data:
        for key, val in mb_data.items():
            if val:
                info[key] = val
        info.pop("_score", None)

    # Каскадный поиск студийной квадратной обложки высокого разрешения,
    # если обложка отсутствует или осталась превьюшкой с YouTube (i.ytimg.com)
    curr_art = info.get("album_art") or ""
    if not curr_art or "ytimg.com" in curr_art:
        best_art = fetch_best_cover_art(
            artist=info.get("artist") or "",
            album=info.get("album") or "",
            title=info.get("title") or "",
            release_id=info.get("_mb_release_id") or ""
        )
        if best_art:
            info["album_art"] = best_art
        
    # Получаем жанры из Last.fm
    if info.get("artist") and info.get("title"):
        genres = fetch_lastfm_genres(info["artist"], info["title"])
        if genres:
            info["genre"] = genres
            
    return sanitize_metadata_strings(info)

def resolve_query_metadata(query: str) -> Optional[dict]:
    """
    Разрешает поисковый запрос (например, 'Artist - Title') в структурированные канонические метаданные.
    Приоритет: iTunes/Apple Music (с отсевом сборников) -> song.link -> Deezer -> Last.fm.
    Фолбек: YouTube Music -> MusicBrainz.
    """
    print(f"[*] Метаданные: '{query}'...")
    
    # 1. Сначала опрашиваем официальный каталог iTunes / Apple Music.
    # Это гарантирует получение студийного альбома (а не VA / Rare RnB), канонических имен и качественного арта.
    itunes_candidate = None
    try:
        r = httpx.get("https://itunes.apple.com/search", params={
            "term": query,
            "media": "music",
            "entity": "musicTrack",
            "limit": 10
        }, timeout=8)
        if r.status_code == 200:
            results = r.json().get("results", [])
            best_it_score = -1.0
            for t in results:
                t_name = t.get("trackName", "")
                a_name = t.get("artistName", "")
                c_name = t.get("collectionName", "")
                ca_name = t.get("collectionArtistName", "")
                s = score_text_candidate(t_name, a_name, query)
                if is_compilation_album(c_name, ca_name):
                    s -= 0.45
                if t.get("trackExplicitness") == "explicit":
                    s += 0.05
                if s > best_it_score:
                    best_it_score = s
                    itunes_candidate = t
            if best_it_score < 0.45:
                itunes_candidate = None
    except Exception as e:
        print(f"[!] iTunes: Ошибка поиска: {e}")

    meta: Optional[dict] = None
    if itunes_candidate:
        title = itunes_candidate.get("trackName")
        artist = itunes_candidate.get("artistName")
        album = itunes_candidate.get("collectionName")
        album_artist = itunes_candidate.get("collectionArtistName") or artist
        year = (itunes_candidate.get("releaseDate") or "")[:4]
        art_url = (itunes_candidate.get("artworkUrl100") or "").replace("100x100bb.jpg", "1000x1000bb.jpg")
        apple_url = itunes_candidate.get("trackViewUrl")
        # Если трек помечен explicit или cleaned (зацензуренная версия), значит оригинал трека - Explicit!
        explicit = itunes_candidate.get("trackExplicitness") in ("explicit", "cleaned")
        duration = (itunes_candidate.get("trackTimeMillis", 0) / 1000.0) if itunes_candidate.get("trackTimeMillis") else None

        meta = {
            "title": title,
            "artist": artist,
            "album": album,
            "album_artist": album_artist,
            "year": year,
            "track_number": itunes_candidate.get("trackNumber"),
            "track_total": itunes_candidate.get("trackCount"),
            "album_art": art_url,
            "duration": duration,
            "explicit": explicit,
            "apple_music_url": apple_url,
            "query": query,
            "deezer_id": None,
            "isrc": None,
            "youtube_music_url": None,
            "spotify_url": None,
        }

        # Отправляем ссылку на Apple Music в song.link для связки с Deezer и ISRC
        if apple_url:
            sl_meta = resolve_song_link(apple_url)
            if sl_meta:
                sl_dz = sl_meta.get("deezer_id")
                if sl_dz and validate_deezer_track(sl_dz, meta.get("title") or title, meta.get("artist") or artist, meta.get("duration") or duration):
                    meta["deezer_id"] = sl_dz
                for k in ("spotify_url", "youtube_music_url", "isrc"):
                    if sl_meta.get(k) and not meta.get(k):
                        meta[k] = sl_meta[k]
                if sl_meta.get("duration") and not meta.get("duration"):
                    meta["duration"] = sl_meta["duration"]

        # Если найден Deezer ID, обогащаем официальными метаданными Deezer
        if not meta.get("deezer_id") and meta.get("artist") and meta.get("title"):
            try:
                from sources.deezer import search_deezer_track
                dz_id = search_deezer_track(
                    meta["artist"],
                    meta["title"],
                    duration=meta.get("duration") or duration,
                    album=meta.get("album") or album,
                )
                if dz_id:
                    meta["deezer_id"] = dz_id
            except Exception:
                pass

        if meta.get("deezer_id"):
            dz = fetch_deezer_metadata(meta["deezer_id"])
            if dz:
                for k, v in dz.items():
                    if v and not meta.get(k):
                        meta[k] = v

    # 2. Фолбек на YouTube Music, если iTunes не нашел трек
    if not meta:
        print(f"[*] YouTube Music: поиск '{query}'...")
        best_track: Optional[dict] = None
        best_score = -1.0
        try:
            from ytmusicapi import YTMusic
            yt = YTMusic()
            yt_results = yt.search(query, filter="songs", limit=10)
            for cand in yt_results:
                cand_title = cand.get("title", "")
                artists = ", ".join(a.get("name", "") for a in cand.get("artists", []))
                s = score_text_candidate(cand_title, artists, query)
                cand_album = cand.get("album", {}).get("name") if cand.get("album") else ""
                if is_compilation_album(cand_album, artists):
                    s -= 0.40
                if cand.get("isExplicit"):
                    s += 0.20
                if s > best_score:
                    best_score = s
                    art_url = None
                    thumbs = cand.get("thumbnails", [])
                    if thumbs:
                        raw_art = thumbs[-1].get("url", "")
                        art_url = re.sub(r"=w\d+-h\d+.*", "=w1000-h1000-l90-rj", raw_art)
                        if "=w1000-h1000" not in art_url and "=s" not in art_url:
                            art_url = raw_art
                            
                    best_track = {
                        "title": cand_title,
                        "artist": artists,
                        "album": cand.get("album", {}).get("name") if cand.get("album") else None,
                        "album_id": cand.get("album", {}).get("id") if cand.get("album") else None,
                        "duration": cand.get("duration_seconds"),
                        "youtube_music_url": f"https://music.youtube.com/watch?v={cand.get('videoId')}",
                        "youtube_video_id": cand.get("videoId"),
                        "album_art": art_url,
                        "album_artist": artists,
                        "explicit": bool(cand.get("isExplicit", False)),
                        "query": query,
                    }
        except Exception as e:
            print(f"[!] YouTube Music: Ошибка поиска: {e}")
            
        if best_track and best_score >= 0.45:
            meta = best_track
            if meta.get("album_id") and not meta.get("year"):
                try:
                    album_data = yt.get_album(meta["album_id"])
                    if album_data:
                        meta["year"] = album_data.get("year")
                        meta["track_total"] = album_data.get("trackCount")
                        for idx, tr in enumerate(album_data.get("tracks", []), 1):
                            if tr.get("videoId") == meta.get("youtube_video_id"):
                                meta["track_number"] = idx
                                break
                except Exception:
                    pass

    if not meta:
        return None

    # 3. Фильтрация сборников: если альбом все еще является сборником, восстанавливаем через MusicBrainz
    album = meta.get("album")
    album_artist = meta.get("album_artist") or meta.get("artist") or ""
    if is_compilation_album(album, album_artist) or not album:
        isrc = meta.get("isrc")
        mb_data = None
        if isrc:
            mb_data = fetch_musicbrainz_by_isrc(isrc, meta.get("artist", ""))
        if not mb_data and meta.get("artist") and meta.get("title"):
            mb_data = search_musicbrainz_by_text(meta["artist"], meta["title"])
        if mb_data:
            for key in ("album", "year", "track_number", "track_total", "album_artist", "album_art", "_mb_release_id"):
                if mb_data.get(key):
                    meta[key] = mb_data[key]

    # Каскадный поиск студийной квадратной обложки высокого разрешения,
    # если обложка отсутствует или осталась превьюшкой с YouTube (i.ytimg.com)
    curr_art = meta.get("album_art") or ""
    if not curr_art or "ytimg.com" in curr_art:
        best_art = fetch_best_cover_art(
            artist=meta.get("artist") or "",
            album=meta.get("album") or "",
            title=meta.get("title") or "",
            release_id=meta.get("_mb_release_id") or ""
        )
        if best_art:
            meta["album_art"] = best_art

    # 4. Получение жанров из Last.fm
    try:
        genres = fetch_lastfm_genres(meta.get("artist", ""), meta.get("title", ""))
        if genres:
            meta["genre"] = genres
    except Exception:
        pass

    return sanitize_metadata_strings(meta)


def search_multisource_tracks(query: str, limit: int = 8) -> list[dict]:
    """
    Агрегированный поиск треков по нескольким источникам (Deezer, iTunes, YouTube Music).
    Возвращает список структурированных кандидатов с рейтингом релевантности.
    """
    query = (query or "").strip()
    if not query:
        return []

    candidates: list[dict] = []
    seen_keys: set[str] = set()

    # 1. Поиск в Deezer (даёт доступ к FLAC и MP3 320k)
    try:
        r = httpx.get("https://api.deezer.com/search", params={"q": query, "limit": limit * 2}, timeout=6.0)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                t_id = str(item.get("id"))
                t_name = item.get("title", "").strip()
                a_name = item.get("artist", {}).get("name", "").strip()
                if not t_name or not a_name:
                    continue
                key = f"{a_name.lower()}::{t_name.lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                alb = item.get("album", {})
                cand = {
                    "title": t_name,
                    "artist": a_name,
                    "album": alb.get("title"),
                    "year": (alb.get("release_date") or "")[:4],
                    "duration": item.get("duration"),
                    "deezer_id": t_id,
                    "album_art": alb.get("cover_xl") or alb.get("cover_big"),
                    "source": "Deezer",
                    "source_quality": "Deezer (FLAC / 320k)",
                    "explicit": bool(item.get("explicit_lyrics")),
                    "url": f"https://www.deezer.com/track/{t_id}",
                    "score": score_text_candidate(t_name, a_name, query),
                }
                candidates.append(cand)
    except Exception as e:
        print(f"[!] Deezer search error: {e}")

    # 2. Поиск в iTunes / Apple Music (даёт точные года, studio-альбомы и 1000x1000 арт)
    try:
        r = httpx.get("https://itunes.apple.com/search", params={
            "term": query,
            "media": "music",
            "entity": "musicTrack",
            "limit": limit * 2
        }, timeout=6.0)
        if r.status_code == 200:
            for t in r.json().get("results", []):
                t_name = (t.get("trackName") or "").strip()
                a_name = (t.get("artistName") or "").strip()
                if not t_name or not a_name:
                    continue
                key = f"{a_name.lower()}::{t_name.lower()}"
                existing = next((c for c in candidates if f"{c['artist'].lower()}::{c['title'].lower()}" == key), None)
                if existing:
                    if not existing.get("year") and t.get("releaseDate"):
                        existing["year"] = t["releaseDate"][:4]
                    if not existing.get("album_art") and t.get("artworkUrl100"):
                        existing["album_art"] = t["artworkUrl100"].replace("100x100bb.jpg", "1000x1000bb.jpg")
                    continue

                seen_keys.add(key)
                cand = {
                    "title": t_name,
                    "artist": a_name,
                    "album": t.get("collectionName"),
                    "year": (t.get("releaseDate") or "")[:4],
                    "duration": int(t.get("trackTimeMillis", 0) / 1000) if t.get("trackTimeMillis") else None,
                    "deezer_id": None,
                    "album_art": (t.get("artworkUrl100") or "").replace("100x100bb.jpg", "1000x1000bb.jpg"),
                    "source": "Apple Music",
                    "source_quality": "Apple Music (256k AAC)",
                    "explicit": t.get("trackExplicitness") == "explicit",
                    "url": t.get("trackViewUrl"),
                    "score": score_text_candidate(t_name, a_name, query),
                }
                candidates.append(cand)
    except Exception as e:
        print(f"[!] iTunes search error: {e}")

    # 3. YouTube Music поиск (для треков, отсутствующих в Deezer/iTunes)
    if len(candidates) < limit:
        try:
            from ytmusicapi import YTMusic
            yt = YTMusic()
            yt_res = yt.search(query, filter="songs", limit=limit)
            for item in yt_res:
                t_name = (item.get("title") or "").strip()
                arts = item.get("artists", [])
                a_name = ", ".join(a.get("name", "") for a in arts if isinstance(a, dict)) or item.get("author") or ""
                a_name = a_name.strip()
                if not t_name:
                    continue
                key = f"{a_name.lower()}::{t_name.lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                vid = item.get("videoId")
                alb_info = item.get("album")
                alb_title = alb_info.get("name") if isinstance(alb_info, dict) else (alb_info or "")
                cand = {
                    "title": t_name,
                    "artist": a_name or "Unknown Artist",
                    "album": alb_title or None,
                    "year": item.get("year"),
                    "duration": item.get("duration_seconds"),
                    "deezer_id": None,
                    "album_art": (item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None),
                    "source": "YouTube Music",
                    "source_quality": "YouTube Music (256k VBR)",
                    "explicit": bool(item.get("isExplicit")),
                    "url": f"https://music.youtube.com/watch?v={vid}" if vid else None,
                    "score": score_text_candidate(t_name, a_name, query) * 0.95,
                }
                candidates.append(cand)
        except Exception:
            pass

    # Сортируем по оценке совпадения
    candidates.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return candidates[:limit]

