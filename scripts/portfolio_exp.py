"""More trades from a wider market, without losing what makes the trades good?

The owner's ask: "take the whole market - hundreds or thousands of coins - analyse all of them and
open correct, successful positions; more trades." On one coin the shipped daily system trades about
once every two months, so the lever for frequency is the NUMBER OF COINS, not a shorter timeframe
(1h/4h/scalp all lost in every combination measured). But a per-coin backtest gives every coin its
own full balance and always looks better than a portfolio: here cash, open-position slots and the
total open risk are SHARED, and when more coins signal than there are slots only some are taken.

HOW
  1. Every Bybit spot USDT pair (.bars_all) runs the real run_backtest with exactly the exits that
     0.15.3/0.15.4 ship (rr 2.5, half at 1R, no trail, rsi_reversion + donchian_breakout, the
     normal 0.55 gate, long-only). Daily path - the real-hours check comes after, for the winner.
  2. A day-by-day portfolio takes those trades: a trade may open only if the coin is in the
     universe ON ITS SIGNAL DAY (liquidity known at that close, never later), a slot is free (a slot
     frees only after the exit day), and the risk per trade keeps total open risk <= 6% of equity.
     When several coins signal on one day, the most liquid is taken first (a fixed rule).
  3. Equity compounds at each exit: equity *= 1 + R x risk_per_trade.

UNIVERSES (all decided from data available on the signal day)
  pinned32   the 32 coins every earlier experiment used
  top50      the 50 coins with the highest trailing 30-day median dollar volume that day
  top100     the same, top 100
  liquid1m   every coin whose trailing 30-day median dollar volume is >= $1M that day
SLOTS  K = 4, 6, 8, risk per trade = min(1%, 6% / K)

CHOICE RULE (fixed 2026-09-15, before the first run). Baseline = pinned32 with K = 4. A config is a
CANDIDATE only if, against the baseline:
  1. trades per week >= 2 x baseline
  2. win rate >= baseline - 3 points
  3. total return >= baseline
  4. maximum drawdown <= 1.5 x baseline
  5. the 2022 calendar-year return >= baseline's 2022 return - 5 points
Among candidates the highest total return / maximum drawdown is recommended. It then has to pass a
real-hours check before anything reaches the app.

CAVEAT: .bars_all holds the coins listed TODAY, so coins that died since 2021 are missing - that
flatters every wide universe, most of all in 2022. The liquidity filter limits it (a coin must have
traded real volume on the day), it does not remove it.

    .venv/bin/python scripts/portfolio_exp.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
os.environ["TGTRADER_OFFLINE"] = "1"
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

# Overridable so a small copied sample can dry-run the unchanged script; a worker pool cannot be
# started from a script piped in on stdin.
ALL = pathlib.Path(os.environ.get("TGTRADER_BARS_ALL", REPO / ".bars_all"))
PINNED32 = {"AAVE", "ADA", "ARB", "BNB", "BTC", "DOGE", "ETH", "LINK", "LTC", "MNT", "NEAR", "SOL", "SUI",
            "UNI", "WLD", "XRP", "APEX", "APT", "AVAX", "BCH", "BONK", "DOT", "GRAM", "HBAR", "INJ", "JTO",
            "KAS", "PEPE", "POL", "STETH", "TRX", "XLM"}
DAY = 86_400


def _frame(path: pathlib.Path):
    import pandas as pd
    rows = json.loads(path.read_text())
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    return df.drop(columns=["ts"])


def trades_for(path_str: str):
    """Every trade the shipped system takes on one coin, with the signal day it was decided on."""
    from trader.backtest.engine import run_backtest
    from trader.config import RiskSettings
    from trader.strategy.builtin import DonchianBreakout, RsiReversion
    path = pathlib.Path(path_str)
    df = _frame(path)
    if len(df) < 260:
        return path.stem.split("_")[0], []
    risk = dataclasses.replace(RiskSettings(), reward_risk=2.5, trail_after_r=0.0,
                               partial_take_r=1.0, partial_take_frac=0.5)
    res = run_backtest(path.stem, df, risk, strategies=[RsiReversion(), DonchianBreakout()],
                       allow_short=False, min_confidence=0.55, partial_at_r=1.0, partial_frac=0.5,
                       warmup=200)
    out = []
    for t in res.trades:
        sig = int(df.index[t.entry_i - 1].timestamp())      # the closed bar the signal came from
        out.append({"sig": sig, "entry": int(df.index[t.entry_i].timestamp()),
                    "exit": int(df.index[t.exit_i].timestamp()), "r": float(t.r), "win": t.pnl > 0})
    return path.stem.split("_")[0], out


def liquidity():
    """Trailing 30-day median dollar volume per coin per day, using only days up to that one."""
    import pandas as pd
    cols = {}
    for f in sorted(ALL.glob("*.json")):
        df = _frame(f)
        cols[f.stem.split("_")[0]] = (df["close"] * df["volume"]).rolling(30, min_periods=20).median()
    return pd.DataFrame(cols)


def simulate(trades_by_coin, liq, universe, k):
    import pandas as pd
    risk = min(0.01, 0.06 / k)
    ranks = liq.rank(axis=1, ascending=False)
    cands = []
    for coin, trs in trades_by_coin.items():
        for t in trs:
            day = pd.Timestamp(t["sig"], unit="s")
            if day not in liq.index or coin not in liq.columns:
                continue
            v, rk = liq.at[day, coin], ranks.at[day, coin]
            if v != v:                                   # no liquidity history yet
                continue
            ok = {"pinned32": coin in PINNED32, "top50": rk <= 50, "top100": rk <= 100,
                  "liquid1m": v >= 1_000_000}[universe]
            if ok:
                cands.append((t["entry"], -v, coin, t))
    cands.sort(key=lambda x: (x[0], x[1]))
    open_exits: list[int] = []
    taken = []
    for entry, _negv, coin, t in cands:
        # Keep the positions still OPEN on the entry day. A slot frees only after its exit day, so an
        # exit on the entry day itself still holds it. (The first version kept the CLOSED ones, so
        # finished trades filled every slot for good - the 40-coin dry run took 7 of 175 signals.)
        open_exits = [e for e in open_exits if e >= entry]
        if len(open_exits) >= k:
            continue
        open_exits.append(t["exit"])
        taken.append(t)
    taken.sort(key=lambda t: t["exit"])
    eq, peak, mdd = 1.0, 1.0, 0.0
    year_start: dict[int, float] = {}
    year_end: dict[int, float] = {}
    for t in taken:
        y = pd.Timestamp(t["exit"], unit="s").year
        year_start.setdefault(y, eq)
        eq *= 1 + t["r"] * risk
        year_end[y] = eq
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    n = len(taken)
    first = min((t["entry"] for t in taken), default=0)
    last = max((t["exit"] for t in taken), default=1)
    weeks = max(1.0, (last - first) / (7 * DAY))
    years = {y: (year_end[y] / year_start[y] - 1) * 100 for y in year_end}
    return {"n": n, "per_week": n / weeks, "win": 100 * sum(t["win"] for t in taken) / n if n else 0.0,
            "ret": (eq - 1) * 100, "mdd": mdd * 100, "sum_r": sum(t["r"] for t in taken),
            "years": years, "risk": risk, "cands": len(cands)}


def main():
    files = sorted(ALL.glob("*.json"))
    print(f"{len(files)} coins in .bars_all", flush=True)
    with ProcessPoolExecutor() as ex:
        trades_by_coin = dict(ex.map(trades_for, [str(f) for f in files], chunksize=4))
    liq = liquidity()
    print(f"per-coin trades: {sum(len(v) for v in trades_by_coin.values())} over "
          f"{sum(1 for v in trades_by_coin.values() if v)} coins\n")
    results = {}
    for u in ("pinned32", "top50", "top100", "liquid1m"):
        for k in (4, 6, 8):
            results[(u, k)] = simulate(trades_by_coin, liq, u, k)
    base = results[("pinned32", 4)]
    yrs = sorted({y for r in results.values() for y in r["years"]})
    print(f"{'universe':9s} {'K':>2s} {'risk':>5s} {'signals':>7s} {'taken':>5s} {'/week':>6s} {'win%':>5s} "
          f"{'return%':>8s} {'maxDD%':>6s} {'sumR':>6s}  " + " ".join(f"{y:>6d}" for y in yrs) + "  verdict")
    cands = []
    for (u, k), r in results.items():
        fails = []
        if (u, k) != ("pinned32", 4):
            if r["per_week"] < 2 * base["per_week"]:
                fails.append("frequency")
            if r["win"] < base["win"] - 3:
                fails.append("win rate")
            if r["ret"] < base["ret"]:
                fails.append("return")
            if r["mdd"] > 1.5 * base["mdd"]:
                fails.append("drawdown")
            if r["years"].get(2022, 0.0) < base["years"].get(2022, 0.0) - 5:
                fails.append("2022")
        verdict = "baseline" if (u, k) == ("pinned32", 4) else ("CANDIDATE" if not fails else "no: " + ", ".join(fails))
        if verdict == "CANDIDATE":
            cands.append((r["ret"] / max(r["mdd"], 1e-9), u, k))
        print(f"{u:9s} {k:2d} {r['risk'] * 100:4.2f}% {r['cands']:7d} {r['n']:5d} {r['per_week']:6.2f} {r['win']:5.1f} "
              f"{r['ret']:+8.1f} {r['mdd']:6.1f} {r['sum_r']:+6.1f}  "
              + " ".join(f"{r['years'].get(y, 0.0):+6.1f}" for y in yrs) + f"  {verdict}")
    if cands:
        best = max(cands)
        print(f"\nRECOMMENDED (highest return / max drawdown among candidates): {best[1]} K={best[2]} "
              f"(ratio {best[0]:.2f}) - still needs the real-hours check")
    else:
        print("\nno candidate: a wider universe did not buy more trades on these terms")


if __name__ == "__main__":
    main()
