"""Looking for an edge on the WHOLE market - many trades, judged without hindsight.

portfolio_exp showed the shipped breakout system is about breakeven on a point-in-time universe:
its good numbers came partly from 32 coins chosen in 2026. The owner wants many trades that win.
This measures two well-known families that trade often, on every Bybit spot USDT pair, where a coin
is tradeable only on days its trailing 30-day median dollar volume was >= $1M and it had >= 120
daily bars - both known at that day's close.

  S1  weekly momentum rotation: every Monday close, rank the universe by L-day return, hold the top
      K (score > 0) in equal weight, rebalance at the next open. Optional filter: hold nothing while
      BTC closes below its 50-day SMA.
  S2  buy the dip in an uptrend: at the close, RSI(2) < T and close > SMA(100) -> buy at the next
      open, equal weight 1/K, lowest RSI first; exit at the next open after a close above SMA(5), or
      after H days.
Costs: 0.1% fee + 0.05% slippage per side on every fill. Long-only, spot.

ANTI-FITTING DESIGN (fixed 2026-09-15, before the first run)
  Coins are split in two by crc32(symbol) % 2. Every variant is measured on half A only, and one is
  chosen by rule. Half B is then measured ONCE, for that variant only.

  A variant is a candidate on half A if:
    1. CAGR >= 10%
    2. maximum drawdown <= 35%
    3. at least 3 round trips a week
    4. positive in at least 4 of the 5 calendar years 2022-2026
  Among candidates, the highest CAGR / max drawdown is chosen.

  It is VALIDATED on half B if:
    1. CAGR >= 0.5 x its half-A CAGR, and > 0
    2. maximum drawdown <= 1.5 x its half-A drawdown
    3. positive in at least 3 of the 5 calendar years
  Only a validated variant goes to the engine replay and the forward paper test. Survivorship
  remains (delisted coins are missing) and flatters everything here; the forward test is the judge
  that has none.

    .venv/bin/python scripts/edge_exp.py
"""
from __future__ import annotations

import json
import os
import pathlib
import zlib

import numpy as np
import pandas as pd

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALL = pathlib.Path(os.environ.get("TGTRADER_BARS_ALL", REPO / ".bars_all"))
COST = 0.0015
START = pd.Timestamp("2022-01-01")


def load():
    fr = {}
    for f in sorted(ALL.glob("*.json")):
        rows = json.loads(f.read_text())
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="ms")
        fr[f.stem.split("_")[0]] = df
    panel = {k: pd.DataFrame({s: d[k] for s, d in fr.items()}).sort_index()
             for k in ("open", "high", "low", "close", "volume")}
    close = panel["close"]
    liq = (close * panel["volume"]).rolling(30, min_periods=20).median()
    age = close.notna().cumsum()
    panel["univ"] = (liq >= 1_000_000) & (age >= 120)
    return panel


def half(sym: str) -> str:
    return "A" if zlib.crc32(sym.encode()) % 2 == 0 else "B"


def rsi2(close):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def metrics(equity: pd.Series, trips: list[tuple[pd.Timestamp, bool]]):
    eq = equity[equity.index >= START]
    if eq.empty or eq.iloc[0] <= 0:
        return None
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    mdd = float((1 - eq / eq.cummax()).max())
    by_year = {y: float(g.iloc[-1] / g.iloc[0] - 1) for y, g in eq.groupby(eq.index.year) if len(g) > 5}
    tr = [w for d, w in trips if d >= START]
    weeks = max(1.0, (eq.index[-1] - eq.index[0]).days / 7)
    return {"cagr": cagr * 100, "mdd": mdd * 100, "years": {y: v * 100 for y, v in by_year.items()},
            "trips": len(tr), "per_week": len(tr) / weeks, "win": 100 * sum(tr) / len(tr) if tr else 0.0}


def simulate(panel, coins, targets_fn, exit_fn=None, k=5):
    """Generic long-only daily simulator. targets_fn(t) -> ordered list of coins wanted after the
    close of day t (rotation), or exit_fn/entries for position strategies. Fills at the next open."""
    o, c = panel["open"][coins], panel["close"][coins]
    dates = c.index
    cash, units, entry_px, entry_day = 1.0, {}, {}, {}
    equity, trips = [], []
    pending_buy, pending_sell = [], []
    for i, t in enumerate(dates):
        # 1. fills at today's open, decided at yesterday's close
        for s in pending_sell:
            if s in units and o.at[t, s] == o.at[t, s]:
                px = o.at[t, s] * (1 - COST)
                cash += units[s] * px
                trips.append((t, px > entry_px[s] * (1 + COST)))
                del units[s], entry_px[s], entry_day[s]
        pending_sell = []
        if pending_buy:
            value = cash + sum(u * (c.at[dates[i - 1], s] if c.at[dates[i - 1], s] == c.at[dates[i - 1], s] else 0)
                               for s, u in units.items())
            for s in pending_buy:
                if len(units) >= k or s in units or not (o.at[t, s] == o.at[t, s]):
                    continue
                spend = min(cash, value / k)
                if spend <= 0:
                    break
                px = o.at[t, s] * (1 + COST)
                units[s], entry_px[s], entry_day[s] = spend / px, px / (1 + COST), i
                cash -= spend
        pending_buy = []
        # 2. mark to market at the close
        mv = sum(u * c.at[t, s] for s, u in units.items() if c.at[t, s] == c.at[t, s])
        equity.append(cash + mv)
        # 3. decide at this close
        sells, buys = (exit_fn or targets_fn)(t, i, units, entry_day)
        pending_sell, pending_buy = sells, buys
    return pd.Series(equity, index=dates), trips


def s1_momentum(panel, coins, lookback, k, btc_filter):
    c = panel["close"]
    score = c[coins] / c[coins].shift(lookback) - 1
    univ = panel["univ"][coins]
    btc = c["BTC"]
    btc_ok = btc > btc.rolling(50).mean()

    def decide(t, i, units, entry_day):
        if t.weekday() != 0:
            return [], []
        if btc_filter and not bool(btc_ok.get(t, False)):
            return list(units), []
        sc = score.loc[t][univ.loc[t].fillna(False).astype(bool)].dropna()
        top = list(sc[sc > 0].sort_values(ascending=False).index[:k])
        return [s for s in units if s not in top], [s for s in top if s not in units]
    return simulate(panel, coins, decide, k=k)


def s2_dip(panel, coins, thresh, hold, k):
    c = panel["close"][coins]
    r2 = rsi2(c)
    trend = c > c.rolling(100).mean()
    exit_up = c > c.rolling(5).mean()
    univ = panel["univ"][coins]

    def decide(t, i, units, entry_day):
        sells = [s for s in units if bool(exit_up.at[t, s]) or i - entry_day[s] >= hold]
        ok = (r2.loc[t] < thresh) & trend.loc[t].fillna(False) & univ.loc[t].fillna(False)
        cand = r2.loc[t][ok.astype(bool)].dropna().sort_values()
        free = k - (len(units) - len(sells))
        return sells, [s for s in cand.index if s not in units][:max(0, free)]
    return simulate(panel, coins, decide, k=k)


VARIANTS = {}
for L in (14, 28, 56):
    for K in (5, 10):
        for f in (True, False):
            VARIANTS[f"S1 mom L{L} K{K}{' btc' if f else ''}"] = (s1_momentum, dict(lookback=L, k=K, btc_filter=f))
for T in (5, 10, 15):
    for H in (5, 10):
        for K in (5, 10):
            VARIANTS[f"S2 dip rsi<{T} H{H} K{K}"] = (s2_dip, dict(thresh=T, hold=H, k=K))


def line(name, m):
    yrs = " ".join(f"{m['years'].get(y, 0.0):+6.1f}" for y in range(2022, 2027))
    return (f"{name:24s} CAGR {m['cagr']:+6.1f}%  maxDD {m['mdd']:5.1f}%  trips/wk {m['per_week']:5.2f}  "
            f"win {m['win']:5.1f}%  years {yrs}")


def main():
    panel = load()
    coins = [s for s in panel["close"].columns]
    a = [s for s in coins if half(s) == "A"]
    b = [s for s in coins if half(s) == "B"]
    print(f"{len(coins)} coins: half A {len(a)}, half B {len(b)}\n\nHALF A (discovery)")
    cands = []
    for name, (fn, kw) in VARIANTS.items():
        eq, trips = fn(panel, a, **kw)
        m = metrics(eq, trips)
        if m is None:
            print(f"{name:24s} no data"); continue
        pos_years = sum(1 for y in range(2022, 2027) if m["years"].get(y, -1) > 0)
        ok = m["cagr"] >= 10 and m["mdd"] <= 35 and m["per_week"] >= 3 and pos_years >= 4
        if ok:
            cands.append((m["cagr"] / max(m["mdd"], 1e-9), name, m))
        print(line(name, m) + ("  CANDIDATE" if ok else ""))
    if not cands:
        print("\nno candidate on half A - nothing is validated, nothing ships")
        return
    _, name, ma = max(cands)
    fn, kw = VARIANTS[name]
    eq, trips = fn(panel, b, **kw)
    mb = metrics(eq, trips)
    pos_years = sum(1 for y in range(2022, 2027) if mb["years"].get(y, -1) > 0)
    ok = mb["cagr"] > 0 and mb["cagr"] >= 0.5 * ma["cagr"] and mb["mdd"] <= 1.5 * ma["mdd"] and pos_years >= 3
    print(f"\nCHOSEN on half A: {name}\nHALF B (validation, run once)\n" + line(name, mb))
    print(f"\nVALIDATION: {'PASS - goes to engine replay and forward paper test' if ok else 'FAIL - nothing ships'}")


if __name__ == "__main__":
    main()
