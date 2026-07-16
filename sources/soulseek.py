import httpx
import time
import shutil
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from config import SLSKD_URL, SLSKD_USER, SLSKD_PASS, SLSKD_DOWNLOADS_PATH

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

def parse_slskd_quality(filename: str, file_info: dict) -> tuple[str, int]:
    """Определяет формат аудио по расширению и метаданным."""
    fn_lower = filename.lower()
    bit_depth = file_info.get("bitDepth", 0)
    sample_rate = file_info.get("sampleRate", 0)
    bit_rate = file_info.get("bitRate", 0)
    
    if fn_lower.endswith(".flac"):
        if bit_depth >= 24:
            return (f"FLAC {bit_depth}bit/{sample_rate//1000}kHz", 150)
        return ("FLAC", 100)
    elif fn_lower.endswith(".wav"):
        return ("WAV", 95)
    elif fn_lower.endswith(".mp3"):
        if bit_rate >= 320:
            return ("MP3 320", 80)
        return (f"MP3 {bit_rate}", 50)
    elif fn_lower.endswith(".m4a") or fn_lower.endswith(".aac"):
        if bit_rate >= 256:
            return ("AAC 256", 75)
        return ("AAC", 60)
    return ("Unknown", 30)

def search_soulseek(artist: str, title: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Ищет трек на Soulseek через slskd API.
    Возвращает список подходящих файлов, отсортированных по качеству.
    """
    token = get_slskd_token()
    if not token:
        return []
        
    query = f"{artist} - {title}"
    print(f"[*] Soulseek: Поиск '{query}'...")
    
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    
    try:
        # 1. Запуск поиска
        resp = httpx.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query}, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[!] Soulseek: Не удалось запустить поиск (код {resp.status_code})")
            return []
            
        search_id = resp.json()["id"]
        
        # 2. Ожидание завершения поиска (макс. 15 секунд)
        start_time = time.time()
        is_complete = False
        while time.time() - start_time < 15:
            time.sleep(1.5)
            status_resp = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                if status_data.get("isComplete"):
                    is_complete = True
                    break
                    
        # 3. Запрос результатов
        resp_results = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses", headers=headers, timeout=10)
        if resp_results.status_code == 200:
            responses = resp_results.json()
            for response in responses:
                username = response.get("username")
                has_free_slot = response.get("hasFreeUploadSlot", False)
                files = response.get("files") or response.get("fileInfos") or []
                
                for f_info in files:
                    if f_info.get("isLocked", False):
                        continue
                        
                    filename = f_info.get("filename", "")
                    fn_lower = filename.lower()
                    
                    # Проверяем, что в имени файла есть и артист, и название
                    art_low = artist.lower()
                    title_low = title.lower()
                    if art_low not in fn_lower or title_low not in fn_lower:
                        continue
                        
                    quality_label, quality_score = parse_slskd_quality(filename, f_info)
                    
                    results.append({
                        "title": title,
                        "artist": artist,
                        "quality": quality_label,
                        "quality_score": quality_score,
                        "slskd_username": username,
                        "slskd_filename": filename,
                        "slskd_size": f_info.get("size", 0),
                        "has_free_slot": has_free_slot
                    })
                    
        # Очищаем поиск в slskd
        try:
            httpx.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
        except Exception:
            pass
            
    except Exception as e:
        print(f"[!] Soulseek: Ошибка при поиске: {e}")
        
    # Сортируем: сначала с бесплатным слотом, затем по качеству
    results.sort(key=lambda x: (x["has_free_slot"], x["quality_score"]), reverse=True)
    return results[:limit]

def download_soulseek_track(username: str, filename: str, size: int, dest_dir: Path) -> Optional[Path]:
    """
    Скачивает файл из Soulseek через slskd и копирует в целевую папку.
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
            time.sleep(5)
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
        # slskd скачивает файлы по умолчанию в подпапку с именем пользователя
        file_name = Path(filename.replace("\\", "/")).name
        
        # Возможные пути к скачанному файлу
        search_dirs = [Path(SLSKD_DOWNLOADS_PATH)] if SLSKD_DOWNLOADS_PATH else []
        search_dirs.extend([Path("./downloads"), Path("/downloads")])
        
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
                
            # Ищем сначала в папке пользователя: downloads/username/filename
            potential_file = search_dir / username / file_name
            if potential_file.exists():
                dest_path = dest_dir / file_name
                shutil.copy2(potential_file, dest_path)
                print(f"[+] Soulseek: Файл найден и скопирован: {dest_path}")
                return dest_path
                
            # Ищем рекурсивно
            for found_file in search_dir.rglob(file_name):
                if found_file.is_file():
                    dest_path = dest_dir / file_name
                    shutil.copy2(found_file, dest_path)
                    print(f"[+] Soulseek: Файл найден рекурсивно и скопирован: {dest_path}")
                    return dest_path
                    
        print("[!] Soulseek: Загрузка завершена, но файл не найден в папке slskd")
        
    except Exception as e:
        print(f"[!] Soulseek: Ошибка скачивания трека: {e}")
        
    return None
