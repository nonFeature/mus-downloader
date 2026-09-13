import httpx
import re
import urllib.parse
from typing import Optional
import config

USER_AGENT = "MusicDownloader/1.0 (akate@gmail.com)"

def resolve_spotify_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека напрямую со страницы Spotify."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', r.text)
        image_m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        
        title = title_m.group(1) if title_m else ""
        desc = desc_m.group(1) if desc_m else ""
        art = image_m.group(1) if image_m else ""
        
        parts = [p.strip() for p in desc.split("·")]
        artist = parts[0] if parts else ""
        album = parts[1] if len(parts) > 2 else ""
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
        print(f"[!] Ошибка разбора ссылки Spotify: {e}")
    return None

def resolve_apple_music_track(url: str) -> Optional[dict]:
    """Извлекает метаданные трека напрямую из Apple Music / iTunes."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=10)
        final_url = str(r.url)
        m = re.search(r"[?&]i=(\d+)", final_url)
        track_id = m.group(1) if m else None
        if not track_id:
            m2 = re.search(r"/album/[^/]+/(\d+)", final_url)
            track_id = m2.group(1) if m2 else None
        if track_id:
            lookup = httpx.get(f"https://itunes.apple.com/lookup?id={track_id}", timeout=10)
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
        print(f"[!] Ошибка разбора ссылки Apple Music: {e}")
    return None

def resolve_deezer_track(url: str) -> Optional[dict]:
    """Извлекает метаданные напрямую из Deezer API."""
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=8)
        m = re.search(r"track/(\d+)", str(r.url))
        if m:
            track_id = m.group(1)
            meta = httpx.get(f"https://api.deezer.com/track/{track_id}", timeout=8).json()
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
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, follow_redirects=True, timeout=10)
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
        print(f"[!] Ошибка разбора ссылки Яндекс Музыки: {e}")
    return None

def resolve_direct_streaming_link(url: str) -> Optional[dict]:
    """Разрешает метаданные напрямую из ссылки на стриминговый сервис."""
    url_lower = url.lower()
    if "spotify.com" in url_lower:
        print("[*] Прямой опрос Spotify для получения метаданных...")
        res = resolve_spotify_track(url)
        if res: return res
    elif "apple.com" in url_lower:
        print("[*] Прямой опрос Apple Music для получения метаданных...")
        res = resolve_apple_music_track(url)
        if res: return res
    elif "deezer.com" in url_lower or "deezer.page.link" in url_lower:
        print("[*] Прямой опрос Deezer для получения метаданных...")
        res = resolve_deezer_track(url)
        if res: return res
    elif "music.yandex" in url_lower or "yandex.ru/album" in url_lower:
        print("[*] Прямой опрос Яндекс Музыки для получения метаданных...")
        res = resolve_yandex_music_track(url)
        if res: return res
    return None

def resolve_song_link(url: str) -> Optional[dict]:
    """
    Запрашивает song.link (Odesli) и извлекает связи между платформами и метаданные.
    Использует веб-скрейпинг Next.js payload (__NEXT_DATA__), что не требует API-ключей.
    """
    try:
        # 1. Сначала пробуем парсинг через веб-интерфейс song.link (не требует API ключа)
        songlink_url = f"https://song.link/{url}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = httpx.get(songlink_url, headers=headers, follow_redirects=True, timeout=10)
        if resp.status_code == 200:
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">({.*?})</script>', resp.text)
            if m:
                import json
                data = json.loads(m.group(1))
                page_data = data.get("props", {}).get("pageProps", {}).get("pageData", {})
                entity = page_data.get("entityData", {})
                if entity:
                    release_date = entity.get("releaseDate", {})
                    year = str(release_date.get("year")) if isinstance(release_date, dict) and release_date.get("year") else None
                    
                    duration_ms = entity.get("duration")
                    duration_s = duration_ms / 1000.0 if duration_ms else None
                    
                    result = {
                        "title": entity.get("title", ""),
                        "artist": entity.get("artistName", ""),
                        "album_art": entity.get("thumbnailUrl", ""),
                        "year": year,
                        "isrc": entity.get("isrc"),
                        "duration": duration_s,
                        "track_number": entity.get("trackNumber"),
                        "deezer_id": None,
                        "spotify_url": None,
                        "youtube_music_url": None,
                        "apple_music_url": None,
                    }
                    
                    for sec in page_data.get("sections", []):
                        links = sec.get("links") or sec.get("items") or []
                        for l in links:
                            plat = l.get("platform")
                            item_url = l.get("url")
                            uniq = l.get("uniqueId", "")
                            if plat == "deezer" or "deezer" in uniq:
                                m_dz = re.search(r"track/(\d+)", item_url or uniq)
                                if m_dz:
                                    result["deezer_id"] = m_dz.group(1)
                            elif plat == "spotify":
                                result["spotify_url"] = item_url
                            elif plat in ("youtubeMusic", "youtube"):
                                if not result["youtube_music_url"]:
                                    result["youtube_music_url"] = item_url
                            elif plat == "appleMusic":
                                result["apple_music_url"] = item_url
                                
                    if result.get("title") and result.get("artist"):
                        return result
    except Exception as e:
        pass

    # 2. Фолбек на api.song.link (если в будущем снова откроют или при наличии ключа)
    try:
        encoded_url = urllib.parse.quote(url)
        api_url = f"https://api.song.link/v1-alpha.1/links?url={encoded_url}"
        resp = httpx.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            entities = data.get("entitiesByUniqueId", {})
            if entities:
                first_entity = next(iter(entities.values()))
                result = {
                    "title": first_entity.get("title", ""),
                    "artist": first_entity.get("artistName", ""),
                    "album_art": first_entity.get("thumbnailUrl", ""),
                    "deezer_id": None,
                    "yandex_id": None,
                    "tidal_id": None,
                    "youtube_music_url": None,
                    "soundcloud_url": None,
                    "spotify_url": None,
                    "isrc": None,
                }
                for entity_id, entity in entities.items():
                    provider = entity.get("apiProvider")
                    if entity.get("type") == "song" and provider == "deezer":
                        result["deezer_id"] = entity.get("id")
                return result
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
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=4.0)
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
        print(f"[!] Ошибка запроса метаданных из Deezer: {e}")
    return None

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
        resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[!] Ошибка запроса записи MusicBrainz по ID: {e}")
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
        resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
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
        print(f"[!] Ошибка поиска по ISRC в MusicBrainz: {e}")
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
        resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
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
        print(f"[!] Ошибка текстового поиска в MusicBrainz: {e}")
    return None

def score_text_candidate(cand_title: str, cand_artist: str, target_query: str) -> float:
    """
    Оценивает совпадение кандидата с целевым запросом по токенам и штрафует
    за лишние маркеры версий (ремиксы, каверы, караоке, лайвы).
    """
    from sources.youtube_matcher import toks, _version_markers
    q_toks = toks(target_query)
    c_toks = toks(f"{cand_artist} {cand_title}")
    if not q_toks:
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
                    s = score_text_candidate(track.get("trackName", ""), track.get("artistName", ""), target_query)
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
                
                return {
                    "title": best_track.get("trackName"),
                    "artist": best_track.get("artistName"),
                    "album": best_track.get("collectionName"),
                    "year": year,
                    "track_number": best_track.get("trackNumber"),
                    "track_total": best_track.get("trackCount"),
                    "album_artist": best_track.get("artistName"),
                    "album_art": art_url,
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
        r = httpx.get(url, params=params, timeout=10)
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
            r = httpx.get(url, params=params, timeout=10)
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

def get_track_metadata(url: str) -> dict:
    """
    Полный цикл извлечения метаданных:
    1. Запрос в song.link.
    2. Запрос в API Deezer и iTunes.
    3. При необходимости - поиск оригинального альбома через MusicBrainz.
    4. Получение жанров из Last.fm.
    """
    print(f"[*] Разрешение метаданных для: {url}")
    info = resolve_song_link(url)
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
        
    dz_meta = None
    if info.get("deezer_id"):
        print("[*] Получение метаданных напрямую из Deezer API...")
        dz_meta = fetch_deezer_metadata(info["deezer_id"])
        
    artist_query = info.get("artist") or ""
    title_query = info.get("title") or ""
    isrc_query = info.get("isrc") or (dz_meta.get("isrc") if dz_meta else None)
    
    print("[*] Получение метаданных из iTunes API...")
    itunes_meta = fetch_itunes_metadata(artist=artist_query, title=title_query, isrc=isrc_query)
    
    discogs_meta = None
    if config.DISCOGS_TOKEN and artist_query and title_query:
        print("[*] Получение метаданных из Discogs API...")
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
            print(f"[*] Ищем оригинальный альбом в MusicBrainz по ISRC: {isrc}")
            mb_data = fetch_musicbrainz_by_isrc(isrc, info.get("artist", ""))
        if not mb_data and info.get("artist") and info.get("title"):
            print(f"[*] Ищем оригинальный альбом в MusicBrainz по тексту: {info['artist']} - {info['title']}")
            mb_data = search_musicbrainz_by_text(info["artist"], info["title"])
            
    if mb_data:
        for key, val in mb_data.items():
            if val:
                info[key] = val
        info.pop("_score", None)
        
    # Получаем жанры из Last.fm
    if info.get("artist") and info.get("title"):
        print("[*] Получение жанров из Last.fm...")
        genres = fetch_lastfm_genres(info["artist"], info["title"])
        if genres:
            info["genre"] = genres
            print(f"[+] Получены жанры: {genres}")
            
    return info

def resolve_query_metadata(query: str) -> Optional[dict]:
    """
    Разрешает поисковый запрос (например, 'Artist - Title') в структурированные канонические метаданные.
    Приоритет: iTunes/Apple Music (с отсевом сборников) -> song.link -> Deezer -> Last.fm.
    Фолбек: YouTube Music -> MusicBrainz.
    """
    print(f"[*] Поиск канонических метаданных для: '{query}'")
    
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
            print(f"[*] Разрешение связей трека через song.link...")
            sl_meta = resolve_song_link(apple_url)
            if sl_meta:
                for k in ("deezer_id", "spotify_url", "youtube_music_url", "isrc"):
                    if sl_meta.get(k) and not meta.get(k):
                        meta[k] = sl_meta[k]
                if sl_meta.get("duration") and not meta.get("duration"):
                    meta["duration"] = sl_meta["duration"]

        # Если найден Deezer ID, обогащаем официальными метаданными Deezer
        if not meta.get("deezer_id") and meta.get("artist") and meta.get("title"):
            try:
                from sources.deezer import search_deezer_track
                dz_id = search_deezer_track(meta["artist"], meta["title"])
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
        print(f"[*] Поиск трека в каталоге YouTube Music: '{query}'")
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
            print(f"[*] Ищем оригинальный студийный альбом в MusicBrainz по ISRC: {isrc}")
            mb_data = fetch_musicbrainz_by_isrc(isrc, meta.get("artist", ""))
        if not mb_data and meta.get("artist") and meta.get("title"):
            print(f"[*] Ищем оригинальный студийный альбом в MusicBrainz по тексту: {meta['artist']} - {meta['title']}")
            mb_data = search_musicbrainz_by_text(meta["artist"], meta["title"])
        if mb_data:
            for key in ("album", "year", "track_number", "track_total", "album_artist"):
                if mb_data.get(key):
                    meta[key] = mb_data[key]

    # 4. Получение жанров из Last.fm
    try:
        genres = fetch_lastfm_genres(meta.get("artist", ""), meta.get("title", ""))
        if genres:
            meta["genre"] = genres
    except Exception:
        pass

    return meta
