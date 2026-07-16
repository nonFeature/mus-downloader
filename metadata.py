import httpx
import re
import urllib.parse
from typing import Optional

USER_AGENT = "MusicDownloader/1.0 (akate@gmail.com)"

def resolve_song_link(url: str) -> Optional[dict]:
    """
    Запрашивает song.link (Odesli) API и возвращает связи и базовые метаданные.
    """
    encoded_url = urllib.parse.quote(url)
    api_url = f"https://api.song.link/v1-alpha.1/links?url={encoded_url}"
    
    try:
        resp = httpx.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code != 200:
            print(f"[!] Ошибка song.link: {resp.status_code}")
            return None
        
        data = resp.json()
        entities = data.get("entitiesByUniqueId", {})
        if not entities:
            return None
        
        # Берем первый попавшийся трек для базовых метаданных
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
        
        # Ищем платформенные идентификаторы и ссылки
        for entity_id, entity in entities.items():
            provider = entity.get("apiProvider")
            entity_type = entity.get("type")
            if entity_type != "song":
                continue
            
            if provider == "deezer":
                result["deezer_id"] = entity.get("id")
            elif provider == "yandex":
                result["yandex_id"] = entity.get("id")
            elif provider == "tidal":
                result["tidal_id"] = entity.get("id")
                
        # Извлекаем ссылки по платформам
        links = data.get("linksByPlatform", {})
        if "deezer" in links:
            # Если нет в entities, вытаскиваем из ссылки
            dz_url = links["deezer"].get("url", "")
            if dz_url and not result["deezer_id"]:
                match = re.search(r"track/(\d+)", dz_url)
                if match:
                    result["deezer_id"] = match.group(1)
                    
        if "tidal" in links:
            td_url = links["tidal"].get("url", "")
            if td_url and not result["tidal_id"]:
                match = re.search(r"track/(\d+)", td_url)
                if match:
                    result["tidal_id"] = match.group(1)
                    
        if "yandex" in links:
            ya_url = links["yandex"].get("url", "")
            if ya_url and not result["yandex_id"]:
                match = re.search(r"track/(\d+)", ya_url)
                if match:
                    result["yandex_id"] = match.group(1)
                    
        if "youtubeMusic" in links:
            result["youtube_music_url"] = links["youtubeMusic"].get("url")
        elif "youtube" in links:
            result["youtube_music_url"] = links["youtube"].get("url")
            
        if "soundcloud" in links:
            result["soundcloud_url"] = links["soundcloud"].get("url")
            
        if "spotify" in links:
            result["spotify_url"] = links["spotify"].get("url")
            
        return result
        
    except Exception as e:
        print(f"[!] Не удалось разрешить ссылку через song.link: {e}")
    return None

def fetch_deezer_metadata(deezer_id: str) -> Optional[dict]:
    """
    Запрашивает публичный API Deezer для получения полной информации о треке.
    """
    if not deezer_id:
        return None
    url = f"https://api.deezer.com/track/{deezer_id}"
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
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
                    
            return {
                "isrc": data.get("isrc"),
                "album": album_info.get("title"),
                "year": year,
                "track_number": data.get("track_position"),
                "album_artist": artist_info.get("name"),
                "album_art": album_info.get("cover_xl") or album_info.get("cover_big") or album_info.get("cover_medium"),
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
    _bad_title_fragments = [
        "various", "now that's what", "hits ", "greatest hits", "best of", 
        "collection", "the very best", "extracts", "sampler", "promo", "compilation"
    ]
    if any(frag in title for frag in _bad_title_fragments):
        score -= 12
        
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

def get_track_metadata(url: str) -> dict:
    """
    Полный цикл извлечения метаданных:
    1. Запрос в song.link.
    2. Запрос в API Deezer (для моментального получения альбома и ISRC).
    3. При необходимости (если это сборник) - поиск студийного альбома через MusicBrainz.
    """
    print(f"[*] Разрешение метаданных для: {url}")
    info = resolve_song_link(url)
    if not info:
        return {"title": "Unknown Track", "artist": "Unknown Artist", "spotify_url": url if "spotify" in url else None}
        
    dz_meta = None
    if info.get("deezer_id"):
        print("[*] Получение метаданных напрямую из Deezer API...")
        dz_meta = fetch_deezer_metadata(info["deezer_id"])
        
    isrc = None
    if dz_meta:
        isrc = dz_meta.get("isrc")
        info["isrc"] = isrc
        
        # Проверяем, не сборник ли это
        album = dz_meta.get("album")
        album_artist = dz_meta.get("album_artist") or ""
        
        is_compilation = False
        if album_artist.lower() in ("various artists", "various"):
            is_compilation = True
        else:
            _bad_words = ["various", "now that's what", "hits ", "greatest hits", "best of", "collection", "the very best", "compilation"]
            if album and any(w in album.lower() for w in _bad_words):
                is_compilation = True
                
        if not is_compilation:
            # Напрямую используем чистые метаданные из Deezer (это очень быстро!)
            print(f"[+] Найден оригинальный альбом в Deezer: '{album}'")
            for key, val in dz_meta.items():
                if val:
                    info[key] = val
            return info
        else:
            print(f"[*] Deezer вернул сборник '{album}'. Попробуем найти оригинальный альбом в MusicBrainz...")
            
    # Если это сборник или Deezer не дал метаданных, ищем через MusicBrainz
    mb_data = None
    if isrc:
        print(f"[*] Ищем оригинальный альбом по ISRC: {isrc}")
        mb_data = fetch_musicbrainz_by_isrc(isrc, info.get("artist", ""))
        
    if not mb_data and info.get("artist") and info.get("title"):
        print(f"[*] Ищем оригинальный альбом по тексту: {info['artist']} - {info['title']}")
        mb_data = search_musicbrainz_by_text(info["artist"], info["title"])
        
    if mb_data:
        # Применяем найденные MusicBrainz метаданные поверх
        for key, val in mb_data.items():
            if val:
                info[key] = val
        info.pop("_score", None)
    elif dz_meta:
        # Если MusicBrainz ничего не нашел/был заблокирован, откатываемся на исходные метаданные Deezer
        print("[*] Студийный альбом не найден в MusicBrainz, оставляем исходные данные из Deezer.")
        for key, val in dz_meta.items():
            if val:
                info[key] = val
                
    return info
