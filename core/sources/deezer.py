import hashlib
import re
import time
import httpx
from pathlib import Path
from Crypto.Cipher import Blowfish
from typing import Optional, Callable

PROXY_API = "https://lufts-dzmedia.fly.dev/get_url"
SECRET = b"g4el58wc0zvf9na1"

# Маппинг качеств
QUALITIES = {
    "FLAC": {"proxyName": "FLAC", "ext": "flac"},
    "MP3_320": {"proxyName": "MP3_320", "ext": "mp3"},
    "MP3_128": {"proxyName": "MP3_128", "ext": "mp3"},
}

def get_blowfish_key(track_id: str) -> bytes:
    """Генерирует Blowfish-ключ для расшифровки трека по его ID."""
    md5 = hashlib.md5(str(track_id).encode()).hexdigest()
    key = bytearray(16)
    for i in range(16):
        key[i] = ord(md5[i]) ^ ord(md5[i + 16]) ^ SECRET[i]
    return bytes(key)

def decrypt_and_save(stream_iterator, bf_key: bytes, dest_path: Path):
    """Дешифрует и сохраняет стрим трека."""
    iv = bytes([0, 1, 2, 3, 4, 5, 6, 7])
    buffer = bytearray()
    chunk_idx = 0

    with open(dest_path, "wb") as f:
        for chunk in stream_iterator:
            buffer.extend(chunk)
            while len(buffer) >= 2048:
                block = bytes(buffer[:2048])
                del buffer[:2048]

                if chunk_idx % 3 == 0:
                    cipher = Blowfish.new(bf_key, Blowfish.MODE_CBC, iv)
                    decrypted = cipher.decrypt(block)
                    f.write(decrypted)
                else:
                    f.write(block)
                chunk_idx += 1

        # Записываем остаток, который не шифруется
        if len(buffer) > 0:
            f.write(bytes(buffer))

def download_deezer_track(
    track_id: str,
    dest_dir: Path,
    target_quality: str = "FLAC",
    artist: str = "",
    title: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> Optional[Path]:
    """
    Скачивает трек из Deezer по ID.
    target_quality: 'FLAC', 'MP3_320' или 'MP3_128'.
    Возвращает путь к скачанному файлу или None.
    Никогда не маскирует MP3 под FLAC.
    """
    if target_quality == "MP3":
        target_quality = "MP3_320"
    elif target_quality not in QUALITIES:
        target_quality = "MP3_320"
        
    quality_info = QUALITIES[target_quality]

    try:
        # 1. Запрашиваем URL потока через прокси-сервер Echo
        payload = {
            "ids": [int(track_id)],
            "formats": [quality_info["proxyName"]]
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        
        resp = httpx.post(PROXY_API, json=payload, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"[!] Deezer: Прокси вернул код {resp.status_code}")
            return None
            
        data = resp.json()
        tracks_data = data.get("data")
        if not tracks_data or not tracks_data[0].get("media"):
            print(f"[!] Deezer: Трек {track_id} не найден или недоступен")
            return None
            
        media_list = tracks_data[0]["media"]
        # Ищем желаемый формат
        media = next((m for m in media_list if m.get("format") == quality_info["proxyName"]), None)
        
        # Если запрашивался FLAC, но его нет - НЕ сохраняем MP3 как .flac, а выходим для Soulseek
        if not media:
            available_formats = [m.get("format") for m in media_list]
            if target_quality == "FLAC":
                print(f"[!] Deezer: FLAC недоступен (сервер вернул только: {available_formats}). Переход на Soulseek...")
                return None
            # Для MP3 режима берём лучший доступный
            media = media_list[0]
            
        if not media.get("sources"):
            print(f"[!] Deezer: Отсутствуют источники для воспроизведения")
            return None
            
        stream_url = media["sources"][0]["url"]
        actual_format = media.get("format", "")
        actual_ext = "flac" if actual_format == "FLAC" else "mp3"
        
        # Получаем метаданные артиста и названия для имени файла
        artist_name = artist
        track_title = title
        if not artist_name or not track_title:
            try:
                meta_url = f"https://api.deezer.com/track/{track_id}"
                meta_resp = httpx.get(meta_url, timeout=3.0)
                if meta_resp.status_code == 200:
                    meta = meta_resp.json()
                    artist_name = artist_name or meta.get("artist", {}).get("name")
                    track_title = track_title or meta.get("title")
            except Exception:
                pass
                
        artist_name = artist_name or "Unknown Artist"
        track_title = track_title or f"Track_{track_id}"
        
        # Очищаем имя файла от недопустимых символов
        clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist_name} - {track_title}")
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        output_filename = f"{clean_name}.{actual_ext}"
        output_path = dest_dir / output_filename

        # 2. Скачиваем зашифрованный поток и дешифруем его на лету
        print(f"[*] Deezer: скачивание {actual_format}...")
        bf_key = get_blowfish_key(track_id)
        
        with httpx.stream("GET", stream_url, headers=headers, timeout=30) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get("Content-Length", 0))

            def _tracked_iter(iterable, total_sz):
                downloaded = 0
                start_time = time.time()
                for chunk in iterable:
                    downloaded += len(chunk)
                    if progress_callback:
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        eta = (total_sz - downloaded) / speed if (speed > 0 and total_sz > downloaded) else None
                        progress_callback({
                            "stage": "download",
                            "source": "Deezer",
                            "description": f"Deezer: скачивание {actual_format}...",
                            "downloaded_bytes": downloaded,
                            "total_bytes": total_sz,
                            "speed": speed,
                            "eta": eta,
                        })
                    yield chunk

            stream_iter = _tracked_iter(r.iter_bytes(chunk_size=4096), total_bytes) if progress_callback else r.iter_bytes(chunk_size=4096)
            decrypt_and_save(stream_iter, bf_key, output_path)
            
        print(f"[+] Deezer: скачан {output_path.name}")
        return output_path

    except Exception as e:
        print(f"[!] Deezer: Ошибка скачивания трека {track_id}: {e}")
    return None

JUNK_TITLE_PATTERNS = [
    r"(?i)\bas\s+made\s+famous\s+by\b",
    r"(?i)\boriginally\s+performed\s+by\b",
    r"(?i)\bin\s+the\s+style\s+of\b",
    r"(?i)\btribute\s+to\b",
    r"(?i)\btribute\s+(?:version|mix|rendition)?\b",
    r"(?i)\bcover\s+(?:version|mix|rendition)?\b",
    r"(?i)\bmelody\s+karaoke\b",
    r"(?i)\bkaraoke\s+(?:version|mix|instrumental)?\b",
    r"(?i)\bbacking\s+track\b",
    r"(?i)\bminus\s+(?:one|track)\b",
    r"(?i)\bsing-?along\b",
    r"(?i)\bpiano\s+version\b",
    r"(?i)\bstring\s+quartet\b",
    r"(?i)\borchestral\s+version\b",
    r"(?i)\blullaby\s+(?:version|rendition)?\b",
    r"(?i)\b8-?bit\s+(?:version|rendition)?\b",
    r"(?i)\bacoustic\s+tribute\b",
    r"(?i)\bparody\s+version\b",
    r"(?i)\bprod\.\s+(?:by\s+)?",
    r"(?i)\btype\s+beat\b",
    r"(?i)\b(?:guitar|bass|drum)\s+backing\b",
    r"(?i)\bmidi\s+version\b",
]

JUNK_ARTIST_PATTERNS = [
    r"(?i)\bkaraoke\b",
    r"(?i)\btribute\s+(?:band|players|ensemble|orchestra|project)\b",
    r"(?i)\bchart\s+toppers\b",
    r"(?i)\bhit\s+makers\b",
    r"(?i)\bsmooth\s+jazz\s+all\s+stars\b",
    r"(?i)\bstring\s+quartet\b",
    r"(?i)\blullaby\s+players\b",
    r"(?i)\b8-?bit\b",
    r"(?i)\btwinkle\s+twinkle\b",
    r"(?i)\bameritz\b",
    r"(?i)\bmidifine\b",
    r"(?i)\bsunfly\b",
    r"(?i)\bkarafun\b",
    r"(?i)\bzzang\b",
    r"(?i)\bthepianokid\b",
    r"(?i)\bparty\s+tyme\b",
    r"(?i)\bzoom\s+karaoke\b",
    r"(?i)\bprosound\b",
    r"(?i)\bpiano\s+tribute\b",
]

def is_junk_track(cand_title: str, cand_artist: str, target_query: str = "") -> bool:
    """
    Проверяет, является ли кандидат караоке, трибьютом, левым кавером или инструменталкой.
    НЕ фильтрует по автору напрямую, а отсекает именно имитационные и караоке паттерны.
    """
    t_low = (target_query or "").lower()
    c_tit = (cand_title or "").strip()
    c_art = (cand_artist or "").strip()

    for pat in JUNK_TITLE_PATTERNS:
        if re.search(pat, c_tit) and not re.search(pat, t_low):
            return True

    for pat in JUNK_ARTIST_PATTERNS:
        if re.search(pat, c_art) and not re.search(pat, t_low):
            return True

    # Проверка на упоминание автора в названии как источника для подражания:
    # "Flashing Lights (Feat. Dwele) (By Kanye West) (Melody Karaoke Version)"
    if re.search(r"(?i)\b(?:originally\s+by|as\s+made\s+famous\s+by|tribute\s+to|in\s+the\s+style\s+of)\b", c_tit):
        return True

    return False

def search_deezer_track(
    artist: str,
    title: str,
    duration: Optional[float] = None,
    album: Optional[str] = None,
) -> Optional[str]:
    """
    Ищет трек по артисту и названию на Deezer с валидацией версий (без нежелательных ремиксов),
    поддержкой коллабораций, нормализацией диакритики, поиском по альбому и проверкой длительности.
    Возвращает track_id наиболее подходящего совпадения.
    """
    import unicodedata
    from sources.youtube_matcher import toks, _version_markers

    def strip_accents(s: str) -> str:
        d = unicodedata.normalize("NFD", s)
        return "".join(c for c in d if unicodedata.category(c) != "Mn")

    collab_parts = [p.strip() for p in re.split(r"(?i)\s*(?:&|and|feat\.?|ft\.?|,|\+|/)\s*", artist) if p.strip()]
    primary_artist = collab_parts[0] if collab_parts else artist

    clean_title_no_feat = re.sub(r"(?i)\s*[\(\[]?\s*f(?:ea)?t\.?[^\)\]]*[\)\]]?", "", title).strip()

    target_query = f"{artist} {title}"
    target_toks = toks(target_query)
    target_title_toks = toks(clean_title_no_feat or title)
    target_artist_toks = toks(artist)
    target_vm = _version_markers(target_query)

    # 1. Если передан альбом, пробуем найти официальный альбом и точный трек внутри него
    if album and primary_artist:
        clean_album = re.sub(r"(?i)\s*[\(\[][^\)\]]+[\)\]]", "", album).strip()
        alb_queries = [f"{primary_artist} {clean_album}".strip()]
        if len(collab_parts) > 1 and collab_parts[1]:
            alb_queries.append(f"{collab_parts[1]} {clean_album}".strip())
        alb_queries.append(clean_album)

        for aq in alb_queries:
            try:
                r_alb = httpx.get(
                    "https://api.deezer.com/search/album",
                    params={"q": aq, "limit": 3},
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
                    timeout=httpx.Timeout(6.0, connect=4.0)
                )
                if r_alb.status_code == 200:
                    for alb_cand in r_alb.json().get("data", []):
                        alb_id = alb_cand.get("id")
                        if alb_id:
                            r_trks = httpx.get(
                                f"https://api.deezer.com/album/{alb_id}/tracks",
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
                                timeout=httpx.Timeout(6.0, connect=4.0)
                            )
                            if r_trks.status_code == 200:
                                for alb_track in r_trks.json().get("data", []):
                                    t_name = alb_track.get("title", "")
                                    t_toks = toks(t_name)
                                    if target_title_toks and (target_title_toks & t_toks):
                                        overlap = len(target_title_toks & t_toks) / len(target_title_toks)
                                        if overlap >= 0.70 and not is_junk_track(t_name, primary_artist, target_query):
                                            return str(alb_track["id"])
            except Exception:
                pass
    
    queries = [f"{artist} {title}"]
    clean_art_no_accents = strip_accents(artist)
    clean_tit_no_accents = strip_accents(clean_title_no_feat or title)
    if clean_art_no_accents.lower() != artist.lower():
        queries.append(f"{clean_art_no_accents} {clean_tit_no_accents}")

    if clean_title_no_feat and clean_title_no_feat.lower() != title.lower():
        queries.append(f"{artist} {clean_title_no_feat}")

    # Добавляем запросы по каждому участнику коллаборации (например, "JAY Z Otis", "Kanye West Otis")
    for part in collab_parts:
        if part:
            queries.append(f"{part} {clean_title_no_feat or title}")
            part_no_accents = strip_accents(part)
            if part_no_accents.lower() != part.lower():
                queries.append(f"{part_no_accents} {clean_tit_no_accents}")

    seen_queries = set()
    best_id = None
    best_score = -1.0

    for query in queries:
        q_norm = query.strip().lower()
        if not q_norm or q_norm in seen_queries:
            continue
        seen_queries.add(q_norm)

        try:
            r = httpx.get(
                "https://api.deezer.com/search",
                params={"q": query, "limit": 15},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
                timeout=httpx.Timeout(10.0, connect=6.0)
            )
            if r.status_code != 200:
                continue
            data = r.json().get("data", [])
            if not data:
                continue

            for track in data:
                track_title = track.get("title", "")
                track_artist = track.get("artist", {}).get("name", "")

                # 1. Отсеиваем караоке, трибьюты, левые инструменталки
                if is_junk_track(track_title, track_artist, target_query):
                    continue

                # 2. Кандидат ОБЯЗАТЕЛЬНО должен иметь пересечение по названию трека!
                c_title_toks = toks(track_title)
                if target_title_toks and not (target_title_toks & c_title_toks):
                    continue

                # Если в целевом названии несколько слов, требуем пересечения хотя бы 40%
                title_overlap = len(target_title_toks & c_title_toks) / max(1, len(target_title_toks))
                if title_overlap < 0.4:
                    continue

                # 3. Умная оценка совпадения артиста (без жесткой фильтрации '=='):
                c_artist_toks = toks(track_artist)
                sub_overlaps = [
                    len(toks(p) & c_artist_toks) / max(1, len(toks(p)))
                    for p in collab_parts if toks(p)
                ] if collab_parts else []
                best_sub_overlap = max(sub_overlaps) if sub_overlaps else 0.0

                artist_overlap = len(target_artist_toks & c_artist_toks) / max(1, len(target_artist_toks)) if target_artist_toks else 1.0

                if best_sub_overlap >= 0.5 or artist_overlap > 0:
                    artist_penalty = 0.0
                else:
                    # Чужой исполнитель: проверяем, не имитация ли
                    is_imitation = False
                    for p in collab_parts:
                        if p and re.search(r"(?i)\b(?:by|with|featuring)\s+" + re.escape(p), track_title):
                            is_imitation = True
                            break
                    if is_imitation:
                        continue
                    artist_penalty = 0.35

                # Расчет оценки соответствия:
                title_score = 0.60 * title_overlap
                artist_score = 0.40 if (best_sub_overlap >= 0.5 or artist_overlap > 0) else max(0.0, 0.40 - artist_penalty)
                score = title_score + artist_score

                c_vm = _version_markers(track_title)
                extra_vm = c_vm - target_vm
                CRITICAL_MARKERS = {"remix", "cover", "karaoke", "instrumental", "tribute", "parody", "live", "vocal"}
                if extra_vm & CRITICAL_MARKERS:
                    score -= 0.60 * len(extra_vm & CRITICAL_MARKERS)
                
                # Мягкие маркеры (исключая 'album', так как это нормальный студийный релиз)
                mild_markers = (extra_vm - {"album"}) - CRITICAL_MARKERS
                if mild_markers:
                    score -= 0.10 * len(mild_markers)

                # Предпочтение explicit и оригинальным студийным альбомам перед edited
                if track.get("explicit_lyrics") is True:
                    score += 0.10
                if re.search(r"(?i)\b(?:explicit|album version explicit)\b", track_title):
                    score += 0.12
                elif re.search(r"(?i)\b(?:album version edited|clean version|radio edit)\b", track_title):
                    score -= 0.10

                # Лишние слова в названии кандидата
                ignorable_words = toks("album version explicit edited clean radio single deluxe remastered")
                cand_extra_words = len(c_title_toks - target_title_toks - ignorable_words)
                score -= 0.02 * min(cand_extra_words, 5)

                # 4. Проверка длительности, если известна
                cand_dur = track.get("duration")
                if duration and cand_dur:
                    try:
                        dur_delta = abs(float(duration) - float(cand_dur))
                        if dur_delta <= 5:
                            score += 0.10
                        elif dur_delta <= 12:
                            score += 0.05
                        elif dur_delta > 30 and dur_delta / max(1.0, float(duration)) > 0.15:
                            score -= 0.40

                        # Колоссальное расхождение длительности (> 45s) отбрасываем
                        if dur_delta > 45 and dur_delta / max(1.0, float(duration)) > 0.20:
                            continue
                    except Exception:
                        pass

                if score > best_score:
                    best_score = score
                    best_id = str(track.get("id"))

            if best_id and best_score >= 0.70:
                return best_id
        except Exception as e:
            print(f"[!] Deezer (Search): {e}")

    if best_id and best_score >= 0.40:
        return best_id
    return None
