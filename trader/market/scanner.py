"""Look at the whole market instead of a hand-typed list of eight symbols.

What this is NOT: a way to find coins that will make money. That was measured before it was
built - volatility, 6-month momentum, trend strength and past backtest performance were each
tested as a way of PICKING which coins to trade, on 28 liquid pairs, ranking on one half of the
history and scoring on the other. Nothing survived. Momentum looked strong at one split point
(+3.7% against +1.3% for trading everything) and its edge averaged -0.10% once the split was
walked across four dates, which is the signature of a rule fitted to a date. Volatility
correlated +0.005 with return.

So this ranks and reports; it does not recommend. The numbers next to each coin are there so
the owner can choose with something in front of them rather than typing tickers from memory.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..config import Settings

# Coins below this in 24h quote volume are skipped: a position that is a meaningful share of
# the day's turnover cannot be entered or left at the price on the screen.
#
# THIS IS A FALLBACK, NOT THE RULE. A flat $3M floor was doing something nobody chose: measured
# against the live bybit spot market on 2026-09-12, 390 USDT pairs are active and exactly 39 of
# them clear $3M - so "watch the whole market" was watching TEN PERCENT of it, and the ninety
# percent it skipped is the part nobody has already bid up. The owner noticed from the outside:
# "you only added the famous coins."
#
# What actually makes a coin untradeable is not its volume, it is OUR POSITION against its
# volume - so the floor is computed from the account (see `volume_floor`) and this constant is
# only what a caller gets when it does not say how much money it has.
MIN_QUOTE_VOLUME = 3_000_000.0

# The largest share of a day's turnover one position may be. At 0.2% a $250 position needs a
# coin doing $125,000 a day, which is a real market with real spreads - and it is a number that
# scales itself: a bigger account is automatically pushed back towards the liquid end, and a
# small one is allowed into coins a big one has no business in.
MAX_SHARE_OF_DAY = 0.002


def volume_floor(position_cap: float, share: float = MAX_SHARE_OF_DAY,
                 floor: float = 50_000.0) -> float:
    """The least daily turnover a coin needs before this account may take a position in it.

    Measured against the live market, this is the difference between watching 39 coins and
    watching 260:

        capital   position cap   floor       coins that clear it (bybit spot, 390 active)
          $1,000        $250     $125,000        260
         $10,000      $2,500     $1,250,000       82
        the old flat constant    $3,000,000        39

    `floor` is the hard bottom whatever the arithmetic says: below about $50k a day there is
    usually no book to speak of, and a stop that cannot be filled is not a stop.
    """
    if position_cap <= 0:
        return MIN_QUOTE_VOLUME
    return max(floor, position_cap / max(share, 1e-9))

# A pair that barely moves cannot pay for the round trip it takes to trade it. The first live
# run of this scanner put USDC/USDT near the top on volume alone - a stablecoin pair with a
# 0.04% daily range, where the fee is many times the whole day's movement.
MIN_DAY_RANGE_PCT = 0.5

# Named as well as measured, because a stablecoin can have a quiet day for other reasons and a
# peg can wobble past the range floor without becoming worth trading.
STABLECOINS = {
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDE", "USDP", "PYUSD", "USDD",
    "EURT", "EURS", "USTC", "GUSD", "LUSD", "FRAX", "SUSD", "USD1", "RLUSD",
}


class ScanUnavailable(RuntimeError):
    """The active data source cannot list a market (KCEX has no tickers endpoint)."""


def scan(settings: Settings, market_data, limit: int = 40, quote: str = "USDT",
         min_volume: float = MIN_QUOTE_VOLUME,
         on_progress: Callable[[str], None] | None = None,
         abort: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Rank the liquid pairs on the active exchange. One cheap pass, no candles.

    Everything here comes from a single fetch_tickers call, so it is one request rather than
    one per symbol - the price feed's own rate-limit lesson.
    """
    say = on_progress or (lambda m: None)
    stop = abort or (lambda: False)
    src = (market_data.active_source or settings.exchange.exchange_id or "").lower()
    if src == "kcex":
        raise ScanUnavailable(
            "اسکن بازار از KCEX ممکن نیست (لیست بازار ندارد). در تنظیمات «منبع داده» را روی "
            "auto یا یک صرافی دیگر بگذار.")
    ex = market_data._ex(src)
    say("گرفتن فهرست بازار…")
    ex.load_markets()
    tickers = ex.fetch_tickers()
    if stop():
        return []

    out: list[dict[str, Any]] = []
    for sym, t in tickers.items():
        if not sym.endswith("/" + quote):
            continue
        m = ex.markets.get(sym) or {}
        if not m.get("spot") or not m.get("active"):
            continue
        qv = float(t.get("quoteVolume") or 0.0)
        if qv < min_volume:
            continue
        last = float(t.get("last") or t.get("close") or 0.0)
        if last <= 0:
            continue
        base = sym.split("/")[0].upper()
        if base in STABLECOINS:
            continue          # a peg traded against another peg is a fee generator
        hi, lo = float(t.get("high") or 0.0), float(t.get("low") or 0.0)
        rng = ((hi - lo) / last * 100.0) if (hi and lo) else 0.0
        if rng and rng < MIN_DAY_RANGE_PCT:
            continue          # catches the pegs this list does not know about
        out.append({
            "symbol": sym,
            "price": last,
            "volume_usd": qv,
            # the day's range as a share of price: what the fee has to be small against
            "range_pct": rng,
            "change_pct": float(t.get("percentage") or 0.0),
        })
    out.sort(key=lambda r: -r["volume_usd"])
    say(f"{len(out)} نماد نقدشونده پیدا شد")
    return out[:limit]


def deepen(settings: Settings, market_data, rows: list[dict[str, Any]], timeframe: str = "1d",
           bars: int = 200, on_progress: Callable[[str], None] | None = None,
           abort: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Add what needs candles: realised volatility, momentum, regime and any live rule signal.

    One request per symbol, so it is deliberately a second step the owner asks for rather than
    something that runs on every scan.
    """
    from .indicators import enrich
    from ..strategy.regime import detect_regime
    from ..strategy.builtin import evaluate_all, DEFAULT_STRATEGIES

    say = on_progress or (lambda m: None)
    stop = abort or (lambda: False)
    done = []
    for i, r in enumerate(rows, 1):
        if stop():
            break
        say(f"بررسی {r['symbol']}  ({i} از {len(rows)})")
        try:
            df = enrich(market_data.candles(r["symbol"], timeframe, limit=bars))
            if len(df) < 60:
                raise RuntimeError("تاریخچه کافی نیست")
            closed = df.iloc[:-1] if len(df) > 80 else df
            atr = float(df["atr14"].iloc[-1] or 0.0)
            px = float(df["close"].iloc[-1])
            regime = detect_regime(closed)
            sigs = evaluate_all(r["symbol"], closed, regime, DEFAULT_STRATEGIES)
            best = max(sigs, key=lambda s: s.strength) if sigs else None
            r = dict(r)
            r.update({
                "atr_pct": (atr / px * 100.0) if px else 0.0,
                "mom_pct": (float(df["close"].iloc[-1] / df["close"].iloc[-31] - 1) * 100.0
                            if len(df) > 31 else 0.0),
                "regime": regime,
                "signal": (f"{best.side} · {best.strategy}" if best else ""),
                "signal_strength": (best.strength if best else 0.0),
            })
        except Exception as exc:
            r = dict(r)
            r.update({"atr_pct": 0.0, "mom_pct": 0.0, "regime": "—",
                      "signal": "", "signal_strength": 0.0, "error": str(exc)[:80]})
        done.append(r)
    return done
