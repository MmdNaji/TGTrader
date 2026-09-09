"""KCEX market data.

KCEX is not in ccxt and publishes no trading API for users, but its website reads
candles, depth and trades from public JSON endpoints under /api/platform/spot/market.
This module uses those for PRICES. Orders on KCEX go through the computer-use
executor (screen control), which is why KCEX + live mode requires it.

Symbols: ccxt style "BTC/USDT" -> KCEX "BTC_USDT".
Intervals: Min1 Min5 Min15 Min30 Min60 Hour4 Day1 (MEXC-style names).
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pandas as pd

BASE = "https://www.kcex.com/api/platform/spot/market"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TGTrader", "Accept": "application/json"}

INTERVALS = {"1m": ("Min1", 60), "5m": ("Min5", 300), "15m": ("Min15", 900), "30m": ("Min30", 1800),
             "1h": ("Min60", 3600), "4h": ("Hour4", 14400), "1d": ("Day1", 86400)}


def kcex_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").split(":")[0].upper()


class KcexData:
    def __init__(self, proxy: str = "", timeout: float = 20.0):
        kw: dict[str, Any] = {"headers": HEADERS, "timeout": timeout, "follow_redirects": True}
        if proxy:
            kw["proxy"] = proxy
        self._http = httpx.Client(**kw)
        self._symbols: dict[str, dict[str, Any]] | None = None

    def _get(self, path: str, **params: Any) -> Any:
        r = self._http.get(f"{BASE}/{path}", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
            raise RuntimeError(f"KCEX {path}: {data.get('msg') or data.get('code')}")
        return data

    # ------------------------------------------------------------ candles
    def candles(self, symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
        if timeframe not in INTERVALS:
            raise ValueError(f"KCEX does not serve timeframe {timeframe}; use one of {', '.join(INTERVALS)}")
        name, secs = INTERVALS[timeframe]
        end = int(time.time() * 1000)
        start = end - (limit + 2) * secs * 1000
        d = self._get("kline", symbol=kcex_symbol(symbol), interval=name, start=start, end=end)["data"]
        if not d or not d.get("t"):
            raise RuntimeError(f"KCEX returned no candles for {symbol}")
        df = pd.DataFrame({
            "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"], "volume": d["q"],
        }, index=pd.to_datetime(d["t"], unit="s", utc=True))
        df.index.name = "time"
        return df.astype(float).tail(limit)

    # ------------------------------------------------------------ price
    def price(self, symbol: str) -> float:
        d = self._get("deals", symbol=kcex_symbol(symbol), limit=1)["data"]
        trades = d.get("data") if isinstance(d, dict) else d
        if not trades:
            raise RuntimeError(f"KCEX: no trades for {symbol}")
        return float(trades[0]["p"])

    # ------------------------------------------------------------ symbols
    def symbols(self) -> dict[str, dict[str, Any]]:
        if self._symbols is None:
            data = self._get("symbols")["data"]
            out: dict[str, dict[str, Any]] = {}
            for quote, rows in data.items():
                for row in rows:
                    out[f"{row['currency']}/{quote}"] = row
            self._symbols = out
        return self._symbols

    def limits(self, symbol: str) -> tuple[float, float]:
        """(min_qty, qty_step) from the quantity scale KCEX publishes; min is one step."""
        row = self.symbols().get(symbol.upper())
        if not row:
            return 0.0, 0.0
        step = 10 ** (-int(row.get("quantityScale") or 0))
        return step, step

    def market_order_allowed(self, symbol: str) -> bool:
        row = self.symbols().get(symbol.upper())
        return bool(row and row.get("marketOrderEnabled", True))


KCEX_SCREEN_NOTES = (
    "Exchange: KCEX (kcex.com), open and logged in in the browser on this desktop.\n"
    "Go to the spot trading page for the symbol (URL pattern: https://www.kcex.com/exchange/{BASE}_{QUOTE}, e.g. "
    "https://www.kcex.com/exchange/BTC_USDT). The order form is on the right side of the chart: tabs 'Limit' / 'Market'.\n"
    "Choose the 'Market' tab. For a BUY, the form asks for the AMOUNT in the base coin or the TOTAL in USDT - use the "
    "base-coin amount field and type the quantity exactly. Then press the green 'Buy' button. For a SELL use the red 'Sell' side.\n"
    "After submitting, the order appears under 'Open Orders' / 'Order History' at the bottom; read the filled price from there.\n"
    "If a confirmation dialog or a 'market order warning' appears, read it, and only confirm if it shows the same symbol, side and amount."
)
