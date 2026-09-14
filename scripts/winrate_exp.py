"""Can the win rate go up without the money going down?

The owner's goal, in his words: not every trade has to win, but MOST trades should close in
profit. Win rate on its own is the easiest number in trading to fake - a tight target and a
wide stop wins nine times in ten and still loses the account - so every variant here is scored
on win rate AND on R per trade after fees, and the pass rule was written BEFORE any result was
seen. It is not to be edited after the fact to let a variant through.

PASS RULE (fixed 2026-09-14, before the first run)
    For every split - whole history, two halves, three thirds (by calendar date of entry), the
    16 "main" symbols and the 16 held-out symbols:
      1. win rate >= baseline's win rate in that split + 3 points
      2. mean R per trade > 0 (after fees and slippage)
    and over the whole history:
      3. total R >= 0.6 x the baseline's total R
      4. at least 100 trades
    A variant that fails any one of these does not ship, however good its headline looks.

Long-only, daily bars, the real `run_backtest` (real RiskManager sizing, real fee gate, real
confidence gate of the "normal" preset, regime-flip exit, stop cooldown). Each symbol is run
once over its whole history and trades are bucketed by entry date, so every split uses the
same warmed-up indicators.

    .venv/bin/python scripts/winrate_exp.py [--bars-dir .bars_long]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(REPO))
os.environ["TGTRADER_OFFLINE"] = "1"
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

MAIN = ["AAVE", "ADA", "ARB", "BNB", "BTC", "DOGE", "ETH", "LINK", "LTC", "MNT", "NEAR", "SOL",
        "SUI", "UNI", "WLD", "XRP"]
HELD_OUT = ["APEX", "APT", "AVAX", "BCH", "BONK", "DOT", "GRAM", "HBAR", "INJ", "JTO", "KAS",
            "PEPE", "POL", "STETH", "TRX", "XLM"]


def _trend_filter(strategies):
    """Only take a long when the long-term trend agrees: close above EMA200 and EMA50 above
    EMA200. Wraps the real strategies rather than editing them, so the baseline is untouched."""
    from trader.strategy.base import Strategy

    class Filtered(Strategy):
        def __init__(self, inner):
            self.inner, self.name, self.regimes = inner, inner.name, inner.regimes

        def wants_regime(self, regime):
            return self.inner.wants_regime(regime)

        def evaluate(self, symbol, df, regime):
            sig = self.inner.evaluate(symbol, df, regime)
            if sig is None or sig.side != "long":
                return sig
            cur = df.iloc[-1]
            e200 = cur.get("ema200")
            if e200 is None or e200 != e200:
                return None
            return sig if (cur["close"] > e200 and cur["ema50"] > e200) else None

    return [Filtered(s) for s in strategies]


VARIANTS = {
    "V0 baseline (rr2.5 trail1)": dict(),
    "V1 half at 1R + BE":         dict(partial_at_r=1.0),
    "V2 rr1.5":                   dict(rr=1.5),
    "V3 rr1.5 + half at 1R":      dict(rr=1.5, partial_at_r=1.0),
    "V4 trend filter":            dict(filt=True),
    "V5 trend + half at 1R":      dict(filt=True, partial_at_r=1.0),
    "V6 trend + rr1.5 + half 1R": dict(filt=True, rr=1.5, partial_at_r=1.0),
    # Round 2, added after round 1 and judged by the SAME rule. Round 1: nothing passed, and
    # the only failing cell for the best variant (V3) was third1 - Feb 2022 to mid 2023, the
    # bear market - where EVERY variant, the baseline included, loses. So round 2 asks whether
    # standing aside while Bitcoin itself is in trend_down removes that loss.
    "V7 V3 + BTC leader":         dict(rr=1.5, partial_at_r=1.0, leader=True),
    "V8 baseline + BTC leader":   dict(leader=True),
    # Round 3, same rule. Round 2's per-strategy breakdown: ema_trend lost in every variant
    # (whole-history win 31.7%, -0.25R over 161 trades) while donchian_breakout carried all of
    # the profit, and rsi_reversion never fires long-only at the 0.55 gate. Dropping a rule
    # because it lost in-sample is how overfitting starts, so the per-split table below must
    # show ema_trend losing in the halves, the thirds AND the held-out symbols before this is
    # believed.
    "V9 V3 without ema_trend":    dict(rr=1.5, partial_at_r=1.0, drop=("ema_trend",)),
    "V10 V7 without ema_trend":   dict(rr=1.5, partial_at_r=1.0, leader=True, drop=("ema_trend",)),
    # Round 4, same rule. The Windows session found the live engine trails the stop off the
    # LIVE price every loop, while the backtest trails on the close. Daily bars cannot show the
    # path inside a bar, so these assume the worst order (high first, then the low). A
    # conclusion only stands if it survives both this and the close-trail above.
    "V11 baseline, intrabar trail": dict(intra=True),
    "V12 V3, intrabar trail":       dict(rr=1.5, partial_at_r=1.0, intra=True),
    "V13 V9, intrabar trail":       dict(rr=1.5, partial_at_r=1.0, drop=("ema_trend",), intra=True),
    # Round 5, same rule. Round 4: under the intrabar trail EVERYTHING collapsed - baseline
    # +53R -> -65R, V9 +133R -> +0.7R - so the trailing stop, not the entries, decides whether
    # this system makes money, and the answer depends on a bar path daily data cannot show.
    # With trailing OFF that ambiguity disappears (the stop only ever moves to break-even at
    # the scale-out), so these are measurable honestly on daily bars.
    "V14 baseline, no trail":       dict(trail=0.0),
    "V15 V9, no trail":             dict(rr=1.5, partial_at_r=1.0, drop=("ema_trend",), trail=0.0),
    "V16 V9 rr2.5, no trail":       dict(partial_at_r=1.0, drop=("ema_trend",), trail=0.0),
    # Round 6, same rule. Round 5: V15 (V9 with trailing OFF) = +132.5R, 53.0% - the same as V9
    # with a close-trail - so trailing adds nothing to this exit design and switching it off
    # removes the path ambiguity that sank round 4. One ambiguity is left: after the scale-out
    # the break-even stop can be hit on the SAME bar. These take that worst case too.
    "V17 V15, pessimistic bar path": dict(rr=1.5, partial_at_r=1.0, drop=("ema_trend",), trail=0.0,
                                          intra=True),
    "V18 V14, pessimistic bar path": dict(trail=0.0, intra=True),
}

_LEADER: dict = {}


def _leader_regimes(bdir: pathlib.Path):
    """Bitcoin's regime on every daily bar, computed once per worker process."""
    if "btc" not in _LEADER:
        import pandas as pd
        from trader.market.indicators import enrich
        from trader.strategy.regime import detect_regime
        rows = json.loads((bdir / "BTC_USDT.json").read_text())
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="ms")
        data = enrich(df.drop(columns=["ts"]))
        _LEADER["btc"] = pd.Series([detect_regime(data.iloc[: i + 1]) for i in range(len(data))],
                                   index=data.index)
    return _LEADER["btc"]


def run_one(job):
    sym, path, vname = job
    import pandas as pd
    from trader.backtest.engine import run_backtest
    from trader.config import RiskSettings
    from trader.strategy.builtin import DEFAULT_STRATEGIES

    v = VARIANTS[vname]
    rows = json.loads(pathlib.Path(path).read_text())
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    df = df.drop(columns=["ts"])
    risk = RiskSettings()
    if "rr" in v:
        risk = dataclasses.replace(risk, reward_risk=v["rr"])
    if "trail" in v:
        risk = dataclasses.replace(risk, trail_after_r=v["trail"])
    strats = _trend_filter(DEFAULT_STRATEGIES) if v.get("filt") else list(DEFAULT_STRATEGIES)
    strats = [s for s in strats if s.name not in v.get("drop", ())]
    leader = _leader_regimes(pathlib.Path(path).parent) if v.get("leader") else None
    res = run_backtest(sym, df, risk, strategies=strats, allow_short=False, min_confidence=0.55,
                       partial_at_r=v.get("partial_at_r", 0.0), warmup=200, leader_regimes=leader,
                       trail_intrabar=v.get("intra", False))
    return vname, sym, [(int(df.index[t.entry_i].timestamp()), t.pnl > 0, t.r, t.strategy)
                        for t in res.trades]


def summarise(trades):
    n = len(trades)
    if not n:
        return dict(n=0, win=0.0, avg_r=0.0, sum_r=0.0)
    return dict(n=n, win=100.0 * sum(1 for t in trades if t[1]) / n,
                avg_r=sum(t[2] for t in trades) / n, sum_r=sum(t[2] for t in trades))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars-dir", default=str(REPO / ".bars_long"))
    args = ap.parse_args()
    bdir = pathlib.Path(args.bars_dir)
    syms = [s for s in MAIN + HELD_OUT if (bdir / f"{s}_USDT.json").exists()]
    jobs = [(s, str(bdir / f"{s}_USDT.json"), v) for v in VARIANTS for s in syms]
    by = {v: {} for v in VARIANTS}
    with ProcessPoolExecutor() as ex:
        for vname, sym, tr in ex.map(run_one, jobs):
            by[vname][sym] = tr

    all_ts = sorted(t[0] for tr in by["V0 baseline (rr2.5 trail1)"].values() for t in tr)
    lo, hi = all_ts[0], all_ts[-1]
    span = hi - lo + 1

    def splits(bysym):
        flat = [(s, t) for s, tr in bysym.items() for t in tr]
        def sel(f):
            return [t for s, t in flat if f(s, t)]
        out = {"whole": sel(lambda s, t: True)}
        for k in range(2):
            out[f"half{k+1}"] = sel(lambda s, t, k=k: lo + k * span / 2 <= t[0] < lo + (k + 1) * span / 2)
        for k in range(3):
            out[f"third{k+1}"] = sel(lambda s, t, k=k: lo + k * span / 3 <= t[0] < lo + (k + 1) * span / 3)
        out["main"] = sel(lambda s, t: s in MAIN)
        out["heldout"] = sel(lambda s, t: s in HELD_OUT)
        return {k: summarise(v) for k, v in out.items()}

    table = {v: splits(by[v]) for v in VARIANTS}
    base = table["V0 baseline (rr2.5 trail1)"]
    keys = list(base.keys())
    print(f"{len(syms)} symbols, entries {pd_date(lo)} .. {pd_date(hi)}\n")
    print(f"{'variant':30s} " + " ".join(f"{k:>18s}" for k in keys) + "   verdict")
    print(" " * 31 + " ".join(f"{'win% avgR  n':>18s}" for _ in keys))
    for v, row in table.items():
        cells = " ".join(f"{row[k]['win']:5.1f} {row[k]['avg_r']:+.2f} {row[k]['n']:4d}" for k in keys)
        fails = []
        if v != "V0 baseline (rr2.5 trail1)":
            for k in keys:
                if row[k]["win"] < base[k]["win"] + 3:
                    fails.append(f"win@{k}")
                if row[k]["avg_r"] <= 0:
                    fails.append(f"R<=0@{k}")
            if row["whole"]["sum_r"] < 0.6 * base["whole"]["sum_r"]:
                fails.append("totalR")
            if row["whole"]["n"] < 100:
                fails.append("n<100")
        verdict = "-" if v.startswith("V0") else ("PASS" if not fails else "fail: " + ",".join(fails[:4]))
        print(f"{v:30s} {cells}   sumR {row['whole']['sum_r']:+7.1f}  {verdict}")

    # Which strategy carries the result, and which one loses in the bad window.
    print("\nper strategy, every split (win% avgR n):")
    for v in ("V0 baseline (rr2.5 trail1)", "V3 rr1.5 + half at 1R"):
        names = sorted({t[3] for tr in by[v].values() for t in tr})
        for name in names:
            row = splits({s: [t for t in tr if t[3] == name] for s, tr in by[v].items()})
            cells = " ".join(f"{row[k]['win']:5.1f} {row[k]['avg_r']:+.2f} {row[k]['n']:4d}" for k in keys)
            print(f"  {v[:12]:12s} {name:18s} {cells}")


def pd_date(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
