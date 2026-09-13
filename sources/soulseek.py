import httpx
import time
import shutil
import re
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any
from config import SLSKD_URL, SLSKD_USER, SLSKD_PASS, SLSKD_DOWNLOADS_PATH
from sources.youtube_matcher import toks, _version_markers, _strip_noise

# Кеширование токена
_token_cache = {"token": None, "expires": 0.0}

def get_slskd_token() -> Optional[str]:
    """Получает JWT-токен для авторизации в slskd."""
    global _token_cache
    if not SLSKD_URL or not SLSKD_USER or not SLSKD_PASS:
        return None
        
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
        
    try:
        url = f"{SLSKD_URL}/api/v0/session"
        resp = httpx.post(url, json={"username": SLSKD_USER, "password": SLSKD_PASS}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _token_cache["token"] = data["token"]
            _token_cache["expires"] = data["expires"]
            return data["token"]
    except Exception as e:
        print(f"[!] Soulseek: Ошибка авторизации slskd: {e}")
    return None

def parse_slskd_quality(
    filename: str,
    file_info: dict,
    target_quality: str = "MP3",
    expected_duration: Optional[float] = None
) -> tuple[str, float]:
    """
    Определяет точный формат аудио по битрейту, глубине квантования и частоте дискретизации.
    Ставит реальный битрейт во главу угла, не пропуская файлы низкого качества.
    """
    fn_lower = filename.lower()
    bit_depth = file_info.get("bitDepth") or 0
    sample_rate = file_info.get("sampleRate") or 0
    raw_bitrate = file_info.get("bitRate") or 0
    is_vbr = file_info.get("isVariableBitRate", False)
    length = file_info.get("length") or 0
    size = file_info.get("size") or 0

    # Если битрейт не указан в тегах slskd, но есть размер и длительность - вычисляем расчетный битрейт
    calc_bitrate = int((size * 8) / (length * 1000)) if (length > 0 and size > 0) else 0
    effective_bitrate = raw_bitrate if raw_bitrate > 0 else calc_bitrate

    is_flac = fn_lower.endswith(".flac")
    is_wav = fn_lower.endswith(".wav")
    is_alac = fn_lower.endswith(".m4a") and (bit_depth > 0 or effective_bitrate > 500)
    is_aac = (fn_lower.endswith(".m4a") or fn_lower.endswith(".aac")) and not is_alac
    is_mp3 = fn_lower.endswith(".mp3")
    is_lossless = is_flac or is_wav or is_alac or fn_lower.endswith(".ape")

    # Валидация длительности (если известна из метаданных)
    dur_penalty = 0.0
    if expected_duration and length > 0:
        diff = abs(length - expected_duration)
        if diff > 45:
            # Слишком сильное расхождение (другая песня / микс / альбом целиком)
            return ("Invalid Duration", -999.0)
        elif diff > 15:
            # Заметное расхождение (интро / аутро / радио-версия)
            dur_penalty = min(diff * 5.0, 80.0)

    if target_quality == "FLAC":
        if not is_lossless:
            # В режиме FLAC lossy форматы (MP3, AAC) строго отклоняются
            return ("Lossy", 0.0)

        # 1. Hi-Res FLAC (24-bit / 96kHz, 88.2kHz, 192kHz)
        if bit_depth >= 24 or sample_rate >= 88200:
            rate_khz = sample_rate // 1000 if sample_rate else 96
            depth = bit_depth if bit_depth else 24
            label = f"FLAC Hi-Res {depth}bit/{rate_khz}kHz (~{effective_bitrate or 2500}kbps)"
            score = 2000.0 + depth * 10.0 + rate_khz - dur_penalty
            return (label, score)

        # 2. Standard Red Book CD FLAC (16-bit / 44.1kHz или 48kHz)
        if is_flac:
            rate_khz = sample_rate // 1000 if sample_rate else 44
            label = f"FLAC 16bit/{rate_khz}kHz (~{effective_bitrate or 900}kbps)"
            score = 1200.0 + rate_khz - dur_penalty
            return (label, score)

        # 3. WAV / ALAC Lossless
        label = f"Lossless ({effective_bitrate or 1000}kbps)"
        score = 1100.0 - dur_penalty
        return (label, score)

    else:
        # Режим MP3 / стандартный
        # 1. Если на Soulseek есть FLAC - это идеальный исходник (сконвертируем в чистый 320 CBR)
        if is_lossless:
            label = f"FLAC Lossless ({effective_bitrate or 900}kbps -> MP3 320)"
            score = 500.0 - dur_penalty
            return (label, score)

        # 2. Честный MP3 320 kbps CBR
        if is_mp3 and effective_bitrate >= 315 and not is_vbr:
            label = "MP3 320 kbps CBR"
            score = 450.0 - dur_penalty
            return (label, score)

        # 3. MP3 320 kbps VBR (или общий 320)
        if is_mp3 and effective_bitrate >= 315:
            label = "MP3 320 kbps VBR"
            score = 430.0 - dur_penalty
            return (label, score)

        # 4. MP3 256 kbps или V0 (VBR ~240-280 kbps)
        if is_mp3 and effective_bitrate >= 240:
            label = f"MP3 {effective_bitrate} kbps"
            score = 300.0 + (effective_bitrate - 240) - dur_penalty
            return (label, score)

        # 5. AAC 256+ kbps
        if is_aac and effective_bitrate >= 250:
            label = f"AAC {effective_bitrate} kbps"
            score = 280.0 - dur_penalty
            return (label, score)

        # 6. Низкий битрейт (< 240 kbps, например 192, 128, 96)
        # Отклоняем (score = 0), чтобы сработал откат на YouTube Music (дает честный MP3 320 CBR)!
        return (f"Low Bitrate ({effective_bitrate}kbps)", 0.0)

def search_soulseek(
    artist: str,
    title: str,
    limit: int = 5,
    target_quality: str = "MP3",
    duration: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Ищет трек на Soulseek через slskd API с сортировкой по реальному битрейту и качеству.
    """
    token = get_slskd_token()
    if not token:
        return []
        
    clean_art = _strip_noise(artist).strip()
    clean_tit = _strip_noise(title).strip()
    
    # Для Soulseek формируем чистый поисковый запрос без лишней пунктуации
    query = f"{clean_art} {clean_tit}".strip()
    query = re.sub(r"[\-\–\—\:\,\.\(\)\[\]\/\\]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    
    print(f"[*] Soulseek: Поиск '{query}' (целевое качество: {target_quality})...")
    
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    
    art_tokens = toks(clean_art)
    tit_tokens = toks(clean_tit)
    
    try:
        # 1. Запуск поиска
        resp = httpx.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query}, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[!] Soulseek: Не удалось запустить поиск (код {resp.status_code})")
            return []
            
        search_id = resp.json()["id"]
        
        # 2. Ожидание завершения поиска (до 12 секунд)
        start_time = time.time()
        while time.time() - start_time < 12:
            time.sleep(1.5)
            status_resp = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                file_count = status_data.get("fileCount", 0)
                is_complete = status_data.get("isComplete", False)
                
                # Если уже набралось достаточно файлов (>= 30) и прошло минимум 5 сек
                if is_complete or (file_count >= 30 and time.time() - start_time >= 5):
                    break
                    
        # 3. Запрос результатов
        resp_results = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses", headers=headers, timeout=10)
        if resp_results.status_code == 200:
            responses = resp_results.json()
            for response in responses:
                username = response.get("username")
                has_free_slot = response.get("hasFreeUploadSlot", False)
                queue_length = response.get("queueLength", 0)
                upload_speed = response.get("uploadSpeed", 0)
                files = response.get("files") or response.get("fileInfos") or []
                
                for f_info in files:
                    if f_info.get("isLocked", False):
                        continue
                        
                    filename = f_info.get("filename", "")
                    fn_tokens = toks(filename)
                    
                    # Проверяем совпадение токенов артиста и названия
                    art_overlap = len(art_tokens & fn_tokens) / max(1, len(art_tokens))
                    tit_overlap = len(tit_tokens & fn_tokens) / max(1, len(tit_tokens))
                    
                    if art_overlap < 0.6 or tit_overlap < 0.6:
                        continue
                        
                    quality_label, quality_score = parse_slskd_quality(
                        filename=filename,
                        file_info=f_info,
                        target_quality=target_quality,
                        expected_duration=duration
                    )
                    
                    if quality_score <= 0:
                        continue
                        
                    # Проверяем маркеры версий (ремиксы, каверы, лайвы и т.д.)
                    target_query = f"{artist} {title}"
                    target_vm = _version_markers(target_query)
                    cand_vm = _version_markers(filename)
                    extra_vm = cand_vm - target_vm
                    if extra_vm & {"remix", "cover", "karaoke", "instrumental", "tribute", "parody", "live"}:
                        quality_score -= 80.0
                    elif extra_vm:
                        quality_score -= 30.0
                        
                    if quality_score <= 0:
                        continue
                        
                    # Расчет общего ранга:
                    # 1. Битрейт / качество ДОМИНИРУЕТ (* 10000.0)
                    # 2. Бонус за свободный слот отдачи (+500.0)
                    # 3. Штраф за длину очереди пира (-min(queue_length * 15, 300))
                    # 4. Бонус за высокую скорость отдачи (+min(upload_speed // 50000, 200))
                    total_rank = (
                        quality_score * 10000.0
                        + (500.0 if has_free_slot else 0.0)
                        - min(queue_length * 15.0, 300.0)
                        + min((upload_speed or 0) / 50000.0, 200.0)
                    )
                    
                    results.append({
                        "title": title,
                        "artist": artist,
                        "quality": quality_label,
                        "quality_score": quality_score,
                        "total_rank": total_rank,
                        "bitrate": f_info.get("bitRate", 0),
                        "slskd_username": username,
                        "slskd_filename": filename,
                        "slskd_size": f_info.get("size", 0),
                        "has_free_slot": has_free_slot,
                        "queue_length": queue_length,
                        "upload_speed": upload_speed
                    })
                    
        # Очищаем поиск в slskd
        try:
            httpx.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
        except Exception:
            pass
            
    except Exception as e:
        print(f"[!] Soulseek: Ошибка при поиске: {e}")
        
    # Сортируем строго по рангу качества (битрейт в приоритете)
    results.sort(key=lambda x: x["total_rank"], reverse=True)
    return results[:limit]

def download_soulseek_track(
    username: str,
    filename: str,
    size: int,
    dest_dir: Path,
    target_quality: str = "MP3"
) -> Optional[Path]:
    """
    Скачивает файл из Soulseek через slskd и копирует в целевую папку.
    При необходимости транскодирует FLAC в честный MP3 320 kbps CBR.
    """
    token = get_slskd_token()
    if not token:
        print("[!] Soulseek: Нет токена авторизации")
        return None
        
    headers = {"Authorization": f"Bearer {token}"}
    queue_item = {"filename": filename, "size": size}
    
    print(f"[*] Soulseek: Постановка в очередь '{Path(filename).name}' от {username}")
    
    try:
        # 1. Постановка на закачку
        resp = httpx.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{username}",
            json=[queue_item],
            headers=headers,
            timeout=10
        )
        if resp.status_code not in (200, 201):
            print(f"[!] Soulseek: Не удалось поставить в очередь (код {resp.status_code})")
            return None
            
        # 2. Ожидание завершения (макс. 120 секунд)
        start_time = time.time()
        download_complete = False
        last_state = ""
        
        while time.time() - start_time < 120:
            time.sleep(3.5)
            status_resp = httpx.get(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}", headers=headers, timeout=10)
            if status_resp.status_code != 200:
                continue
                
            downloads_data = status_resp.json()
            files_to_check = []
            if isinstance(downloads_data, dict):
                for directory in downloads_data.get("directories", []):
                    files_to_check.extend(directory.get("files", []))
            elif isinstance(downloads_data, list):
                files_to_check = downloads_data
                
            # Ищем наш файл в очереди
            target_fn = filename.replace("\\", "/")
            found = False
            for dl in files_to_check:
                dl_fn = dl.get("filename", "").replace("\\", "/")
                if dl_fn == target_fn:
                    found = True
                    state = dl.get("stateDescription", dl.get("state", ""))
                    progress = dl.get("percentComplete", 0)
                    
                    if state != last_state:
                        print(f"[*] Soulseek: Статус: {state} ({progress}%)")
                        last_state = state
                        
                    state_lower = state.lower()
                    if "completed" in state_lower or "succeeded" in state_lower:
                        if "error" not in state_lower:
                            download_complete = True
                        break
                    elif any(s in state_lower for s in ("failed", "cancelled", "rejected", "errored")):
                        print(f"[!] Soulseek: Загрузка прервана со статусом: {state}")
                        return None
            
            if download_complete:
                break
            if not found and last_state:
                print("[!] Soulseek: Файл пропал из очереди")
                return None
                
        if not download_complete:
            print("[!] Soulseek: Превышено время ожидания загрузки")
            return None
            
        # 3. Поиск и копирование скачанного файла
        file_name = Path(filename.replace("\\", "/")).name
        
        # Возможные пути к скачанному файлу
        search_dirs = [Path(SLSKD_DOWNLOADS_PATH)] if SLSKD_DOWNLOADS_PATH else []
        search_dirs.extend([
            dest_dir,
            Path("./downloads"),
            Path("/downloads"),
            Path.home() / "Downloads" / "slskd",
            Path.home() / ".local" / "share" / "slskd" / "downloads"
        ])
        
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
                
            # Ищем сначала в папке пользователя: downloads/username/filename
            potential_file = search_dir / username / file_name
            if potential_file.exists():
                dest_path = dest_dir / file_name
                if potential_file.resolve() != dest_path.resolve():
                    shutil.copy2(potential_file, dest_path)
                
                # Если в режиме MP3 скачался FLAC-исходник - транскодируем в честный 320 kbps CBR
                if target_quality == "MP3" and dest_path.suffix.lower() == ".flac":
                    print(f"[*] Soulseek: Транскодирование Lossless исходника в честный MP3 320 kbps (LAME CBR)...")
                    mp3_path = dest_path.with_suffix(".mp3")
                    cmd = [
                        "ffmpeg", "-y", "-i", str(dest_path),
                        "-c:a", "libmp3lame", "-b:a", "320k",
                        str(mp3_path)
                    ]
                    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 10000:
                        if dest_path.resolve() != mp3_path.resolve():
                            try:
                                dest_path.unlink()
                            except Exception:
                                pass
                        dest_path = mp3_path
                
                print(f"[+] Soulseek: Файл найден и готов: {dest_path}")
                return dest_path
                
            # Ищем рекурсивно
            for found_file in search_dir.rglob(file_name):
                if found_file.is_file():
                    dest_path = dest_dir / file_name
                    if found_file.resolve() != dest_path.resolve():
                        shutil.copy2(found_file, dest_path)
                        
                    if target_quality == "MP3" and dest_path.suffix.lower() == ".flac":
                        print(f"[*] Soulseek: Транскодирование Lossless исходника в честный MP3 320 kbps (LAME CBR)...")
                        mp3_path = dest_path.with_suffix(".mp3")
                        cmd = [
                            "ffmpeg", "-y", "-i", str(dest_path),
                            "-c:a", "libmp3lame", "-b:a", "320k",
                            str(mp3_path)
                        ]
                        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if res.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 10000:
                            if dest_path.resolve() != mp3_path.resolve():
                                try:
                                    dest_path.unlink()
                                except Exception:
                                    pass
                            dest_path = mp3_path
                            
                    print(f"[+] Soulseek: Файл найден рекурсивно: {dest_path}")
                    return dest_path
                    
        print("[!] Soulseek: Загрузка завершена, но файл не найден в путях поиска slskd")
        
    except Exception as e:
        print(f"[!] Soulseek: Ошибка скачивания трека: {e}")
        
    return None
