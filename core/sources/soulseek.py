import asyncio
import logging
import threading
import httpx
import time
import shutil
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any
from config import (
    SLSK_USER, SLSK_PASS, SLSKD_URL, SLSKD_USER, SLSKD_PASS,
    SLSKD_DOWNLOADS_PATH, DOWNLOAD_DIR, is_soulseek_configured
)
from .youtube_matcher import toks, _version_markers, _strip_noise

try:
    from aioslsk.client import SoulSeekClient
    from aioslsk.settings import Settings, CredentialsSettings
    from aioslsk.events import SearchResultEvent
    from aioslsk.protocol.primitives import AttributeKey
    from aioslsk.transfer.state import TransferState
    AIOSLSK_AVAILABLE = True
    AIOSLSK_ERROR = None
except Exception as e:
    AIOSLSK_AVAILABLE = False
    AIOSLSK_ERROR = str(e)

class ActiveDownload:
    """Отслеживает состояние активной P2P-загрузки и подписчиков на неё."""
    def __init__(self, username: str, filename: str, staging_file: Path, loop: asyncio.AbstractEventLoop, key: str):
        self.key: str = key
        self.username: str = username
        self.filename: str = filename
        self.staging_file: Path = staging_file
        self.transfer: Optional[Any] = None
        self.future: asyncio.Future = loop.create_future()
        self.subscribers: int = 1
        self.task: Optional[asyncio.Task] = None
        self.abort_requested: bool = False
        self.created_at: float = time.time()

class EmbeddedSoulseek:
    """
    Встроенный Soulseek P2P клиент (на базе aioslsk).
    Работает в отдельном фоновом потоке с собственным asyncio event loop.
    Поддерживает одновременные загрузки в нескольких потоках без взаимных помех.
    """
    _instance: Optional["EmbeddedSoulseek"] = None
    _lock = threading.Lock()
    _staging_refs: Dict[Path, int] = {}
    _staging_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> Optional["EmbeddedSoulseek"]:
        if not AIOSLSK_AVAILABLE:
            return None
        if not SLSK_USER or not SLSK_PASS:
            return None
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(SLSK_USER, SLSK_PASS, DOWNLOAD_DIR)
        return cls._instance

    def __init__(self, username: str, password: str, download_dir: Path):
        self.username = username
        self.password = password
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.download_dir / ".staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        # Очистка остатков staging от прошлых сессий
        if self.staging_dir.exists():
            for p in self.staging_dir.iterdir():
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass

        self._staging_refs: Dict[Path, int] = {}
        self._staging_lock = threading.Lock()

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.client: Optional[Any] = None
        self._connected = threading.Event()
        self._init_error: Optional[str] = None
        self._active_downloads: Dict[str, ActiveDownload] = {}
        self._start_thread()

    def _increment_staging_ref(self, path: Path):
        with self._staging_lock:
            self._staging_refs[path] = self._staging_refs.get(path, 0) + 1

    def _decrement_staging_ref(self, path: Path):
        with self._staging_lock:
            if path in self._staging_refs:
                self._staging_refs[path] -= 1
                if self._staging_refs[path] <= 0:
                    del self._staging_refs[path]
                    try:
                        path.unlink(missing_ok=True)
                        parent = path.parent
                        if parent != self.staging_dir and parent.is_relative_to(self.staging_dir):
                            try:
                                parent.rmdir()
                            except Exception:
                                pass
                    except Exception:
                        pass

    def cleanup_staging_file(self, path: Path):
        """Потокобезопасное освобождение staging файла. Удаляет файл, когда все потоки закончили работу с ним."""
        with self._staging_lock:
            if path in self._staging_refs:
                self._staging_refs[path] -= 1
                if self._staging_refs[path] > 0:
                    return
                del self._staging_refs[path]
            try:
                if path.exists() and path.is_relative_to(self.staging_dir):
                    path.unlink(missing_ok=True)
                    parent = path.parent
                    if parent != self.staging_dir and parent.is_relative_to(self.staging_dir):
                        try:
                            parent.rmdir()
                        except Exception:
                            pass
            except Exception:
                pass

    release_staging_file = cleanup_staging_file

    def _start_thread(self):
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="EmbeddedSoulseek")
        self.thread.start()
        connected = self._connected.wait(timeout=10)
        if not connected:
            err = f": {self._init_error}" if self._init_error else " (таймаут)"
            print(f"[!] Soulseek: ошибка подключения{err}")
        else:
            print(f"[+] Soulseek: вход выполнен ({self.username})")

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._async_init())
            self.loop.run_forever()
        except Exception as e:
            self._init_error = str(e)

    async def _async_init(self):
        try:
            for logger_name in ("aioslsk", "asyncio"):
                l = logging.getLogger(logger_name)
                l.setLevel(logging.CRITICAL)
                l.propagate = False

            settings = Settings(
                credentials=CredentialsSettings(
                    username=self.username,
                    password=self.password
                )
            )
            settings.network.upnp.enabled = False
            settings.shares.scan_on_start = False
            settings.shares.download = str(self.download_dir.resolve())

            self.client = SoulSeekClient(settings)
            await self.client.start(connect=True)
            await self.client.login()
            self._connected.set()
        except Exception as e:
            self._init_error = str(e)

    def search(self, query: str, timeout: float = 6.0) -> List[Dict[str, Any]]:
        if not self._connected.is_set() or not self.loop or not self.client:
            return []
        try:
            future = asyncio.run_coroutine_threadsafe(self._async_search(query, timeout), self.loop)
            return future.result(timeout=timeout + 5)
        except Exception as e:
            print(f"[!] Soulseek: Ошибка P2P-поиска: {e}")
            return []

    async def _async_search(self, query: str, timeout: float) -> List[Dict[str, Any]]:
        found = []
        target_ticket = None

        async def on_result(event: SearchResultEvent):
            if target_ticket is None:
                return
            eq = getattr(event, "query", None)
            if eq is not None:
                t = getattr(eq, "ticket", None)
                if t is not None and t != target_ticket:
                    return
            res = event.result
            for item in res.shared_items:
                attr_map = item.get_attribute_map()
                found.append({
                    "username": res.username,
                    "filename": item.filename,
                    "size": item.filesize,
                    "bitrate": attr_map.get(AttributeKey.BITRATE, 0),
                    "duration": attr_map.get(AttributeKey.DURATION, 0),
                    "sample_rate": attr_map.get(AttributeKey.SAMPLE_RATE, 0),
                    "bit_depth": attr_map.get(AttributeKey.BIT_DEPTH, 0),
                    "is_vbr": bool(attr_map.get(AttributeKey.VBR, 0)),
                    "has_free_slot": res.has_free_slots,
                    "upload_speed": res.avg_speed,
                    "queue_length": res.queue_size,
                })

        self.client.events.register(SearchResultEvent, on_result)
        req = None
        try:
            req = await self.client.searches.search(query)
            if req is not None:
                target_ticket = getattr(req, "ticket", None)
                for res in getattr(req, "results", []):
                    for item in getattr(res, "shared_items", []):
                        attr_map = item.get_attribute_map()
                        found.append({
                            "username": res.username,
                            "filename": item.filename,
                            "size": item.filesize,
                            "bitrate": attr_map.get(AttributeKey.BITRATE, 0),
                            "duration": attr_map.get(AttributeKey.DURATION, 0),
                            "sample_rate": attr_map.get(AttributeKey.SAMPLE_RATE, 0),
                            "bit_depth": attr_map.get(AttributeKey.BIT_DEPTH, 0),
                            "is_vbr": bool(attr_map.get(AttributeKey.VBR, 0)),
                            "has_free_slot": res.has_free_slots,
                            "upload_speed": res.avg_speed,
                            "queue_length": res.queue_size,
                        })

            start_t = time.time()
            while time.time() - start_t < timeout:
                await asyncio.sleep(0.5)
                if len(found) >= 40 and (time.time() - start_t >= 3.5):
                    break
        finally:
            try:
                self.client.events.unregister(SearchResultEvent, on_result)
            except Exception:
                pass
            if req is not None and hasattr(self.client.searches, "remove_request"):
                try:
                    self.client.searches.remove_request(req)
                except Exception:
                    pass

        return found

    def download(self, username: str, filename: str, timeout: float = 600.0) -> Optional[Path]:
        if not self._connected.is_set() or not self.loop or not self.client:
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_download(username, filename, timeout),
                self.loop
            )
            return future.result(timeout=timeout + 15)
        except Exception as e:
            print(f"[!] Soulseek: Ошибка скачивания: {e}")
            return None

    async def _run_transfer_loop(self, active: ActiveDownload, timeout: float = 600.0):
        norm_key = active.key
        username = active.username
        filename = active.filename
        staging_file = active.staging_file
        file_basename = staging_file.name
        transfer = None
        result_path: Optional[Path] = None

        try:
            try:
                # В aioslsk передаем оригинальный remote_path без подмены слешей
                transfer = await self.client.transfers.download(username, filename)
                active.transfer = transfer
            except Exception as e:
                print(f"[!] Soulseek [{file_basename}]: Ошибка постановки в очередь: {e}")
                return

            # Изолируем путь скачивания для этой конкретной загрузки
            transfer.local_path = str(staging_file.resolve())

            start_t = time.time()
            queued_start = None
            init_start = None
            last_state = None
            last_print = 0
            last_bytes = 0
            last_progress_time = time.time()
            stall_timeout = 30.0  # 30 секунд без прогресса при активном скачивании
            max_lifetime = max(600.0, timeout)

            while time.time() - start_t < max_lifetime:
                if active.abort_requested or active.subscribers <= 0:
                    print(f"[!] Soulseek [{file_basename}]: Отмена загрузки (все подписчики отключились)")
                    try:
                        await transfer.abort()
                    except Exception:
                        pass
                    return

                await asyncio.sleep(1)
                st = transfer.state.VALUE

                total_bytes = transfer.filesize or 0
                cur_bytes = transfer.bytes_transfered or 0

                if time.time() - last_print >= 2.5 or st != last_state:
                    total_mb = total_bytes / (1024 * 1024) if total_bytes else 0.0
                    cur_mb = cur_bytes / (1024 * 1024)
                    pct = int((cur_bytes / total_bytes) * 100) if total_bytes else 0
                    print(f"[*] Soulseek [{file_basename}]: {st.name} ({cur_mb:.1f}/{total_mb:.1f} MB, {pct}%) [{username}]")
                    last_print = time.time()
                    last_state = st

                # Отслеживание прогресса и зависания при скачивании
                if st == TransferState.State.DOWNLOADING:
                    if cur_bytes > last_bytes:
                        last_bytes = cur_bytes
                        last_progress_time = time.time()
                    elif time.time() - last_progress_time > stall_timeout:
                        print(f"[!] Soulseek [{file_basename}]: Загрузка зависла (нет новых данных более {stall_timeout:.0f}с), отмена...")
                        try:
                            await transfer.abort()
                        except Exception:
                            pass
                        return
                else:
                    last_progress_time = time.time()

                # Проверка зависания на этапе инициализации
                if st == TransferState.State.INITIALIZING:
                    if init_start is None:
                        init_start = time.time()
                    elif time.time() - init_start > 30.0:
                        print(f"[!] Soulseek [{file_basename}]: Пир {username} не отвечает на подключение более 30с, отмена...")
                        try:
                            await transfer.abort()
                        except Exception:
                            pass
                        return
                else:
                    init_start = None

                # Проверка очереди у пира:
                if st == TransferState.State.QUEUED:
                    # Если пир сейчас параллельно отдает или инициализирует нам другой трек — не считаем очередь зависшей
                    peer_busy_with_us = any(
                        other.username.lower() == username.lower()
                        and other is not active
                        and other.transfer
                        and other.transfer.state.VALUE in (
                            TransferState.State.DOWNLOADING,
                            TransferState.State.INITIALIZING
                        )
                        for other in self._active_downloads.values()
                    )
                    if peer_busy_with_us:
                        queued_start = time.time()
                    else:
                        if queued_start is None:
                            queued_start = time.time()
                        elif time.time() - queued_start > 20.0:
                            print(f"[!] Soulseek [{file_basename}]: Очередь у пира {username} не продвигается более 20 сек, отмена...")
                            try:
                                await transfer.abort()
                            except Exception:
                                pass
                            return
                else:
                    queued_start = None

                if st == TransferState.State.COMPLETE:
                    if staging_file.exists() and staging_file.stat().st_size > 0:
                        result_path = staging_file
                        return
                    lp = getattr(transfer, "local_path", None)
                    if lp and Path(lp).exists() and Path(lp).stat().st_size > 0:
                        result_path = Path(lp)
                        return

                    await asyncio.sleep(0.5)
                    if staging_file.exists() and staging_file.stat().st_size > 0:
                        result_path = staging_file
                        return
                    if lp and Path(lp).exists() and Path(lp).stat().st_size > 0:
                        result_path = Path(lp)
                        return
                    return

                elif st in (TransferState.State.FAILED, TransferState.State.ABORTED):
                    print(f"[!] Soulseek [{file_basename}]: Загрузка от {username} завершилась с ошибкой: {st.name}")
                    return

            print(f"[!] Soulseek [{file_basename}]: Превышено максимальное время загрузки ({max_lifetime}s)")
            try:
                await transfer.abort()
            except Exception:
                pass

        except asyncio.CancelledError:
            if transfer:
                try:
                    await transfer.abort()
                except Exception:
                    pass
            raise

        finally:
            if not active.future.done():
                active.future.set_result(result_path)

            self._active_downloads.pop(norm_key, None)

            if transfer:
                try:
                    await self.client.transfers.remove(transfer)
                except Exception:
                    pass

            if result_path is None:
                self._decrement_staging_ref(staging_file)

    async def _async_download(self, username: str, filename: str, timeout: float) -> Optional[Path]:
        norm_fn = filename.replace("/", "\\").strip("\\").lower()
        norm_user = username.strip().lower()
        norm_key = f"{norm_user}:{norm_fn}"
        file_basename = Path(filename.replace("\\", "/")).name

        if norm_key in self._active_downloads and not self._active_downloads[norm_key].future.done():
            active = self._active_downloads[norm_key]
            active.subscribers += 1
            self._increment_staging_ref(active.staging_file)
            print(f"[*] Soulseek [{file_basename}]: Подключение к уже активной загрузке ({username})...")
        else:
            self._active_downloads.pop(norm_key, None)
            staging_subdir = self.staging_dir / uuid.uuid4().hex
            staging_subdir.mkdir(parents=True, exist_ok=True)
            staging_file = staging_subdir / file_basename

            active = ActiveDownload(username, filename, staging_file, self.loop, key=norm_key)
            self._active_downloads[norm_key] = active
            self._increment_staging_ref(staging_file)

            active.task = asyncio.create_task(self._run_transfer_loop(active, timeout=timeout))

        try:
            res = await asyncio.wait_for(asyncio.shield(active.future), timeout=timeout)
            if res and res.exists():
                return res
            self._decrement_staging_ref(active.staging_file)
            return None
        except asyncio.TimeoutError:
            print(f"[!] Soulseek [{file_basename}]: Превышено время ожидания скачивания ({timeout}s)")
            self._decrement_staging_ref(active.staging_file)
            return None
        except Exception as e:
            print(f"[!] Soulseek [{file_basename}]: Ошибка ожидания загрузки: {e}")
            self._decrement_staging_ref(active.staging_file)
            return None
        finally:
            active.subscribers -= 1
            if active.subscribers <= 0 and not active.future.done():
                active.abort_requested = True
                if active.task and not active.task.done():
                    active.task.cancel()
                if active.transfer:
                    try:
                        await active.transfer.abort()
                    except Exception:
                        pass

# Кеширование токена
_token_cache = {"token": None, "expires": 0.0}
_token_lock = threading.Lock()

def get_slskd_token() -> Optional[str]:
    """Получает JWT-токен для авторизации в slskd."""
    global _token_cache
    if not SLSKD_URL or not SLSKD_USER or not SLSKD_PASS:
        return None
        
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
            return _token_cache["token"]
            
        try:
            url = f"{SLSKD_URL}/api/v0/session"
            resp = httpx.post(url, json={"username": SLSKD_USER, "password": SLSKD_PASS}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                _token_cache["token"] = data["token"]
                exp = data.get("expires", 0.0)
                try:
                    exp_val = float(exp)
                except (ValueError, TypeError):
                    exp_val = time.time() + 3600.0
                _token_cache["expires"] = exp_val
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
    bit_depth = file_info.get("bitDepth") or file_info.get("bit_depth") or 0
    sample_rate = file_info.get("sampleRate") or file_info.get("sample_rate") or 0
    raw_bitrate = file_info.get("bitRate") or file_info.get("bitrate") or 0
    is_vbr = file_info.get("isVariableBitRate") or file_info.get("is_vbr", False)
    length = file_info.get("length") or file_info.get("duration") or 0
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
            score = 2000.0 + depth * 10.0 + rate_khz + min((effective_bitrate or 0) / 100.0, 50.0) - dur_penalty
            return (label, score)

        # 2. Standard Red Book CD FLAC (16-bit / 44.1kHz или 48kHz)
        if is_flac:
            rate_khz = sample_rate // 1000 if sample_rate else 44
            label = f"FLAC 16bit/{rate_khz}kHz (~{effective_bitrate or 900}kbps)"
            score = 1200.0 + rate_khz + min((effective_bitrate or 0) / 100.0, 20.0) - dur_penalty
            return (label, score)

        # 3. WAV / ALAC Lossless
        label = f"Lossless ({effective_bitrate or 1000}kbps)"
        score = 1100.0 - dur_penalty
        return (label, score)

    else:
        # Режим MP3 / стандартный
        # 1. Честный готовый MP3 320 kbps CBR (качается сразу, без пережатия и нагрузки на CPU)
        if is_mp3 and effective_bitrate >= 315 and not is_vbr:
            label = "MP3 320 kbps CBR"
            score = 500.0 - dur_penalty
            return (label, score)

        # 2. FLAC Lossless (если готового MP3 320 CBR нет - берем FLAC и конвертируем в честный 320 CBR)
        if is_lossless:
            label = f"FLAC Lossless ({effective_bitrate or 900}kbps -> MP3 320)"
            score = 480.0 - dur_penalty
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
    Ищет трек на Soulseek (через встроенный клиент aioslsk или slskd API) с сортировкой по реальному битрейту и качеству.
    """
    clean_art = _strip_noise(artist).strip()
    clean_tit = _strip_noise(title).strip()
    
    # Для Soulseek формируем чистый поисковый запрос без лишней пунктуации
    query = f"{clean_art} {clean_tit}".strip()
    query = re.sub(r"[\-\–\—\:\,\.\(\)\[\]\/\\]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    
    print(f"[*] Soulseek: поиск '{query}' [{target_quality}]...")
    
    art_tokens = toks(clean_art)
    tit_tokens = toks(clean_tit)
    raw_candidates = []

    # 1. Приоритет: встроенный клиент Soulseek
    es = EmbeddedSoulseek.get_instance()
    if not es and not SLSKD_URL:
        if not SLSK_USER or not SLSK_PASS:
            print("[!] Soulseek: не указаны SLSK_USER и SLSK_PASS в .env")
        elif not AIOSLSK_AVAILABLE:
            print(f"[!] Soulseek: aioslsk недоступен ({AIOSLSK_ERROR or 'ошибка импорта'})")
        return []

    if es:
        raw_items = es.search(query, timeout=6.0)
        # Если ничего не нашли, а в названии были маркеры версий или года, пробуем упрощенный запрос
        if not raw_items:
            simplified_tit = re.sub(r"\b(edit|version|remastered|remaster|radio|original|mix|\d{4})\b", " ", clean_tit, flags=re.I)
            simplified_tit = re.sub(r"\s+", " ", simplified_tit).strip()
            simplified_query = f"{clean_art} {simplified_tit}".strip()
            if simplified_query and simplified_query.lower() != query.lower():
                print(f"[*] Soulseek: повтор без спецслов: '{simplified_query}'...")
                raw_items = es.search(simplified_query, timeout=6.0)

        if not raw_items:
            print(f"[!] Soulseek: 0 файлов по запросу '{query}'")
        else:
            peers_cnt = len(set(item["username"] for item in raw_items))
            print(f"[*] Soulseek: получено {len(raw_items)} файлов ({peers_cnt} пиров)...")

        for item in raw_items:
            raw_candidates.append({
                "username": item["username"],
                "filename": item["filename"],
                "file_info": item,
                "has_free_slot": item.get("has_free_slot", False),
                "queue_length": item.get("queue_length", 0),
                "upload_speed": item.get("upload_speed", 0),
            })
    elif SLSKD_URL:
        # Резерв: внешний slskd HTTP API
        token = get_slskd_token()
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = httpx.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query}, headers=headers, timeout=10)
                if resp.status_code == 200:
                    search_id = resp.json()["id"]
                    start_time = time.time()
                    while time.time() - start_time < 12:
                        time.sleep(1.5)
                        status_resp = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
                        if status_resp.status_code == 200:
                            status_data = status_resp.json()
                            file_count = status_data.get("fileCount", 0)
                            is_complete = status_data.get("isComplete", False)
                            if is_complete or (file_count >= 30 and time.time() - start_time >= 5):
                                break
                    resp_results = httpx.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses", headers=headers, timeout=10)
                    if resp_results.status_code == 200:
                        for response in resp_results.json():
                            u = response.get("username")
                            slot = response.get("hasFreeUploadSlot", False)
                            q_len = response.get("queueLength", 0)
                            up_spd = response.get("uploadSpeed", 0)
                            files = response.get("files") or response.get("fileInfos") or []
                            for f_info in files:
                                if not f_info.get("isLocked", False):
                                    raw_candidates.append({
                                        "username": u,
                                        "filename": f_info.get("filename", ""),
                                        "file_info": f_info,
                                        "has_free_slot": slot,
                                        "queue_length": q_len,
                                        "upload_speed": up_spd,
                                    })
                    try:
                        httpx.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=headers, timeout=5)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[!] Soulseek: Ошибка slskd API: {e}")

    results = []
    rejected_names = 0
    rejected_lossy = 0
    rejected_duration = 0
    rejected_low_bitrate = 0
    rejected_remix = 0

    for cand in raw_candidates:
        filename = cand["filename"]
        fn_tokens = toks(filename)
        leaf_stem = Path(filename.replace("\\", "/")).stem
        leaf_tokens = toks(leaf_stem)
        
        # Артист может находиться как в названии файла, так и в структуре папок
        art_overlap = len(art_tokens & fn_tokens) / max(1, len(art_tokens))
        # Название трека ОБЯЗАНО присутствовать именно в имени аудиофайла (leaf stem),
        # чтобы исключить скачивание соседних треков из папки одноименного альбома
        tit_overlap = len(tit_tokens & leaf_tokens) / max(1, len(tit_tokens)) if tit_tokens else 1.0
        
        if art_overlap < 0.6 or tit_overlap < 0.6:
            rejected_names += 1
            continue
            
        f_info = cand["file_info"]
        quality_label, quality_score = parse_slskd_quality(
            filename=filename,
            file_info=f_info,
            target_quality=target_quality,
            expected_duration=duration
        )
        
        if quality_score <= 0:
            if quality_label == "Lossy":
                rejected_lossy += 1
            elif quality_label == "Invalid Duration":
                rejected_duration += 1
            elif "Low Bitrate" in quality_label:
                rejected_low_bitrate += 1
            continue
            
        target_query = f"{artist} {title}"
        target_vm = _version_markers(target_query)
        cand_vm = _version_markers(filename)
        extra_vm = cand_vm - target_vm
        if extra_vm & {"remix", "cover", "karaoke", "instrumental", "tribute", "parody", "live"}:
            quality_score -= 1500.0
            if quality_score <= 0:
                rejected_remix += 1
                continue
        elif extra_vm:
            quality_score -= 500.0
            if quality_score <= 0:
                rejected_remix += 1
                continue
            
        has_free_slot = cand["has_free_slot"]
        queue_length = cand["queue_length"]
        upload_speed = cand["upload_speed"]
        
        total_rank = (
            quality_score * 10000.0
            + (100000.0 if has_free_slot else 0.0)
            - min(queue_length * 50.0, 5000.0)
            + min((upload_speed or 0) / 10000.0, 1000.0)
        )
        
        results.append({
            "title": title,
            "artist": artist,
            "quality": quality_label,
            "quality_score": quality_score,
            "total_rank": total_rank,
            "bitrate": f_info.get("bitrate") or f_info.get("bitRate", 0),
            "slskd_username": cand["username"],
            "slskd_filename": filename,
            "slskd_size": f_info.get("size", 0),
            "has_free_slot": has_free_slot,
            "queue_length": queue_length,
            "upload_speed": upload_speed
        })
        
    results.sort(key=lambda x: x["total_rank"], reverse=True)

    if not results:
        if raw_candidates:
            reasons = []
            if rejected_lossy:
                reasons.append(f"{rejected_lossy} lossy")
            if rejected_duration:
                reasons.append(f"{rejected_duration} по длительности")
            if rejected_names:
                reasons.append(f"{rejected_names} не то имя")
            if rejected_low_bitrate:
                reasons.append(f"{rejected_low_bitrate} битрейт <240k")
            if rejected_remix:
                reasons.append(f"{rejected_remix} ремикс/кавер")
            reasons_str = f" ({', '.join(reasons)})" if reasons else ""
            print(f"[!] Soulseek: отсеяно {len(raw_candidates)} файлов{reasons_str}")
    else:
        best = results[0]
        free_str = "слот" if best["has_free_slot"] else f"очередь {best['queue_length']}"
        print(f"[+] Soulseek: лучший из {len(results)}: '{Path(best['slskd_filename']).name}' | {best['quality']} ({best['slskd_username']}, {free_str})")

    return results[:limit]

def download_soulseek_track(
    username: str,
    filename: str,
    size: int,
    dest_dir: Path,
    target_quality: str = "MP3",
    timeout: float = 600.0,
) -> Optional[Path]:
    """
    Скачивает файл из Soulseek (через встроенный клиент или slskd) и копирует в целевую папку.
    При необходимости транскодирует FLAC в честный MP3 320 kbps CBR.
    Безопасен при одновременном скачивании в несколько параллельных потоков.
    """
    file_name = Path(filename.replace("\\", "/")).name
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Приоритет: встроенный Soulseek клиент
    es = EmbeddedSoulseek.get_instance()
    if es:
        print(f"[*] Soulseek: скачивание '{file_name}' ({username})...")
        downloaded = es.download(username, filename, timeout=timeout)
        if downloaded and downloaded.exists():
            dest_path = dest_dir / file_name

            try:
                if target_quality == "MP3" and downloaded.suffix.lower() == ".flac":
                    print(f"[*] Soulseek: FLAC -> MP3 320k CBR...")
                    final_dest = dest_dir / dest_path.with_suffix(".mp3").name
                    temp_mp3 = dest_dir / f".tmp_{uuid.uuid4().hex}_{final_dest.name}"
                    cmd = [
                        "ffmpeg", "-y", "-i", str(downloaded),
                        "-c:a", "libmp3lame", "-b:a", "320k",
                        str(temp_mp3)
                    ]
                    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0 and temp_mp3.exists() and temp_mp3.stat().st_size > 10000:
                        try:
                            temp_mp3.replace(final_dest)
                        except Exception:
                            if final_dest.exists() and final_dest.stat().st_size > 10000:
                                temp_mp3.unlink(missing_ok=True)
                            else:
                                try:
                                    shutil.copy2(temp_mp3, final_dest)
                                    temp_mp3.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        dest_path = final_dest
                    else:
                        if temp_mp3.exists():
                            temp_mp3.unlink(missing_ok=True)
                        if downloaded.resolve() != dest_path.resolve():
                            temp_copy = dest_dir / f".tmp_{uuid.uuid4().hex}_{file_name}"
                            shutil.copy2(downloaded, temp_copy)
                            try:
                                temp_copy.replace(dest_path)
                            except Exception:
                                if dest_path.exists() and dest_path.stat().st_size > 0:
                                    temp_copy.unlink(missing_ok=True)
                                else:
                                    try:
                                        shutil.copy2(temp_copy, dest_path)
                                        temp_copy.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                else:
                    if downloaded.resolve() != dest_path.resolve():
                        temp_copy = dest_dir / f".tmp_{uuid.uuid4().hex}_{file_name}"
                        shutil.copy2(downloaded, temp_copy)
                        try:
                            temp_copy.replace(dest_path)
                        except Exception:
                            if dest_path.exists() and dest_path.stat().st_size > 0:
                                temp_copy.unlink(missing_ok=True)
                            else:
                                try:
                                    shutil.copy2(temp_copy, dest_path)
                                    temp_copy.unlink(missing_ok=True)
                                except Exception:
                                    pass

                print(f"[+] Soulseek: скачан {dest_path.name}")
                return dest_path
            finally:
                if hasattr(es, "cleanup_staging_file"):
                    es.cleanup_staging_file(downloaded)
                else:
                    try:
                        staging_dir = getattr(es, "staging_dir", None)
                        if staging_dir and downloaded.is_relative_to(staging_dir):
                            downloaded.unlink(missing_ok=True)
                            try:
                                downloaded.parent.rmdir()
                            except Exception:
                                pass
                    except Exception:
                        pass

    # 2. Резерв: внешний slskd HTTP API
    token = get_slskd_token()
    if not token:
        if not es:
            print("[!] Soulseek: клиент не настроен (задайте SLSK_USER и SLSK_PASS в .env)")
        return None
        
    headers = {"Authorization": f"Bearer {token}"}
    queue_item = {"filename": filename, "size": size}
    
    print(f"[*] Soulseek: скачивание '{file_name}' ({username}, slskd)...")
    
    try:
        resp = httpx.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{username}",
            json=[queue_item],
            headers=headers,
            timeout=10
        )
        if resp.status_code not in (200, 201):
            print(f"[!] Soulseek: Не удалось поставить в очередь (код {resp.status_code})")
            return None
            
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
                
            potential_file = search_dir / username / file_name
            if potential_file.exists():
                dest_path = dest_dir / file_name
                if potential_file.resolve() != dest_path.resolve():
                    temp_copy = dest_dir / f".tmp_{uuid.uuid4().hex}_{file_name}"
                    shutil.copy2(potential_file, temp_copy)
                    try:
                        temp_copy.replace(dest_path)
                    except Exception:
                        if dest_path.exists() and dest_path.stat().st_size > 0:
                            temp_copy.unlink(missing_ok=True)
                        else:
                            try:
                                shutil.copy2(temp_copy, dest_path)
                                temp_copy.unlink(missing_ok=True)
                            except Exception:
                                pass
                
                if target_quality == "MP3" and dest_path.suffix.lower() == ".flac":
                    print(f"[*] Soulseek: Транскодирование Lossless исходника в честный MP3 320 kbps (LAME CBR)...")
                    mp3_path = dest_path.with_suffix(".mp3")
                    temp_mp3 = dest_dir / f".tmp_{uuid.uuid4().hex}_{mp3_path.name}"
                    cmd = [
                        "ffmpeg", "-y", "-i", str(dest_path),
                        "-c:a", "libmp3lame", "-b:a", "320k",
                        str(temp_mp3)
                    ]
                    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0 and temp_mp3.exists() and temp_mp3.stat().st_size > 10000:
                        try:
                            temp_mp3.replace(mp3_path)
                        except Exception:
                            if mp3_path.exists() and mp3_path.stat().st_size > 10000:
                                temp_mp3.unlink(missing_ok=True)
                            else:
                                try:
                                    shutil.copy2(temp_mp3, mp3_path)
                                    temp_mp3.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        if dest_path.resolve() != mp3_path.resolve():
                            try:
                                dest_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                        dest_path = mp3_path
                
                print(f"[+] Soulseek: Файл найден и готов: {dest_path}")
                return dest_path
                
            for found_file in search_dir.rglob(file_name):
                if found_file.is_file():
                    dest_path = dest_dir / file_name
                    if found_file.resolve() != dest_path.resolve():
                        temp_copy = dest_dir / f".tmp_{uuid.uuid4().hex}_{file_name}"
                        shutil.copy2(found_file, temp_copy)
                        try:
                            temp_copy.replace(dest_path)
                        except Exception:
                            if dest_path.exists() and dest_path.stat().st_size > 0:
                                temp_copy.unlink(missing_ok=True)
                            else:
                                try:
                                    shutil.copy2(temp_copy, dest_path)
                                    temp_copy.unlink(missing_ok=True)
                                except Exception:
                                    pass
                            
                    if target_quality == "MP3" and dest_path.suffix.lower() == ".flac":
                        print(f"[*] Soulseek: Транскодирование Lossless исходника в честный MP3 320 kbps (LAME CBR)...")
                        mp3_path = dest_path.with_suffix(".mp3")
                        temp_mp3 = dest_dir / f".tmp_{uuid.uuid4().hex}_{mp3_path.name}"
                        cmd = [
                            "ffmpeg", "-y", "-i", str(dest_path),
                            "-c:a", "libmp3lame", "-b:a", "320k",
                            str(temp_mp3)
                        ]
                        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if res.returncode == 0 and temp_mp3.exists() and temp_mp3.stat().st_size > 10000:
                            try:
                                temp_mp3.replace(mp3_path)
                            except Exception:
                                if mp3_path.exists() and mp3_path.stat().st_size > 10000:
                                    temp_mp3.unlink(missing_ok=True)
                                else:
                                    try:
                                        shutil.copy2(temp_mp3, mp3_path)
                                        temp_mp3.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                            if dest_path.resolve() != mp3_path.resolve():
                                try:
                                    dest_path.unlink(missing_ok=True)
                                except Exception:
                                    pass
                            dest_path = mp3_path
                            
                    print(f"[+] Soulseek: Файл найден рекурсивно: {dest_path}")
                    return dest_path
                    
        print("[!] Soulseek: Загрузка завершена, но файл не найден в путях поиска slskd")
        
    except Exception as e:
        print(f"[!] Soulseek: Ошибка скачивания трека: {e}")
        
    return None
