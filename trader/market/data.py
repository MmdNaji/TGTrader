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
    from ..net import ccxt_proxy_params
    params.update(ccxt_proxy_params(settings))
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


# Public market-data sources tried in order when the configured exchange is unreachable
# (Bybit, Binance, OKX and KuCoin answer 403 "blocked from your country" from Iran).
FALLBACK_SOURCES = ["mexc", "kcex", "gateio", "htx", "bitget"]


def _blocked(exc: Exception) -> bool:
    m = str(exc).lower()
    return any(k in m for k in ("403", "forbidden", "country", "restricted", "cloudfront", "451", "unavailable in your", "timed out", "timeout", "connection", "resolve"))


class MarketData:
    """Fetches candles and prices with an automatic fallback chain of public sources.

    One instance per engine. ``active_source`` says which exchange the data is actually
    coming from; ``notice`` (str or None) explains a switch so the UI can show it.
    """

    def __init__(self, settings: Settings, on_notice=None):
        self.settings = settings
        self._exs: dict[str, object] = {}
        self._kcex = None
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self.active_source: str | None = None
        self.notice: str | None = None
        self.on_notice = on_notice or (lambda s: None)

    # ------------------------------------------------------------ sources
    def _sources(self) -> list[str]:
        cfg = (self.settings.exchange.exchange_id or "bybit").lower()
        pref = (getattr(self.settings, "data_source", "auto") or "auto").lower()
        if pref != "auto":
            return [pref]
        return [cfg] + [f for f in FALLBACK_SOURCES if f != cfg]

    @property
    def is_kcex(self) -> bool:
        return self.settings.market == "crypto" and (self.active_source or self.settings.exchange.exchange_id).lower() == "kcex"

    @property
    def kcex(self):
        if self._kcex is None:
            from .kcex import KcexData
            from ..net import resolve_proxy
            self._kcex = KcexData(proxy=resolve_proxy(self.settings) or "")
        return self._kcex

    def _ex(self, ex_id: str):
        if ex_id not in self._exs:
            import ccxt
            cls = getattr(ccxt, ex_id)
            from ..net import ccxt_proxy_params
            params: dict = {"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "spot"}}
            params.update(ccxt_proxy_params(self.settings))
            self._exs[ex_id] = cls(params)
        return self._exs[ex_id]

    @property
    def exchange(self):
        return self._ex((self.active_source or self.settings.exchange.exchange_id).lower())

    def _try_sources(self, what: str, fn):
        """Run fn(source) on the active source, else walk the chain; remember what worked."""
        order = [self.active_source] if self.active_source else []
        order += [s for s in self._sources() if s not in order]
        errors: list[str] = []
        for src in order:
            try:
                out = fn(src)
                if self.active_source != src:
                    cfg = self.settings.exchange.exchange_id.lower()
                    if src != cfg and errors:
                        self.notice = f"منبع داده: {src} (چون {cfg} از این‌جا در دسترس نیست: {errors[0][:90]})"
                        self.on_notice(self.notice)
                    self.active_source = src
                return out
            except Exception as exc:
                errors.append(f"{src}: {str(exc).splitlines()[0][:120]}")
                if not _blocked(exc) and src == order[0] and len(order) > 1 and "not found" in str(exc).lower():
                    # a bad symbol, not a blocked exchange - do not hop sources for it
                    raise
        raise RuntimeError(f"no market-data source reachable for {what}: " + " | ".join(errors))

    # ------------------------------------------------------------ data
    def candles(self, symbol: str, timeframe: str | None = None, limit: int = 400, max_age: float = 20.0) -> pd.DataFrame:
        tf = timeframe or self.settings.timeframe
        key = (symbol, tf)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < max_age and len(cached[1]) >= limit:
            return cached[1].tail(limit)
        if self.settings.market == "forex":
            df = self._mt5_candles(symbol, tf, limit)
        else:
            def fetch(src: str) -> pd.DataFrame:
                if src == "kcex":
                    return self.kcex.candles(symbol, tf, limit)
                return ohlcv_to_frame(self._ex(src).fetch_ohlcv(symbol, tf, limit=limit))
            df = self._try_sources(f"{symbol} {tf}", fetch)
        self._cache[key] = (now, df)
        return df

    def price(self, symbol: str) -> float:
        if self.settings.market == "forex":
            import MetaTrader5 as mt5  # type: ignore
            tick = mt5.symbol_info_tick(symbol.replace("/", ""))
            return float((tick.bid + tick.ask) / 2)

        def fetch(src: str) -> float:
            if src == "kcex":
                return self.kcex.price(symbol)
            t = self._ex(src).fetch_ticker(symbol)
            return float(t["last"] or t["close"])
        return self._try_sources(symbol, fetch)

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
