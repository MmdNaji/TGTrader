"""Market data through ccxt (crypto) or MetaTrader 5 (forex, Windows only)."""
from __future__ import annotations

import os
import time
from typing import Any, Callable

import pandas as pd

from ..config import Settings


def offline() -> bool:
    """True when this process must not touch the network.

    The test suite sets TGTRADER_OFFLINE, the same way it already sets TGTRADER_NO_AUTOUPDATE.
    A GUI test builds a real window, the chart page starts a background candle fetch, and that
    thread was still waiting on an SSL read when the suite ended - so the teardown fell through
    to os._exit, which on Windows tears every thread down where it stands. Doing that to a
    thread inside OpenSSL is itself an access violation, and the whole run exited 0xC0000005
    with 88 tests passed and nothing reported as failed.

    A test that reaches the internet is not a test of this program anyway: it is slow, it
    depends on where the machine is, and here it was the difference between a green run and a
    crash. So the network is closed at the two places that open it.
    """
    return bool(os.environ.get("TGTRADER_OFFLINE"))


def _ccxt_exchange(settings: Settings, authenticated: bool = False) -> Any:
    if offline():
        raise RuntimeError("TGTRADER_OFFLINE: no network in this process")
    import ccxt  # imported lazily so the GUI starts even if ccxt is missing

    ex_cls = getattr(ccxt, settings.exchange.exchange_id)
    params: dict[str, Any] = {"enableRateLimit": True, "timeout": 20000,
                              "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}}
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
# "gate", not "gateio": ccxt renamed the id and `ccxt.gateio` does not exist, so that entry
# was a guaranteed AttributeError in the middle of every fallback walk. Anything listed here
# is checked against the installed ccxt at import time below, so a rename cannot go unnoticed
# again.
FALLBACK_SOURCES = ["mexc", "kcex", "gate", "htx", "bitget", "kucoin"]


def available_sources() -> list[str]:
    """The fallback list, minus anything this build of ccxt does not actually have."""
    try:
        import ccxt
    except Exception:
        return list(FALLBACK_SOURCES)
    return [s for s in FALLBACK_SOURCES if s == "kcex" or hasattr(ccxt, s)]


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
        # Set by a caller that can be asked to stop (the GUI price feed). Returning True makes
        # the fallback chain give up instead of walking every remaining source.
        self.abort: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------ sources
    def _sources(self) -> list[str]:
        cfg = (self.settings.exchange.exchange_id or "bybit").lower()
        pref = (getattr(self.settings, "data_source", "auto") or "auto").lower()
        if pref != "auto":
            return [pref]
        return [cfg] + [f for f in available_sources() if f != cfg]

    @property
    def is_kcex(self) -> bool:
        return self.settings.market == "crypto" and (self.active_source or self.settings.exchange.exchange_id).lower() == "kcex"

    @property
    def kcex(self):
        if offline():
            raise RuntimeError("TGTRADER_OFFLINE: no network in this process")
        if self._kcex is None:
            from .kcex import KcexData
            from ..net import resolve_proxy
            self._kcex = KcexData(proxy=resolve_proxy(self.settings) or "")
        return self._kcex

    def _ex(self, ex_id: str):
        if offline():
            raise RuntimeError("TGTRADER_OFFLINE: no network in this process")
        if ex_id not in self._exs:
            import ccxt
            cls = getattr(ccxt, ex_id)
            from ..net import ccxt_proxy_params
            # fetchMarkets is limited to spot on purpose. load_markets() is called implicitly by
            # the first price/candle request, and an exchange otherwise walks spot, swap, future
            # AND option markets - each a request of its own, so ONE candle call can block a
            # thread for minutes behind a proxy. This app only trades spot.
            #
            # It has to be {"types": [...]}. A bare ["spot"] was here, and an exchange that
            # honours this option reads it with safe_dict(), which returns None for a LIST and
            # falls back to its own defaults - so the limit never applied.
            #
            # WHICH exchanges it changes, measured by stubbing each one's fetch_*_markets and
            # calling fetch_markets() - no network, no guessing:
            #
            #   gate     4 calls -> 1     (spot, swap, future, option -> spot)
            #   kucoin   fetched contract markets -> fetches none of them
            #   bybit    unchanged        mexc  unchanged        bitget  unchanged
            #
            # So this is a gate fix, and gate is where a thread sat in an SSL read inside
            # fetch_future_markets at the end of the Windows test run. It is NOT a general
            # speed-up, and the Windows session measured that correctly: with bybit configured,
            # the self-test timings did not move. An earlier version of this comment claimed it
            # spared bybit four market types; that was wrong about bybit.
            #
            # And load_markets() caches on the exchange OBJECT, so even on gate the saving is
            # paid once per object, not per call - every new MarketData (the price feed builds
            # one on each restart; the engine and the scanner build their own), up to six times
            # over when the fallback chain walks. Steady-state candle and price throughput is
            # unchanged. What it buys is that a source nothing here trades cannot hang a thread.
            params: dict = {"enableRateLimit": True, "timeout": 20000,
                            "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}}
            params.update(ccxt_proxy_params(self.settings))
            self._exs[ex_id] = cls(params)
        return self._exs[ex_id]

    @property
    def exchange(self):
        return self._ex((self.active_source or self.settings.exchange.exchange_id).lower())

    def _try_sources(self, what: str, fn):
        """Run fn(source) on the active source, else walk the chain; remember what worked.

        ``abort`` lets a caller that has been asked to shut down stop walking the chain. Six
        sources at a 20-second timeout is two minutes of work nobody wants any more, and that
        is the whole reason a price feed could not be joined when the window closed."""
        order = [self.active_source] if self.active_source else []
        order += [s for s in self._sources() if s not in order]
        errors: list[str] = []
        for src in order:
            if self.abort():
                raise RuntimeError(f"{what}: abandoned, shutting down")
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
                out = (self.kcex.candles(symbol, tf, limit) if src == "kcex"
                       else ohlcv_to_frame(self._ex(src).fetch_ohlcv(symbol, tf, limit=limit)))
                # An exchange that answers 200 with an empty list is NOT a working source. Taking
                # it as success cached an empty frame, defeated the fallback chain, and every
                # caller then died on df["close"].iloc[-1] with an IndexError.
                if out is None or len(out) < 2:
                    raise RuntimeError(f"{src} returned no candles for {symbol} {tf}")
                return out
            df = self._try_sources(f"{symbol} {tf}", fetch)
        if df is None or df.empty:
            raise RuntimeError(f"no candles for {symbol} {tf}")
        self._cache[key] = (now, df)
        return df

    def price(self, symbol: str) -> float:
        if self.settings.market == "forex":
            import MetaTrader5 as mt5  # type: ignore
            sym = symbol.replace("/", "")
            # symbol_info_tick returns None for a symbol that was never selected, and on a
            # terminal this process has not initialised. Falling through to tick.bid then threw
            # AttributeError - out of close_all, which is the one call that must not die halfway.
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                mt5.initialize(login=self.settings.mt5_login or None,
                               password=self.settings.mt5_password or None,
                               server=self.settings.mt5_server or None)
                mt5.symbol_select(sym, True)
                tick = mt5.symbol_info_tick(sym)
            if tick is None or not (tick.bid and tick.ask):
                raise RuntimeError(f"MT5 has no price for {sym}")
            return float((tick.bid + tick.ask) / 2)

        def fetch(src: str) -> float:
            if src == "kcex":
                return self.kcex.price(symbol)
            t = self._ex(src).fetch_ticker(symbol)
            return float(t["last"] or t["close"])
        return self._try_sources(symbol, fetch)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """Every one of these in ONE request, where the exchange can do that.

        MEASURED on bybit: `fetch_tickers()` returns all 538 spot symbols in 0.20s, while four
        individual `fetch_ticker` calls take 1.00s. So the old price loop - one request per
        symbol, per cycle - was both slower AND the reason it had to rotate: asking for eight
        symbols every second was eight requests a second, which is what produced "Too Many
        Requests" and left the bot with no prices at all for the coins it was holding.

        One request means every watched symbol can update every cycle instead of taking turns,
        which is what the owner asked for: "make the price changes as instant as possible, in
        everything".

        Falls back to per-symbol on anything that cannot do it, so a source without a bulk
        endpoint still works exactly as before.
        """
        if not symbols:
            return {}
        if self.settings.market == "forex":
            out = {}
            for sym in symbols:
                try:
                    out[sym] = self.price(sym)
                except Exception:
                    pass
            return out

        def fetch(_sym: str) -> dict[str, float]:
            src = self.active_source or self.settings.exchange.exchange_id
            if src == "kcex":
                raise RuntimeError("kcex has no bulk ticker endpoint")
            ex = self._ex(src)
            if not getattr(ex, "has", {}).get("fetchTickers"):
                raise RuntimeError(f"{src} has no fetchTickers")
            tk = ex.fetch_tickers(list(symbols))
            out: dict[str, float] = {}
            for sym in symbols:
                t = tk.get(sym) or {}
                px = t.get("last") or t.get("close")
                if px:
                    out[sym] = float(px)
            if not out:
                raise RuntimeError("the bulk ticker call returned nothing usable")
            return out

        try:
            # _try_sources wants a symbol for its messages; the work is over the whole list.
            return self._try_sources(symbols[0], fetch)
        except Exception:
            out = {}
            for sym in symbols:
                try:
                    out[sym] = self.price(sym)
                except Exception:
                    pass
            return out

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
