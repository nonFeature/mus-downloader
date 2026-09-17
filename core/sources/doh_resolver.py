import socket
import urllib.request
import json
import ssl
from typing import Dict, List

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: Dict[str, str] = {}
_doh_enabled = False

def _query_doh(host: str) -> List[str]:
    """Запрашивает A-записи домена через DNS-over-HTTPS (Cloudflare / Google)."""
    # 1. Cloudflare 1.1.1.1 DoH
    ctx = ssl.create_default_context()
    for endpoint in (
        f"https://1.1.1.1/dns-query?name={host}&type=A",
        f"https://dns.google/resolve?name={host}&type=A"
    ):
        try:
            req = urllib.request.Request(
                endpoint,
                headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=3.5, context=ctx) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    answers = data.get("Answer", [])
                    ips = [ans["data"] for ans in answers if ans.get("type") == 1 and "data" in ans]
                    if ips:
                        return ips
        except Exception:
            continue
    return []

def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # Сначала пробуем системный DNS
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as e:
        # Если системный DNS вернул ошибку разрешения (например, блокировка РКН / сбой DNS роутера)
        if isinstance(host, str) and not host.replace(".", "").isdigit() and ":" not in host:
            cached_ip = _dns_cache.get(host)
            if cached_ip:
                try:
                    return _orig_getaddrinfo(cached_ip, port, family, type, proto, flags)
                except Exception:
                    pass

            ips = _query_doh(host)
            if ips:
                _dns_cache[host] = ips[0]
                return _orig_getaddrinfo(ips[0], port, family, type, proto, flags)
        raise e

def setup_doh_fallback():
    """Активирует фолбек на DNS-over-HTTPS при сбоях системного DNS."""
    global _doh_enabled
    if not _doh_enabled:
        socket.getaddrinfo = _custom_getaddrinfo
        _doh_enabled = True
