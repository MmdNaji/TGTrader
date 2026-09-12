"""Sweep the whole crypto market on the server, so the app does not have to.

WHY THIS EXISTS. Reading a chart costs one request per coin. The market is 390 active USDT
pairs on bybit and about 235 of them are reachable for a $1,000 account, so a full sweep is
~235 requests - roughly eighty seconds of solid traffic, every sweep, on a home connection that
the owner has already reported dropping. The app was therefore looking at the most liquid 40,
which is ten percent of the market and exactly the "famous coins" complaint.

This box has a stable connection and is idle. It does the sweep once for everyone and publishes
one small JSON; the app makes ONE request instead of 235. That also means a dropped connection
on the owner's side costs a refresh, not a blind spot - the sweep already happened.

WHAT IT IS NOT: it never places an order, never sees an API key, and never decides anything.
The keys live on the owner's machine and the trading decisions stay there. This publishes
FACTS - prices, indicators, which rules fired, and the headlines - and nothing else.

Memory is deliberately tight. This box also runs the live SMM panel, MariaDB, two Redis
instances and a VPN in 4 GB, so symbols are processed one at a time and nothing accumulates
except the small result row.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, "/root/trader")
os.environ.setdefault("TGTRADER_HOME", "/var/lib/tgtrader-feed/home")

OUT = pathlib.Path(os.environ.get("TGTRADER_FEED_OUT", "/var/lib/tgtrader-dl/market.json"))
STATE = pathlib.Path("/var/lib/tgtrader-feed")
FEED_VERSION = 1

# The sweep is sized for the smallest account we expect to serve, because a floor computed for a
# big account would hide coins a small one is allowed to trade. The app filters down from here
# with its OWN floor - publishing more than a given account may use is free, hiding it is not.
SMALL_POSITION_CAP = 100.0


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def build_market() -> dict[str, Any]:
    from trader.config import Settings
    from trader.market.data import MarketData
    from trader.market import scanner
    from trader.market.indicators import enrich, snapshot
    from trader.strategy.regime import detect_regime
    from trader.strategy.builtin import evaluate_all, DEFAULT_STRATEGIES

    s = Settings()
    s.exchange.exchange_id = "bybit"
    s.data_source = "bybit"
    s.proxy_mode = "none"
    s.timeframe = "1d"
    md = MarketData(s)

    floor = scanner.volume_floor(SMALL_POSITION_CAP)
    t0 = time.time()
    rows = scanner.scan(s, md, limit=1000, min_volume=floor)
    log(f"tickers: {len(rows)} pairs clear ${floor:,.0f}/day  ({time.time() - t0:.1f}s)")

    coins: list[dict[str, Any]] = []
    failed = 0
    for i, r in enumerate(rows, 1):
        try:
            df = enrich(md.candles(r["symbol"], "1d", limit=200))
            if len(df) < 60:
                raise RuntimeError("not enough history")
            closed = df.iloc[:-1] if len(df) > 80 else df
            regime = detect_regime(closed)
            sigs = evaluate_all(r["symbol"], closed, regime, DEFAULT_STRATEGIES)
            best = max(sigs, key=lambda x: x.strength) if sigs else None
            snap = snapshot(closed)
            px = float(df["close"].iloc[-1])
            atr = float(df["atr14"].iloc[-1] or 0.0)
            coins.append({
                "symbol": r["symbol"],
                "price": px,
                "volume_usd": r["volume_usd"],
                "range_pct": r["range_pct"],
                "change_pct": r["change_pct"],
                "atr_pct": (atr / px * 100.0) if px else 0.0,
                "mom_pct": (float(df["close"].iloc[-1] / df["close"].iloc[-31] - 1) * 100.0
                            if len(df) > 31 else 0.0),
                "regime": regime,
                "signal": (f"{best.side} · {best.strategy}" if best else ""),
                "signal_side": (best.side if best else ""),
                "signal_strength": (best.strength if best else 0.0),
                "signal_reason": (best.reason if best else ""),
                "stop_distance": (float(best.stop_distance or 0.0) if best else 0.0),
                # what the app needs to draw the projection without re-fetching candles
                "rsi14": snap.get("rsi14"),
                "adx14": snap.get("adx14"),
                "ema20": snap.get("ema20"),
                "ema50": snap.get("ema50"),
                "atr14": atr,
            })
        except Exception as exc:
            failed += 1
            if failed <= 3:
                log(f"  {r['symbol']}: {type(exc).__name__}: {exc}")
        finally:
            df = closed = None          # one symbol at a time; nothing accumulates
        if i % 50 == 0:
            log(f"  charts {i}/{len(rows)}")
    log(f"charts: {len(coins)} read, {failed} failed  ({time.time() - t0:.1f}s total)")
    return {"floor_usd": floor, "universe": len(rows), "coins": coins}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "home").mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        market = build_market()
    except Exception:
        log("sweep failed:\n" + traceback.format_exc())
        return 1

    # Headlines, attached to the coins they are actually about. A failure here must not cost
    # the sweep - the chart data is the part the bot cannot trade without.
    try:
        sys.path.insert(0, "/root/trader-server")
        import news as _news
        feed = _news.build([c["symbol"] for c in market["coins"]])
        log(f"news: {len(feed['items'])} headlines, {len(feed['by_coin'])} coins mentioned")
    except Exception as exc:
        log(f"news failed ({type(exc).__name__}: {exc}) - publishing the market without it")
        feed = {"items": [], "by_coin": {}, "at": 0.0}

    payload = {
        "feed_version": FEED_VERSION,
        "at": time.time(),
        "took_s": round(time.time() - started, 1),
        "source": "bybit",
        "news": feed,
        **market,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)                       # atomic: a reader never sees half a file
    log(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {len(payload['coins'])} coins)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
