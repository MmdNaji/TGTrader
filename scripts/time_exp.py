"""Does a time stop cut the bear-market loss without giving back the rest?

Market trend gates made the bear window WORSE per trade (scripts/bear_exp.py). The owner chose to
try a time stop next: a breakout that has not reached its 1R scale-out N daily bars after the fill
is treated as failed and closed at that bar's close. Everything else is exactly what 0.15.3/0.15.4
ships, on the real-hours backtest, both per-hour orders.

Definition, identical on both sides (a clarification recorded while stage 1 was running; it matches
the code that ran): the fill bar is bar 0; the time stop fires at the close of the N-th closed bar
after it (i - entry_i >= N); a trade that has already scaled out at 1R is exempt. The live engine
sees bar N close and exits at bar N+1's first price - the same shape as a regime-flip exit, so a
parity replay shows the same price with the exit labelled one bar later.

Every round on the same history is another look at it, so this runs in TWO stages, both fixed here
on 2026-09-14 before the first run.

STAGE 1 - the 32 coins (.bars_long / .bars_1h). Against T0 (the shipped system) in the SAME order,
holding under BOTH orders:
  1. third1 mean R per trade >= T0's + 0.10, and third1 total R >= T0's + 5
  2. whole-history total R >= 0.90 x T0's
  3. whole-history win rate >= T0's - 2 points
  4. held-out coins total R >= 0.90 x T0's
  5. at least 400 trades
An N that passes is RECOMMENDED only if at least one neighbouring N (next shorter or longer in
3, 5, 8, 13, 21) also passes. If several are recommended, the one with the highest whole-history
total R under the pessimistic order is taken forward - chosen by that rule, not by eye.

STAGE 2 - fresh coins, run ONCE, only for the N stage 1 took forward. Universe chosen by rule:
the 16 Bybit spot USDT pairs with the highest 24h quote volume at fetch time that are not among the
32, are not stablecoins or wrapped/staked tokens, and have a first daily bar before 2022-03-01.
They are fetched only after stage 1 has picked N. The chosen N must, under BOTH orders:
  1. whole-history total R >= T0's on the same coins
  2. if T0 has >= 30 trades in third1 there: third1 mean R >= T0's + 0.05
If stage 2 fails, nothing ships, whatever stage 1 said.

    .venv/bin/python scripts/time_exp.py                 # stage 1
    .venv/bin/python scripts/time_exp.py --stage2 N      # stage 2, fresh coins, after fetching them
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
sys.path.insert(0, str(REPO / "scripts"))
os.environ["TGTRADER_OFFLINE"] = "1"
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

import winrate_exp as W  # noqa: E402

NS = (3, 5, 8, 13, 21)
ORDERS = ("conv", "hp")


def _frame(path: pathlib.Path):
    import pandas as pd
    rows = json.loads(path.read_text())
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms")
    return df.drop(columns=["ts"])


def run_one(job):
    daily, hourly, n, order = job
    from trader.backtest.engine import run_backtest
    from trader.config import RiskSettings
    from trader.strategy.builtin import DonchianBreakout, RsiReversion
    daily, hourly = pathlib.Path(daily), pathlib.Path(hourly)
    df = _frame(daily)
    # PINNED to what 0.15.3/0.15.4 ships - never read from the repo's defaults
    risk = dataclasses.replace(RiskSettings(), reward_risk=2.5, trail_after_r=0.0,
                               partial_take_r=1.0, partial_take_frac=0.5)
    res = run_backtest(daily.stem, df, risk, strategies=[RsiReversion(), DonchianBreakout()],
                       allow_short=False, min_confidence=0.55, partial_at_r=1.0, partial_frac=0.5,
                       warmup=200, trail_intrabar=(order == "hp"), intraday=W._hours_by_day(hourly),
                       time_stop_bars=n)
    sym = daily.stem.split("_")[0]
    return n, order, sym, [(int(df.index[t.entry_i].timestamp()), t.pnl > 0, t.r) for t in res.trades]


def measure(bars: pathlib.Path, hours: pathlib.Path, ns):
    syms = sorted(p.stem.split("_")[0] for p in bars.glob("*.json") if (hours / p.name).exists())
    jobs = [(str(bars / f"{s}_USDT.json"), str(hours / f"{s}_USDT.json"), n, o)
            for n in ns for o in ORDERS for s in syms]
    by: dict = {}
    with ProcessPoolExecutor() as ex:
        for n, order, sym, tr in ex.map(run_one, jobs):
            by.setdefault((n, order), {})[sym] = tr
    return syms, by


def splits(by, heldout=()):
    ts = sorted(t[0] for tr in by[(0, "conv")].values() for t in tr)
    lo, span = ts[0], ts[-1] - ts[0] + 1

    def one(bysym):
        flat = [(s, t) for s, tr in bysym.items() for t in tr]
        pick = lambda f: W.summarise([t for s, t in flat if f(s, t)])  # noqa: E731
        out = {"whole": pick(lambda s, t: True), "heldout": pick(lambda s, t: s in heldout)}
        for k in range(2):
            out[f"half{k+1}"] = pick(lambda s, t, k=k: lo + k * span / 2 <= t[0] < lo + (k + 1) * span / 2)
        for k in range(3):
            out[f"third{k+1}"] = pick(lambda s, t, k=k: lo + k * span / 3 <= t[0] < lo + (k + 1) * span / 3)
        return out

    return {key: one(v) for key, v in by.items()}


def row(label, r):
    w, t1 = r["whole"], r["third1"]
    return (f"{label:10s} {w['win']:6.1f} {w['avg_r']:+.2f} {w['n']:4d} {w['sum_r']:+7.1f}   "
            f"{t1['avg_r']:+.2f} {t1['n']:4d} {t1['sum_r']:+7.1f}  {r['half1']['sum_r']:+7.1f} "
            f"{r['half2']['sum_r']:+7.1f} {r['third2']['sum_r']:+7.1f} {r['third3']['sum_r']:+7.1f} "
            f"{r['heldout']['sum_r']:+8.1f}")


def stage1():
    syms, by = measure(REPO / ".bars_long", REPO / ".bars_1h", (0,) + NS)
    table = splits(by, heldout=W.HELD_OUT)
    print(f"STAGE 1: {len(syms)} coins, real hours, shipped exits + time stop after N bars\n")
    print(f"{'N / order':10s} {'whole: win% avgR n sumR':>28s} {'third1: avgR n sumR':>22s} "
          f"{'half1':>7s} {'half2':>7s} {'third2':>7s} {'third3':>7s} {'heldout':>8s}  verdict")
    passes = {}
    for n in (0,) + NS:
        ok_both = True
        for order in ORDERS:
            r, b = table[(n, order)], table[(0, order)]
            fails = []
            if n:
                if r["third1"]["avg_r"] < b["third1"]["avg_r"] + 0.10:
                    fails.append("third1 avgR")
                if r["third1"]["sum_r"] < b["third1"]["sum_r"] + 5:
                    fails.append("third1 sumR")
                if r["whole"]["sum_r"] < 0.90 * b["whole"]["sum_r"]:
                    fails.append("whole R")
                if r["whole"]["win"] < b["whole"]["win"] - 2.0:
                    fails.append("win rate")
                if r["heldout"]["sum_r"] < 0.90 * b["heldout"]["sum_r"]:
                    fails.append("held-out R")
                if r["whole"]["n"] < 400:
                    fails.append("n<400")
            ok_both &= not fails
            verdict = "control" if not n else ("pass" if not fails else "fail: " + ", ".join(fails))
            print(row(f"{'T0' if not n else f'N={n}'} {order}", r) + f"  {verdict}")
        passes[n] = bool(n) and ok_both
    rec = []
    for k, n in enumerate(NS):
        nb = [NS[j] for j in (k - 1, k + 1) if 0 <= j < len(NS)]
        if passes[n] and any(passes[m] for m in nb):
            rec.append(n)
    print("\npass under both orders:", [n for n in NS if passes[n]] or "none")
    print("recommended (a neighbouring N also passes):", rec or "none")
    if rec:
        best = max(rec, key=lambda n: table[(n, "hp")]["whole"]["sum_r"])
        print(f"TAKEN FORWARD to stage 2: N={best} (highest pessimistic-order total R among recommended)")
    else:
        print("nothing goes to stage 2")


def stage2(n):
    bars, hours = REPO / ".bars_fresh", REPO / ".bars_fresh_1h"
    # The exact coins and when they were picked are written by the fetch, and required here: the
    # selection rule depends on today's volumes, so nobody should be able to quietly re-pick them.
    manifest = bars / "MANIFEST.json"
    if not manifest.exists():
        raise SystemExit(f"{manifest} is missing - the fresh coins must be fetched with their selection record")
    m = json.loads(manifest.read_text())
    syms, by = measure(bars, hours, (0, n))
    if sorted(syms) != sorted(m["coins"]):
        raise SystemExit(f"coins on disk {syms} differ from the manifest {m['coins']}")
    table = splits(by)
    print(f"STAGE 2: {len(syms)} FRESH coins, N={n}, run once")
    print(f"  selected at {m['selected_at']} by: {m['rule']}")
    print(f"  coins: {' '.join(m['coins'])}\n")
    ok = True
    for order in ORDERS:
        r, b = table[(n, order)], table[(0, order)]
        print(row(f"T0 {order}", b))
        print(row(f"N={n} {order}", r))
        fails = []
        if r["whole"]["sum_r"] < b["whole"]["sum_r"]:
            fails.append("whole R below shipped")
        if b["third1"]["n"] >= 30 and r["third1"]["avg_r"] < b["third1"]["avg_r"] + 0.05:
            fails.append("third1 avgR")
        print(f"   {order}: {'pass' if not fails else 'fail: ' + ', '.join(fails)}"
              f"{'' if b['third1']['n'] >= 30 else ' (third1 has < 30 trades: criterion 2 not applied)'}")
        ok &= not fails
    print(f"\nSTAGE 2 VERDICT: {'PASS' if ok else 'FAIL - nothing ships'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage2", type=int, default=0, metavar="N")
    a = ap.parse_args()
    stage2(a.stage2) if a.stage2 else stage1()
