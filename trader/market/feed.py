"""Read the whole-market sweep the server already did, instead of doing it again.

THE ARITHMETIC THAT MADE THIS NECESSARY. Reading a chart costs one request per coin. 304 coins
on the owner's connection is about a hundred seconds of solid traffic every sweep - on the
connection they have already reported dropping. So the app was looking at the most liquid 40,
which is ten percent of the market, and the owner's reading of that from the outside was "you
only added the famous coins".

The server sweeps all of them every ten minutes and publishes one 160 KB file. The app makes
ONE request. A dropped connection now costs a refresh rather than a blind spot, because the
sweep already happened somewhere with a stable line.

WHAT THIS IS NOT. It is not an instruction and it is not a signal to act on. The feed carries
FACTS - prices, indicators, which rules fired, and headlines - and every decision is still
taken on the owner's machine by the engine and the model, with their own settings and their own
risk layer. Nothing here ever sees an API key or places an order.

AND IT IS NOT TRUSTED BLINDLY:

- A feed older than `MAX_AGE` is refused outright. Stale market data is worse than none,
  because it looks exactly like fresh market data.
- Everything is filtered by the app's OWN liquidity floor. The server publishes down to a $50k
  floor so a small account can see what it is allowed to trade; a bigger account must not be
  handed coins it cannot get out of just because they were in the file.
- A malformed row is skipped, not repaired. Guessing at a number in a money path is how a typo
  becomes a position.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

DEFAULT_URL = "http://91.107.163.109:40002/market.json"
MAX_AGE = 45 * 60.0          # 4.5 sweeps. Beyond that the server is in trouble and we say so.
TIMEOUT = 12.0


class FeedUnavailable(RuntimeError):
    """No usable feed - the caller falls back to sweeping locally."""


def fetch(url: str = DEFAULT_URL, timeout: float = TIMEOUT) -> dict[str, Any]:
    """The published sweep, or an exception. Never a partial or stale answer dressed as fresh."""
    # The same gate the exchange clients honour. A test that reaches the internet is not a test
    # of this program, and the market watch test proved it immediately: the moment the feed went
    # in, a test with its own fake market started returning REAL coins from the live server.
    from .data import offline
    if offline():
        raise FeedUnavailable("TGTRADER_OFFLINE is set - the feed is a network call")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TGTrader"})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("coins"), list):
        raise FeedUnavailable("the feed is not in the shape this version understands")
    age = time.time() - float(data.get("at") or 0.0)
    if age > MAX_AGE:
        raise FeedUnavailable(f"the feed is {age / 60:.0f} minutes old - too stale to trade on")
    data["age_s"] = age
    return data


def rows(data: dict[str, Any], min_volume: float = 0.0,
         allow_short: bool = True) -> list[dict[str, Any]]:
    """The coins this account may actually trade, in the shape the watchlist expects."""
    out: list[dict[str, Any]] = []
    for c in data.get("coins") or []:
        try:
            sym = str(c["symbol"])
            vol = float(c.get("volume_usd") or 0.0)
            price = float(c.get("price") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue                      # a broken row is skipped, never guessed at
        if price <= 0 or vol < min_volume:
            continue
        side = str(c.get("signal_side") or "")
        if not allow_short and side == "short":
            continue
        out.append(dict(c))
    return out


def headlines_for(data: dict[str, Any], symbol: str, limit: int = 4) -> list[dict[str, Any]]:
    """What was published about this coin, newest first - for the analysis panel and the model."""
    feed = data.get("news") or {}
    items = feed.get("items") or []
    idx = (feed.get("by_coin") or {}).get(symbol) or []
    out = []
    for i in idx[:limit]:
        if isinstance(i, int) and 0 <= i < len(items):
            out.append(items[i])
    return out
