"""One proxy policy for everything the app talks to: exchanges, KCEX, Claude, OpenAI, updates.

Modes (Settings.proxy_mode):
  none    - direct connection
  system  - use the Windows/OS system proxy if one is set (what most VPN apps configure),
            otherwise direct. Default.
  manual  - Settings.exchange.proxy, e.g. http://127.0.0.1:10809 (v2rayN), http://127.0.0.1:7890
            (Clash), socks5://127.0.0.1:10808
"""
from __future__ import annotations

import urllib.request
from typing import Any

from .config import Settings


def resolve_proxy(settings: Settings) -> str | None:
    mode = (getattr(settings, "proxy_mode", "system") or "system").lower()
    manual = (settings.exchange.proxy or "").strip()
    if mode == "none":
        return None
    if mode == "manual" or (mode == "system" and manual and not _system_proxy()):
        return manual or None
    return _system_proxy()


def _system_proxy() -> str | None:
    try:
        p = urllib.request.getproxies()
    except Exception:
        return None
    url = p.get("https") or p.get("http")
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    return url


def httpx_client(settings: Settings, timeout: float = 30.0):
    """A plain httpx client honouring the proxy policy (used by KCEX, updater, tests)."""
    import httpx
    proxy = resolve_proxy(settings)
    return httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True) if proxy else httpx.Client(timeout=timeout, follow_redirects=True)


def ccxt_proxy_params(settings: Settings) -> dict[str, Any]:
    proxy = resolve_proxy(settings)
    if not proxy:
        return {}
    if proxy.startswith("socks"):
        return {"socksProxy": proxy}
    return {"httpProxy": proxy, "httpsProxy": proxy}


def anthropic_http_client(settings: Settings):
    import anthropic
    proxy = resolve_proxy(settings)
    return anthropic.DefaultHttpxClient(proxy=proxy) if proxy else None


def openai_http_client(settings: Settings):
    import openai
    proxy = resolve_proxy(settings)
    return openai.DefaultHttpxClient(proxy=proxy) if proxy else None


def probe(settings: Settings) -> dict[str, Any]:
    """Where does the app appear to be connecting from? Returns ip/country and the proxy in use."""
    proxy = resolve_proxy(settings)
    with httpx_client(settings, timeout=15.0) as c:
        r = c.get("https://ipinfo.io/json", headers={"User-Agent": "TGTrader"})
        r.raise_for_status()
        d = r.json()
    return {"proxy": proxy or "direct", "ip": d.get("ip"), "country": d.get("country"), "city": d.get("city"), "org": d.get("org")}
