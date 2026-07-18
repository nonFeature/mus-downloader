import httpx
import re
import json
import time
from pathlib import Path
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    MONOCHROME_QOBUZ_PROXY_URL,
    MONOCHROME_HIFI_URL,
    MONOCHROME_AMAZON_API_URL,
    MONOCHROME_AMAZON_JWT,
    MONOCHROME_AMAZON_BYPASS_TOKEN
)
from sources.amazon_decrypt import Mp4CencDecryptor

def _get_turnstile_jwt_from_chrome() -> Optional[str]:
    """
    Автоматически подключается к открытой сессии отладки Chrome на порту 9222,
    открывает временную вкладку monochrome.samidy.com для получения Turnstile JWT из localStorage.
    """
    try:
        # 1. Получаем devtools URL браузера
        r = httpx.get("http://127.0.0.1:9222/json/version", timeout=3)
        browser_ws_url = r.json().get("webSocketDebuggerUrl")
        if not browser_ws_url:
            return None
        
        import websocket
        # Открываем подключение к менеджеру вкладок браузера
        ws = websocket.create_connection(browser_ws_url, suppress_origin=True)
        
        # Создаем новую вкладку
        create_msg = {
            "id": 1,
            "method": "Target.createTarget",
            "params": {
                "url": "https://monochrome.samidy.com/"
            }
        }
        ws.send(json.dumps(create_msg))
        res = json.loads(ws.recv())
        target_id = res.get("result", {}).get("targetId")
        ws.close()
        
        if not target_id:
            return None
            
        # Ждем загрузки вкладки
        time.sleep(2.5)
        
        # Находим новую вкладку в списке для подключения к ней напрямую
        r = httpx.get("http://127.0.0.1:9222/json", timeout=3)
        tabs = r.json()
        target_ws_url = None
        for tab in tabs:
            if tab.get("id") == target_id:
                target_ws_url = tab.get("webSocketDebuggerUrl")
                break
                
        if not target_ws_url:
            # Закрываем вкладку в случае неудачи
            ws = websocket.create_connection(browser_ws_url, suppress_origin=True)
            ws.send(json.dumps({"id": 2, "method": "Target.closeTarget", "params": {"targetId": target_id}}))
            ws.close()
            return None
            
        # Подключаемся к вкладке и считываем localStorage
        tab_ws = websocket.create_connection(target_ws_url, suppress_origin=True)
        eval_msg = {
            "id": 3,
            "method": "Runtime.evaluate",
            "params": {
                "expression": 'localStorage.getItem("amazon_turnstile_jwt")',
                "returnByValue": True
            }
        }
        tab_ws.send(json.dumps(eval_msg))
        eval_res = json.loads(tab_ws.recv())
        tab_ws.close()
        
        # Закрываем вкладку
        ws = websocket.create_connection(browser_ws_url, suppress_origin=True)
        ws.send(json.dumps({"id": 4, "method": "Target.closeTarget", "params": {"targetId": target_id}}))
        ws.close()
        
        jwt = eval_res.get("result", {}).get("result", {}).get("value")
        if jwt:
            print("[+] Monochrome: Автоматически получен JWT-токен из Chrome")
            return jwt
    except Exception:
        pass
    return None

def _amazon_stream_url(artist: str, title: str, album: str, duration: Optional[float], target_quality: str) -> Optional[Tuple[str, str]]:
    """
    Запрашивает API прокси Amazon Music (amz.geeked.wtf) для получения ссылки на CENC поток и ключа.
    """
    # Сначала пытаемся вытянуть токен из запущенного Хрома, если нет - берем из конфига
    jwt = _get_turnstile_jwt_from_chrome() or MONOCHROME_AMAZON_JWT
    bypass_token = MONOCHROME_AMAZON_BYPASS_TOKEN
    
    url = f"{MONOCHROME_AMAZON_API_URL.rstrip('/')}/api/track/"
    
    # Форматируем длительность в секундах
    dur_str = ""
    if duration:
        dur_str = str(round(duration))
        
    q_val = "LOSSLESS" if target_quality == "FLAC" else "HIGH"
    params = {
        "track": title,
        "artist": artist,
        "album": album or "",
        "duration": dur_str,
        "quality": q_val
    }
    if bypass_token:
        params["bypass_token"] = bypass_token
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if jwt:
        headers["X-Turnstile-JWT"] = jwt
        
    try:
        print(f"[*] Monochrome: Попытка получить Amazon Music поток для '{artist} - {title}' на {MONOCHROME_AMAZON_API_URL}...")
        resp = httpx.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 428:
            print("[!] Monochrome: Требуется Turnstile капча для Amazon. Откройте https://amz.geeked.wtf/ в Chrome или настройте MONOCHROME_AMAZON_JWT.")
            return None
        if resp.status_code == 200:
            body = resp.json()
            data = body.get("data") if isinstance(body, dict) else None
            if not data and isinstance(body, dict):
                data = body
                
            stream_url = data.get("stream_url") or data.get("streamUrl")
            decryption_key = (
                data.get("decryption_key") or 
                data.get("decryptionKey") or 
                data.get("decryption", {}).get("key") or
                data.get("drm", {}).get("decryption_key") or
                data.get("drm", {}).get("decryptionKey")
            )
            if stream_url and decryption_key:
                return stream_url, decryption_key
    except Exception as e:
        print(f"[!] Monochrome: Ошибка запроса к Amazon API: {e}")
    return None


# Список публичных прокси Qobuz для отказоустойчивости
QOBUZ_PROXIES = [
    MONOCHROME_QOBUZ_PROXY_URL,
    "https://qobuz.kennyy.com.br",
    "https://mono.scavengerfurs.net",
    "https://qdl-api.monochrome.tf"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
}

def resolve_qobuz_track(isrc: str, query: str = None) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Ищет трек по ISRC или по текстовому запросу на прокси-серверах Qobuz в мультипотоке.
    """
    # 1. Попытка поиска по ISRC
    if isrc:
        def check_isrc(base_url):
            try:
                url = f"{base_url.rstrip('/')}/api/get-music"
                resp = httpx.get(url, params={"q": isrc, "offset": 0}, headers=HEADERS, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", {}).get("tracks", {}).get("items", [])
                    exact_matches = [t for t in items if (t.get("isrc") or "").upper() == isrc.upper()]
                    if exact_matches:
                        track = exact_matches[0]
                        hires_matches = [t for t in exact_matches if t.get("hires")]
                        if hires_matches:
                            track = hires_matches[0]
                        return str(track.get("id")), track.get("title"), track.get("performer", {}).get("name")
            except Exception:
                pass
            return None

        print(f"[*] Monochrome: Параллельный запрос ISRC {isrc} на {len(QOBUZ_PROXIES)} прокси Qobuz...")
        with ThreadPoolExecutor(max_workers=len(QOBUZ_PROXIES)) as executor:
            futures = [executor.submit(check_isrc, p) for p in QOBUZ_PROXIES if p]
            for future in as_completed(futures):
                res = future.result()
                if res:
                    return res

    # 2. Попытка поиска по тексту (если по ISRC не нашли или его нет)
    if query:
        def check_query(base_url):
            try:
                url = f"{base_url.rstrip('/')}/api/get-music"
                resp = httpx.get(url, params={"q": query, "offset": 0}, headers=HEADERS, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", {}).get("tracks", {}).get("items", [])
                    if items:
                        track = items[0]
                        hires_items = [t for t in items if t.get("hires")]
                        if hires_items:
                            track = hires_items[0]
                        return str(track.get("id")), track.get("title"), track.get("performer", {}).get("name")
            except Exception:
                pass
            return None

        print(f"[*] Monochrome: Параллельный поиск по тексту '{query}' на Qobuz...")
        with ThreadPoolExecutor(max_workers=len(QOBUZ_PROXIES)) as executor:
            futures = [executor.submit(check_query, p) for p in QOBUZ_PROXIES if p]
            for future in as_completed(futures):
                res = future.result()
                if res:
                    return res
                
    return None, None, None

def get_qobuz_download_url(track_id: str, quality_fmt: int) -> Optional[str]:
    """
    Получает прямую CDN ссылку для скачивания трека с Qobuz в мультипотоке.
    """
    def check_download(base_url):
        try:
            url = f"{base_url.rstrip('/')}/api/download-music"
            resp = httpx.get(
                url,
                params={"track_id": track_id, "quality": quality_fmt},
                headers=HEADERS,
                timeout=12
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("success"):
                    stream_url = body.get("data", {}).get("url")
                    if stream_url:
                        return stream_url
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=len(QOBUZ_PROXIES)) as executor:
        futures = [executor.submit(check_download, p) for p in QOBUZ_PROXIES if p]
        for future in as_completed(futures):
            res = future.result()
            if res:
                return res
    return None

def _tidal_stream_url(tidal_id: str, quality: str) -> Optional[str]:
    """
    Получает прямой поток Tidal из Monochrome HiFi API в мультипотоке.
    """
    hifi_api_endpoints = [
        MONOCHROME_HIFI_URL,
        "https://monochrome-api.samidy.com",
        "https://api.monochrome.tf",
        "https://us-west.monochrome.tf",
        "https://eu-central.monochrome.tf"
    ]
    
    q_val = "LOSSLESS" if quality == "FLAC" else "HIGH"
    
    def check_tidal(base):
        try:
            url = f"{base.rstrip('/')}/track/"
            resp = httpx.get(url, params={"id": int(tidal_id), "quality": q_val}, headers=HEADERS, timeout=12)
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    for key in ("OriginalTrackUrl", "originalTrackUrl", "url"):
                        stream_url = data.get(key)
                        if stream_url and stream_url.startswith("http"):
                            return stream_url
                    urls = data.get("urls")
                    if urls and isinstance(urls, list) and urls[0].startswith("http"):
                        return urls[0]
        except Exception:
            pass
        return None

    print(f"[*] Monochrome: Параллельный запрос потока Tidal {tidal_id} ({q_val}) на {len(hifi_api_endpoints)} инстансах...")
    with ThreadPoolExecutor(max_workers=len(hifi_api_endpoints)) as executor:
        futures = [executor.submit(check_tidal, b) for b in hifi_api_endpoints if b]
        for future in as_completed(futures):
            res = future.result()
            if res:
                return res
    return None

def download_monochrome_track(
    isrc: str,
    artist_hint: str,
    title_hint: str,
    dest_dir: Path,
    target_quality: str = "FLAC",
    tidal_id: str = None,
    album_hint: str = None,
    duration: float = None
) -> Optional[Path]:
    """
    Скачивает трек во FLAC (или MP3/M4A при выборе) через Qobuz прокси, Tidal hifi-api или Amazon Music.
    """
    if not isrc and not (artist_hint and title_hint):
        print("[!] Monochrome: Идентификаторы отсутствуют, скачивание невозможно")
        return None

    # 1. Сначала пробуем Qobuz прокси (по ISRC или тексту)
    track_id, title, performer = resolve_qobuz_track(isrc, f"{artist_hint} - {title_hint}")
    
    artist = performer or artist_hint or "Unknown Artist"
    track_title = title or title_hint or "Unknown Track"
    clean_name = re.sub(r'[\\/*?:"<>|]', "_", f"{artist} - {track_title}")
    
    stream_url = None
    ext = "flac"
    is_amazon = False
    decryption_key = None
    
    if track_id:
        if target_quality == "FLAC":
            print("[*] Monochrome: Попытка получить Hi-Res FLAC (24-bit) с Qobuz...")
            stream_url = get_qobuz_download_url(track_id, 27)
            if not stream_url:
                print("[*] Monochrome: Hi-Res недоступен, пробуем стандартный FLAC (16-bit) с Qobuz...")
                stream_url = get_qobuz_download_url(track_id, 7)
            if not stream_url:
                print("[!] Monochrome: FLAC недоступен на Qobuz, пробуем MP3 320kbps...")
                stream_url = get_qobuz_download_url(track_id, 6)
                ext = "mp3"
        else:
            stream_url = get_qobuz_download_url(track_id, 6)
            ext = "mp3"

    # 2. Если Qobuz прокси не сработал, пробуем Tidal hifi-api
    if not stream_url and tidal_id:
        print("[*] Monochrome: Qobuz прокси не дали результатов. Попытка через Tidal hifi-api...")
        stream_url = _tidal_stream_url(tidal_id, target_quality)
        if stream_url:
            print("[+] Monochrome: Получен прямой поток из Tidal hifi-api")
            ext = "flac" if target_quality == "FLAC" else "mp3"

    # 3. Если и Tidal не сработал, пробуем Amazon Music
    if not stream_url:
        print("[*] Monochrome: Qobuz и Tidal не дали результатов. Попытка через Amazon Music...")
        amazon_res = _amazon_stream_url(
            artist=artist_hint,
            title=title_hint,
            album=album_hint,
            duration=duration,
            target_quality=target_quality
        )
        if amazon_res:
            stream_url, decryption_key = amazon_res
            is_amazon = True
            ext = "flac" if target_quality == "FLAC" else "m4a"
            print("[+] Monochrome: Получен шифрованный поток из Amazon Music")

    if not stream_url:
        print("[!] Monochrome: Не удалось получить ссылку на поток ни из одного Monochrome источника")
        return None

    output_path = dest_dir / f"{clean_name}.{ext}"

    # Скачивание и дешифрование потока Amazon CENC
    if is_amazon and decryption_key:
        print(f"[*] Monochrome: Скачивание зашифрованного потока Amazon: {output_path}")
        try:
            encrypted_data = bytearray()
            with httpx.stream("GET", stream_url, headers=HEADERS, timeout=30) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(chunk_size=65536):
                    encrypted_data.extend(chunk)
            
            print("[*] Monochrome: Дешифрование потока Amazon (CENC)...")
            decryptor = Mp4CencDecryptor(decryption_key, target_codec='flac' if target_quality == "FLAC" else 'aac')
            decrypted_data = decryptor.decrypt(bytes(encrypted_data))
            
            with open(output_path, "wb") as f:
                f.write(decrypted_data)
            print(f"[+] Monochrome: Скачивание и дешифрование завершено: {output_path}")
            return output_path
        except Exception as e:
            print(f"[!] Monochrome: Ошибка скачивания/дешифрования Amazon: {e}")
            if output_path.exists():
                output_path.unlink()
            return None

    # Обычное скачивание для Qobuz/Tidal
    print(f"[*] Monochrome: Скачивание потока: {output_path}")
    try:
        with httpx.stream("GET", stream_url, headers=HEADERS, timeout=30) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
        print(f"[+] Monochrome: Скачивание завершено: {output_path}")
        return output_path
    except Exception as e:
        print(f"[!] Monochrome: Ошибка сохранения файла: {e}")
        if output_path.exists():
            output_path.unlink()
            
    return None
