"""The dip-buying lead, re-measured the way the ENGINE would have to trade it.

edge_exp found "buy the dip in an uptrend" held up on both coin halves (win ~62%, CAGR +10..+14%),
but it was measured with no stop and equal weights. The engine's risk layer is non-negotiable: every
trade has a stop and is sized so that stop costs a fixed share of equity. So this runs the dip rule
through the REAL run_backtest - regime gate, RiskManager sizing, fees and slippage - with a
catastrophe stop, and then through portfolio_exp's shared-slot portfolio on the point-in-time
universe (trailing 30-day median dollar volume >= $1M on the signal day).

  entry   RSI(2) < 5 and close > SMA(100), on the closed bar; fill at the next open
  stop    M x ATR(14), M in {2, 3, 4}; no target (reward_risk 50), no scale-out, no trail
  exits   close back above SMA(5) ("rule exit"), or 10 bars after the fill, or the stop, or the
          engine's regime-flip exit
  slots   10, risk per trade min(1%, 6%/10) = 0.6%

PASS RULE (fixed 2026-09-15, before the first run). Coins are split by crc32(symbol) % 2, as in
edge_exp. Half B has ALREADY been looked at once for this family (edge_exp's exploratory check), so
it is not untouched any more - a pass here is weaker evidence, and the forward paper test stays the
final judge.
  Candidate on half A:
    1. CAGR >= 8%
    2. maximum drawdown <= 25%
    3. win rate >= 55%
    4. >= 1.2 round trips a week
    5. positive in >= 4 of the 5 calendar years 2022-2026
  Of the candidates, the highest CAGR / maximum drawdown is chosen. That M is then checked on half B:
    1. CAGR > 0 and >= 0.5 x its half-A CAGR
    2. maximum drawdown <= 1.5 x its half-A drawdown
    3. positive in >= 3 of 5 years
  Only a pass goes on to the strategy class, the parity replay and a forward paper run.

    .venv/bin/python scripts/dip_exp.py
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

import edge_exp as E  # noqa: E402
import portfolio_exp as P  # noqa: E402

MS = (2, 3, 4)
K = 10


def _dip_strategy(stop_atr: float):
    from trader.strategy.base import Signal, Strategy

    class RsiDip(Strategy):
        name = "rsi_dip"
        regimes = ()

        def evaluate(self, symbol, df, regime):
            if len(df) < 110:
                return None
            cur = df.iloc[-1]
            atr = cur.get("atr14")
            if atr is None or atr != atr:
                return None
            c = df["close"]
            d = c.iloc[-40:].diff()
            up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean().iloc[-1]
            dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean().iloc[-1]
            rsi = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
            if rsi < 5 and float(cur["close"]) > float(c.iloc[-100:].mean()):
                return Signal(symbol, "long", 0.6, self.name, f"dip: RSI2 {rsi:.1f}, above SMA100",
                              stop_distance=stop_atr * float(atr))
            return None

    return RsiDip()


def _above_sma5(window) -> bool:
    c = window["close"]
    return bool(c.iloc[-1] > c.iloc[-5:].mean())


def trades_for(job):
    path_str, m = job
    from trader.backtest.engine import run_backtest
    from trader.config import RiskSettings
    path = pathlib.Path(path_str)
    df = P._frame(path)
    sym = path.stem.split("_")[0]
    if len(df) < 260:
        return sym, m, []
    risk = dataclasses.replace(RiskSettings(), reward_risk=50.0, trail_after_r=0.0, partial_take_r=0.0)
    res = run_backtest(path.stem, df, risk, strategies=[_dip_strategy(m)], allow_short=False,
                       min_confidence=0.55, warmup=200, time_stop_bars=10, close_exit=_above_sma5)
    return sym, m, [{"sig": int(df.index[t.entry_i - 1].timestamp()), "entry": int(df.index[t.entry_i].timestamp()),
                     "exit": int(df.index[t.exit_i].timestamp()), "r": float(t.r), "win": t.pnl > 0,
                     "why": t.reason.rsplit("-> ", 1)[-1]} for t in res.trades]


def main():
    files = sorted(P.ALL.glob("*.json"))
    jobs = [(str(f), m) for m in MS for f in files]
    by: dict = {m: {} for m in MS}
    with ProcessPoolExecutor() as ex:
        for sym, m, tr in ex.map(trades_for, jobs, chunksize=8):
            by[m][sym] = tr
    liq = P.liquidity()

    def run(m, h):
        coins = {s: tr for s, tr in by[m].items() if E.half(s) == h}
        r = P.simulate(coins, liq, "liquid1m", K)
        years = r["n"] / r["per_week"] / 52.18 if r["per_week"] else 1.0
        r["cagr"] = ((1 + r["ret"] / 100) ** (1 / years) - 1) * 100 if years > 0 else 0.0
        r["pos_years"] = sum(1 for y in range(2022, 2027) if r["years"].get(y, -1) > 0)
        whys: dict = {}
        for tr in coins.values():
            for t in tr:
                whys[t["why"]] = whys.get(t["why"], 0) + 1
        r["whys"] = whys
        return r

    def line(label, r):
        yrs = " ".join(f"{r['years'].get(y, 0.0):+6.1f}" for y in range(2022, 2027))
        return (f"{label:22s} CAGR {r['cagr']:+6.1f}%  ret {r['ret']:+7.1f}%  maxDD {r['mdd']:5.1f}%  "
                f"trips/wk {r['per_week']:5.2f}  win {r['win']:5.1f}%  n {r['n']:4d}  years {yrs}")

    print(f"{len(files)} coins, universe liquid >= $1M on the signal day, {K} slots\n\nHALF A")
    cands = []
    for m in MS:
        r = run(m, "A")
        ok = (r["cagr"] >= 8 and r["mdd"] <= 25 and r["win"] >= 55 and r["per_week"] >= 1.2
              and r["pos_years"] >= 4)
        print(line(f"stop {m} ATR", r) + ("  CANDIDATE" if ok else ""))
        print(f"{'':22s} exits per coin backtest (before slots): {r['whys']}")
        if ok:
            cands.append((r["cagr"] / max(r["mdd"], 1e-9), m, r))
    if not cands:
        print("\nno candidate on half A - the dip rule does not survive the engine's stop and sizing")
        return
    _, m, ra = max(cands)
    rb = run(m, "B")
    ok = rb["cagr"] > 0 and rb["cagr"] >= 0.5 * ra["cagr"] and rb["mdd"] <= 1.5 * ra["mdd"] and rb["pos_years"] >= 3
    print(f"\nCHOSEN: stop {m} ATR\nHALF B (already seen once for this family - weaker evidence)\n" + line(f"stop {m} ATR", rb))
    print(f"\nRESULT: {'PASS - go to strategy class, parity replay and forward paper run' if ok else 'FAIL - nothing ships'}")


if __name__ == "__main__":
    main()
