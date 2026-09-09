"""Market data through ccxt (crypto) or MetaTrader 5 (forex, Windows only)."""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from ..config import Settings


def _ccxt_exchange(settings: Settings, authenticated: bool = False) -> Any:
    import ccxt  # imported lazily so the GUI starts even if ccxt is missing

    ex_cls = getattr(ccxt, settings.exchange.exchange_id)
    params: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if authenticated:
        params.update({
            "apiKey": settings.exchange.api_key,
            "secret": settings.exchange.secret,
        })
        if settings.exchange.password:
            params["password"] = settings.exchange.password
    if settings.exchange.proxy:
        params["proxies"] = {"http": settings.exchange.proxy, "https": settings.exchange.proxy}
        params["httpsProxy"] = settings.exchange.proxy
    ex = ex_cls(params)
    if settings.exchange.sandbox and hasattr(ex, "set_sandbox_mode"):
        try:
            ex.set_sandbox_mode(True)
        except Exception:
            pass
    return ex


def ohlcv_to_frame(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("time").drop(columns=["ts"])
    return df.astype(float)


class MarketData:
    """Fetches candles and prices. One instance per engine; caches the exchange object."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._ex = None
        self._kcex = None
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    @property
    def is_kcex(self) -> bool:
        return self.settings.market == "crypto" and self.settings.exchange.exchange_id.lower() == "kcex"

    @property
    def kcex(self):
        if self._kcex is None:
            from .kcex import KcexData
            self._kcex = KcexData(proxy=self.settings.exchange.proxy)
        return self._kcex

    @property
    def exchange(self):
        if self._ex is None:
            self._ex = _ccxt_exchange(self.settings, authenticated=False)
        return self._ex

    def candles(self, symbol: str, timeframe: str | None = None, limit: int = 400, max_age: float = 20.0) -> pd.DataFrame:
        tf = timeframe or self.settings.timeframe
        key = (symbol, tf)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < max_age and len(cached[1]) >= limit:
            return cached[1].tail(limit)
        if self.settings.market == "forex":
            df = self._mt5_candles(symbol, tf, limit)
        elif self.is_kcex:
            df = self.kcex.candles(symbol, tf, limit)
        else:
            rows = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            df = ohlcv_to_frame(rows)
        self._cache[key] = (now, df)
        return df

    def price(self, symbol: str) -> float:
        if self.settings.market == "forex":
            import MetaTrader5 as mt5  # type: ignore
            tick = mt5.symbol_info_tick(symbol.replace("/", ""))
            return float((tick.bid + tick.ask) / 2)
        if self.is_kcex:
            return self.kcex.price(symbol)
        t = self.exchange.fetch_ticker(symbol)
        return float(t["last"] or t["close"])

    # ------------------------------------------------------------ forex
    def _mt5_candles(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        import MetaTrader5 as mt5  # type: ignore

        tf_map = {"1m": mt5.TIMEFRAME_M1, "5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15, "30m": mt5.TIMEFRAME_M30,
                  "1h": mt5.TIMEFRAME_H1, "4h": mt5.TIMEFRAME_H4, "1d": mt5.TIMEFRAME_D1}
        if not mt5.initialize(login=self.settings.mt5_login or None, password=self.settings.mt5_password or None,
                              server=self.settings.mt5_server or None):
            raise RuntimeError(f"MetaTrader5 initialize failed: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol.replace("/", ""), tf_map[tf], 0, limit)
        if rates is None:
            raise RuntimeError(f"MetaTrader5 returned no data for {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"}).set_index("time")[["open", "high", "low", "close", "volume"]]
        return df.astype(float)
