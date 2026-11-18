"""Подписанные сессии Zarz (api.zarz.moe) — шлюз к пулу платных аккаунтов.

Вендорено из Mornye `utilities.sflx` (signed_session_mobile/solver/errors).
Через этот шлюз работают источники Tidal, Qobuz, Amazon и Beatport:

- первичный доступ: ``bootstrap`` → Cloudflare Turnstile решается в реальном
  Chrome (pydoll) → ``exchange_grant`` → сессия сохраняется на диск;
- далее каждый запрос подписывается HMAC (схема ``ZARZ-HMAC-V1``), а
  скачивание — одноразовым тикетом (``POST /tickets`` → ``X-Zarz-Ticket``).

Клиент асинхронный, пайплайн проекта синхронный, поэтому здесь же живёт
мост: фоновый event loop в отдельном потоке (тот же приём, что в soulseek).
"""
import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .signed_session_mobile import SignedSessionClient

BASE_URL_DEFAULT = "https://api.zarz.moe/v2"

#: appVersion должен совпадать с официальными экстеншенами Mornye: сессия
#: на сервере скоупится по (namespace, baseUrl, appVersion, platform), и у
#: каждого провайдера она своя.
APP_VERSIONS = {
    "tidal": "tidal-web@1.2.5",
    "qobuz": "qobuz-web@1.2.21",
    "amazon": "amzn@2.3.12",
    "beatport": "beatport@1.0.0",
}

_clients: dict[str, SignedSessionClient] = {}
_clients_lock = threading.Lock()

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


class ZarzUnavailableError(RuntimeError):
    """Zarz-шлюз недоступен (нет Chrome/pydoll, капча не решилась, отказ API)."""


class ZarzAPIError(RuntimeError):
    """Шлюз ответил 4xx/5xx структурированным JSON (code/retryable/retry_after)."""

    def __init__(
        self,
        status: int,
        method: str,
        path: str,
        code: str = "",
        retry_after: float = 0.0,
        retryable: bool = False,
        retry_mode: str = "",
        snippet: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.retry_after = float(retry_after or 0.0)
        self.retryable = bool(retryable)
        self.retry_mode = retry_mode
        super().__init__(
            f"zarz HTTP {status} для {method} {path}"
            + (f" [{code}]" if code else "")
            + (f": {snippet}" if snippet else "")
        )


def _get_loop() -> asyncio.AbstractEventLoop:
    """Фоновый event loop для async-клиента (живёт в daemon-потоке)."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed() or not (_loop_thread and _loop_thread.is_alive()):
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever, daemon=True, name="zarz-loop"
            )
            _loop_thread.start()
        return _loop


def run_sync(coro, timeout: Optional[float] = None) -> Any:
    """Выполняет корутину на фоновом loop из синхронного кода."""
    future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return future.result(timeout)


def _base_url() -> str:
    try:
        import config

        return (getattr(config, "ZARZ_API_BASE", "") or BASE_URL_DEFAULT).rstrip("/")
    except Exception:
        return BASE_URL_DEFAULT


def get_client(provider: str) -> SignedSessionClient:
    """Возвращает (и кэширует) клиент для провайдера из APP_VERSIONS."""
    provider = provider.lower()
    if provider not in APP_VERSIONS:
        raise ValueError(f"unknown zarz provider: {provider}")
    with _clients_lock:
        client = _clients.get(provider)
        if client is None:
            client = SignedSessionClient(
                base_url=_base_url(),
                namespace="zarz-v2",
                app_version=APP_VERSIONS[provider],
                platform="extension",
            )
            _clients[provider] = client
        return client


_auth_lock: Optional[asyncio.Lock] = None


def _get_auth_lock() -> asyncio.Lock:
    global _auth_lock
    if _auth_lock is None:
        _auth_lock = asyncio.Lock()
    return _auth_lock


async def _authenticate(client: SignedSessionClient) -> None:
    """Bootstrap + (при необходимости) авто-решение Turnstile в браузере.

    С несколькими попытками: solver даёт виджету 3×10с на решение, на
    нестабильной сети этого может не хватить. Каждая попытка стартует
    со свежего challenge (bootstrap кэширует pending_auth_url — сбрасываем).
    """
    if client.authenticated:
        await client.ensure_session()
        return

    # Защита от параллельного запуска браузеров: когда гонка источников
    # стартует с пустым кэшем сессий, провайдеры должны проходить авторизацию
    # строго по очереди, а не запускать 4 экземпляра Chromium одновременно.
    lock = _get_auth_lock()
    async with lock:
        if client.authenticated:
            await client.ensure_session()
            return
        attempts = max(1, int(os.getenv("ZARZ_AUTH_ATTEMPTS", "3")))
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                result = await client.bootstrap()
                if result is True:
                    return
                # result — auth URL: нужна капча. Решаем её в реальном Chrome (pydoll).
                await client.authenticate_with_turnstile(timeout=120.0, hold_open_seconds=4.0)
                return
            except Exception as e:
                last_error = e
                client.pending_auth_url = None
                client.pending_challenge_id = None
                client.pending_sitekey = None
                if attempt < attempts:
                    await asyncio.sleep(2.0)
        assert last_error is not None
        raise last_error


async def _ensure_auth_async(
    provider: str, status_callback: Optional[Callable[[str], None]] = None
) -> SignedSessionClient:
    """Async-ядро авторизации: bootstrap + (при необходимости) Turnstile.

    Использовать из корутин на фоновом loop; синхронная обёртка —
    ensure_authenticated(). Вложенный run_sync из loop-потока deadlock'ит.
    """
    client = get_client(provider)
    if not client.authenticated and status_callback:
        status_callback(
            f"[*] Zarz ({provider}): первичная авторизация — решаю капчу в браузере..."
        )
    await _authenticate(client)
    return client


def ensure_authenticated(
    provider: str, status_callback: Optional[Callable[[str], None]] = None
) -> SignedSessionClient:
    """Синхронно: клиент провайдера с живой сессией.

    Бросает ZarzUnavailableError, если шлюз недоступен — пайплайн ловит её
    и переключается на следующий источник.
    """
    try:
        return run_sync(_ensure_auth_async(provider, status_callback), timeout=300)
    except ImportError as exc:  # pydoll не установлен
        raise ZarzUnavailableError(f"нужен pydoll и Chrome для решения капчи: {exc}") from exc
    except Exception as exc:
        raise ZarzUnavailableError(f"сессия Zarz ({provider}) недоступна: {exc}") from exc


async def _signed_json(
    client: SignedSessionClient,
    method: str,
    path: str,
    json_body: Any = None,
    extra_headers: Optional[dict] = None,
) -> Any:
    resp = await client.request(method, path, json_body=json_body, extra_headers=extra_headers)
    if resp.status_code >= 400:
        import contextlib

        data: dict = {}
        with contextlib.suppress(Exception):
            parsed = resp.json()
            if isinstance(parsed, dict):
                data = parsed
        raise ZarzAPIError(
            status=resp.status_code,
            method=method,
            path=path,
            code=str(data.get("code") or ""),
            retry_after=float(data.get("retry_after_seconds") or 0),
            retryable=bool(data.get("retryable")),
            retry_mode=str(data.get("retry_mode") or ""),
            snippet=resp.text[:200].replace("\n", " "),
        )
    return resp.json()


async def _ticket_and_download_info(
    provider: str,
    ticket_provider: str,
    resource_id: str,
    dl_path: str,
    dl_body: dict,
    hash_provider: Optional[str] = None,
    max_retries: int = 2,
) -> Any:
    """Тикет + подписанный POST на /dl/... одним заходом.

    ``hash_provider`` — когда resource_hash считается от другого имени, чем
    provider в теле /tickets (Beatport: тело ``btp``, хэш ``beatport:...``).

    Retryable-отказы шлюза (429/503 PROVIDER_BUSY и т.п. с retry_after ≤ 60с)
    ретраятся со свежим тикетом: тикет одноразовый, переиспользовать нельзя.
    """
    client = await _ensure_auth_async(provider)

    async def _mint_ticket() -> str:
        if hash_provider:
            resource_hash = hashlib.sha256(
                f"{hash_provider}:track:{str(resource_id).lower()}".encode()
            ).hexdigest()
            ticket_resp = await _signed_json(
                client,
                "POST",
                "/tickets",
                json_body={
                    "capability": "download_ticket",
                    "provider": ticket_provider,
                    "resource_hash": resource_hash,
                },
            )
            ticket_id = str(ticket_resp.get("ticket_id") or ticket_resp.get("ticket") or "").strip()
            if not ticket_id:
                raise RuntimeError(f"zarz не выдал тикет: {ticket_resp}")
            return ticket_id
        return await client.get_download_ticket(ticket_provider, resource_id)

    attempt = 0
    while True:
        try:
            ticket_id = await _mint_ticket()
            return await _signed_json(
                client,
                "POST",
                dl_path,
                json_body=dl_body,
                extra_headers={"X-Zarz-Ticket": ticket_id},
            )
        except ZarzAPIError as e:
            retryable = (e.retryable or e.status in (429, 503)) and 0 < e.retry_after <= 60
            if not retryable or attempt >= max_retries:
                raise
            attempt += 1
            await asyncio.sleep(max(e.retry_after, 1.0))


async def _signed_json_auth(
    provider: str,
    method: str,
    path: str,
    json_body: Any = None,
    extra_headers: Optional[dict] = None,
) -> Any:
    client = await _ensure_auth_async(provider)
    return await _signed_json(client, method, path, json_body=json_body, extra_headers=extra_headers)


def session_ready(provider: str) -> bool:
    """True, если на диске уже есть живая сессия провайдера.

    Не делает сети и не запускает капчу — используется поиском, чтобы
    не открывать браузер посреди пользовательского запроса.
    """
    try:
        return get_client(provider).authenticated
    except Exception:
        return False


def signed_json_sync(
    provider: str,
    method: str,
    path: str,
    json_body: Any = None,
    extra_headers: Optional[dict] = None,
    timeout: float = 120.0,
) -> Any:
    """Синхронный подписанный запрос (поиск/метаданные через zarz)."""
    return run_sync(
        _signed_json_auth(provider, method, path, json_body=json_body, extra_headers=extra_headers),
        timeout=timeout,
    )


def ticketed_download_info_sync(
    provider: str,
    ticket_provider: str,
    resource_id: str,
    dl_path: str,
    dl_body: dict,
    hash_provider: Optional[str] = None,
    timeout: float = 300.0,
) -> Any:
    """Синхронно: тикет + POST /dl/... → JSON-ответ провайдера."""
    return run_sync(
        _ticket_and_download_info(
            provider, ticket_provider, resource_id, dl_path, dl_body, hash_provider
        ),
        timeout=timeout,
    )


def stream_download(
    url: str,
    dest_path: Path,
    source_label: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
    headers: Optional[dict] = None,
    expected_size: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Обычное HTTP-скачивание файла с прогрессом (формат как у deezer.py).

    ``cancel_event`` (threading.Event) — кооперативная отмена: используется
    гонкой источников, чтобы обрывать скачивание проигравших.
    """
    request_headers = {"User-Agent": _browser_ua()}
    if headers:
        request_headers.update(headers)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with httpx.stream("GET", url, headers=request_headers, timeout=60.0, follow_redirects=True) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            start_time = time.time()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("скачивание отменено (гонка)")
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        eta = (total_bytes - downloaded) / speed if (speed > 0 and total_bytes > downloaded) else None
                        progress_callback({
                            "stage": "download",
                            "source": source_label,
                            "description": f"{source_label}: скачивание...",
                            "downloaded_bytes": downloaded,
                            "total_bytes": total_bytes,
                            "speed": speed,
                            "eta": eta,
                        })
        if expected_size and downloaded != expected_size:
            raise RuntimeError(
                f"неполный файл: {downloaded} из {expected_size} байт"
            )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(dest_path)
    return dest_path


def _browser_ua() -> str:
    try:
        import config

        return config.BROWSER_USER_AGENT
    except Exception:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )


def is_flac(path: Path) -> bool:
    """Проверяет magic-байты FLAC (fLaC) — защита от подменённого mp3."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"fLaC"
    except OSError:
        return False
