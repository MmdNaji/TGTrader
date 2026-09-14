"""Can the bear-market loss be cut without giving back the rest?

Every exit design measured in winrate_exp loses in the Feb 2022 - mid 2023 window ("third1"),
the shipped one included (V16: -0.19R a trade over 149 trades on real hours). This measures
MARKET-LEVEL entry gates on top of exactly what 0.15.3/0.15.4 ships, on the same real-hours
backtest: 32 coins since 2022, each daily bar walked through its closed hourly bars, both
per-hour orders. Every gate is decided from data closed at the signal bar - nothing later.

PASS RULE (fixed 2026-09-14, before the first run). Against B0 (the shipped system) in the SAME
per-hour order, and it must hold under BOTH orders:
  1. third1 mean R per trade >= B0's + 0.10, and third1 total R >= B0's + 5
  2. whole-history total R >= 0.90 x B0's
  3. whole-history win rate >= B0's - 2 points
  4. held-out coins, total R >= 0.90 x B0's
  5. at least 400 trades
A gate that passes is only RECOMMENDED if at least 2 of the 3 settings in its family pass too - a
lone pass among its neighbours is a fitted peak, not an edge.

CONTROL: B0 must reproduce winrate_exp V27/V28 (812 trades, +93.1R conventional; +84.4R hp).

    .venv/bin/python scripts/bear_exp.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import sys
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
os.environ["TGTRADER_OFFLINE"] = "1"
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

import winrate_exp as W  # noqa: E402

BARS = REPO / ".bars_long"
HOURS = REPO / ".bars_1h"

VARIANTS = {
    "B0 shipped (V16)":        {},
    "B1 BTC > EMA100":         dict(btc=100),
    "B2 BTC > EMA200":         dict(btc=200),
    "B3 BTC > EMA300":         dict(btc=300),
    "B4 breadth >= 40%":       dict(breadth=0.4),
    "B5 breadth >= 50%":       dict(breadth=0.5),
    "B6 breadth >= 60%":       dict(breadth=0.6),
    "B7 coin > EMA200":        dict(own=200),
}
FAMILIES = {"BTC EMA": ["B1 BTC > EMA100", "B2 BTC > EMA200", "B3 BTC > EMA300"],
            "breadth": ["B4 breadth >= 40%", "B5 breadth >= 50%", "B6 breadth >= 60%"]}
ORDERS = ("conv", "hp")
_CACHE: dict = {}


def _frame(path: pathlib.Path):
    import pandas as pd
    rows = json.loads(path.read_text())
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    return df.drop(columns=["ts"])


def _gates() -> dict:
    """Dates on which a new long is ALLOWED, per gate. Only closes up to that date are used, and
    an EMA is not trusted before it has seen as many bars as its span."""
    if "g" in _CACHE:
        return _CACHE["g"]
    import pandas as pd
    closes = {f.stem.split("_")[0]: _frame(f)["close"] for f in sorted(BARS.glob("*.json"))}
    g: dict = {}
    btc = closes["BTC"]
    for n in (100, 200, 300):
        ema = btc.ewm(span=n, adjust=False).mean()
        seen = pd.Series(range(len(btc)), index=btc.index) >= n
        g[("btc", n)] = set(btc.index[(btc > ema) & seen])
    above = {}
    for sym, c in closes.items():
        ema = c.ewm(span=200, adjust=False).mean()
        seen = pd.Series(range(len(c)), index=c.index) >= 200
        above[sym] = (c > ema).astype(float).where(seen)
    table = pd.DataFrame(above)
    frac, count = table.mean(axis=1, skipna=True), table.notna().sum(axis=1)
    for th in (0.4, 0.5, 0.6):
        g[("breadth", th)] = set(table.index[(frac >= th) & (count >= 8)])
    _CACHE["g"] = g
    return g


def _gated(strategies, allowed=None, own=None):
    from trader.strategy.base import Strategy

    class Gated(Strategy):
        def __init__(self, inner):
            self.inner, self.name, self.regimes = inner, inner.name, inner.regimes

        def wants_regime(self, regime):
            return self.inner.wants_regime(regime)

        def evaluate(self, symbol, df, regime):
            sig = self.inner.evaluate(symbol, df, regime)
            if sig is None or sig.side != "long":
                return sig
            if allowed is not None and df.index[-1] not in allowed:
                return None
            if own:
                e = df.iloc[-1].get(f"ema{own}")
                if len(df) < own or e is None or e != e or df.iloc[-1]["close"] <= e:
                    return None
            return sig

    return [Gated(s) for s in strategies]


def run_one(job):
    sym, name, order = job
    from trader.backtest.engine import run_backtest
    from trader.config import RiskSettings
    from trader.strategy.builtin import DonchianBreakout, RsiReversion
    v = VARIANTS[name]
    path = BARS / f"{sym}_USDT.json"
    df = _frame(path)
    # PINNED to what 0.15.3/0.15.4 ships - never read from the repo's defaults
    risk = dataclasses.replace(RiskSettings(), reward_risk=2.5, trail_after_r=0.0,
                               partial_take_r=1.0, partial_take_frac=0.5)
    strats = [RsiReversion(), DonchianBreakout()]
    allowed = None
    if "btc" in v:
        allowed = _gates()[("btc", v["btc"])]
    if "breadth" in v:
        allowed = _gates()[("breadth", v["breadth"])]
    if allowed is not None or v.get("own"):
        strats = _gated(strats, allowed, v.get("own"))
    hours = W._hours_by_day(HOURS / path.name)
    res = run_backtest(sym, df, risk, strategies=strats, allow_short=False, min_confidence=0.55,
                       partial_at_r=1.0, partial_frac=0.5, warmup=200,
                       trail_intrabar=(order == "hp"), intraday=hours)
    return name, order, sym, [(int(df.index[t.entry_i].timestamp()), t.pnl > 0, t.r) for t in res.trades]


def main():
    syms = [s for s in W.MAIN + W.HELD_OUT if (BARS / f"{s}_USDT.json").exists()]
    jobs = [(s, n, o) for n in VARIANTS for o in ORDERS for s in syms]
    by: dict = {}
    with ProcessPoolExecutor() as ex:
        for name, order, sym, tr in ex.map(run_one, jobs):
            by.setdefault((name, order), {})[sym] = tr

    ts = sorted(t[0] for tr in by[("B0 shipped (V16)", "conv")].values() for t in tr)
    lo, span = ts[0], ts[-1] - ts[0] + 1

    def split(bysym):
        flat = [(s, t) for s, tr in bysym.items() for t in tr]
        pick = lambda f: W.summarise([t for s, t in flat if f(s, t)])  # noqa: E731
        out = {"whole": pick(lambda s, t: True), "heldout": pick(lambda s, t: s in W.HELD_OUT)}
        for k in range(2):
            out[f"half{k+1}"] = pick(lambda s, t, k=k: lo + k * span / 2 <= t[0] < lo + (k + 1) * span / 2)
        for k in range(3):
            out[f"third{k+1}"] = pick(lambda s, t, k=k: lo + k * span / 3 <= t[0] < lo + (k + 1) * span / 3)
        return out

    table = {key: split(v) for key, v in by.items()}
    passes: dict = {}
    print(f"{len(syms)} symbols, real hours, shipped exits (rr2.5, half at 1R, no trail, no ema_trend)\n")
    print(f"{'variant':22s} {'ord':4s} {'whole: win% avgR n sumR':>28s} {'third1: avgR n sumR':>22s} "
          f"{'half1':>7s} {'half2':>7s} {'third2':>7s} {'third3':>7s} {'heldout':>8s}  verdict")
    for name in VARIANTS:
        ok_both = True
        for order in ORDERS:
            r, b = table[(name, order)], table[("B0 shipped (V16)", order)]
            w, t1 = r["whole"], r["third1"]
            fails = []
            if not name.startswith("B0"):
                if t1["avg_r"] < b["third1"]["avg_r"] + 0.10:
                    fails.append("third1 avgR")
                if t1["sum_r"] < b["third1"]["sum_r"] + 5:
                    fails.append("third1 sumR")
                if w["sum_r"] < 0.90 * b["whole"]["sum_r"]:
                    fails.append("whole R")
                if w["win"] < b["whole"]["win"] - 2.0:
                    fails.append("win rate")
                if r["heldout"]["sum_r"] < 0.90 * b["heldout"]["sum_r"]:
                    fails.append("held-out R")
                if w["n"] < 400:
                    fails.append("n<400")
            ok_both &= not fails
            verdict = "control" if name.startswith("B0") else ("pass" if not fails else "fail: " + ", ".join(fails))
            print(f"{name:22s} {order:4s} {w['win']:6.1f} {w['avg_r']:+.2f} {w['n']:4d} {w['sum_r']:+7.1f}   "
                  f"{t1['avg_r']:+.2f} {t1['n']:4d} {t1['sum_r']:+7.1f}  "
                  f"{r['half1']['sum_r']:+7.1f} {r['half2']['sum_r']:+7.1f} {r['third2']['sum_r']:+7.1f} "
                  f"{r['third3']['sum_r']:+7.1f} {r['heldout']['sum_r']:+8.1f}  {verdict}")
        passes[name] = ok_both and not name.startswith("B0")

    print("\nPASS under both orders:", [n for n, p in passes.items() if p] or "none")
    for fam, members in FAMILIES.items():
        n_pass = sum(passes[m] for m in members)
        rec = [m for m in members if passes[m]] if n_pass >= 2 else []
        print(f"  family {fam}: {n_pass}/3 pass -> recommended: {rec or 'none (not a flat region)'}")
    own = "B7 coin > EMA200"
    print(f"  {own}: {'pass (single setting, no family)' if passes[own] else 'fail'}")


if __name__ == "__main__":
    main()
