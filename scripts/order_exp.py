"""When four setups want three slots, who gets the money - and does the answer matter?

Over 16 symbols and 750 daily bars, 343 entries were refused because all four slots were
taken. So on most bars the engine is not asking "is there a setup", it is asking "which of
these". Until this was measured the answer was the order of the symbol LIST, which on a sorted
list means AAVE is asked before XRP for no reason anyone chose.

This replays the SAME bars with only `Engine._rank_candidates` swapped, across the whole
window, two halves and three thirds. Two randoms are in there as a CONTROL: without them there
is no way to tell an ordering that is better from an ordering that got lucky.

    .venv/bin/python scripts/order_exp.py [--bars 1000] [--symbols 16] [--seeds 4]

It needs the bar cache that `paper_session.py` builds (`.bars/` in the repo, or $TGTRADER_BARS)
and it never touches the network or a real account. It is deterministic: the same cache gives
the same table on any machine, and two machines disagreeing is itself a finding.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import tempfile

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(REPO))
# The same lesson `paper_session.py` had to learn: a default pointing at the author's own
# scratchpad makes a script that runs where it was written and nowhere else.
BARS_DIR = pathlib.Path(os.environ.get("TGTRADER_BARS", REPO / ".bars"))

os.environ["TGTRADER_OFFLINE"] = "1"
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"


class Replay:
    """Bars one at a time, no chaos - this experiment is about ordering and nothing else."""

    active_source = "replay"
    notice = None
    is_kcex = False
    abort = staticmethod(lambda: False)

    def __init__(self, frames, i):
        self.frames, self.i = frames, i

    def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
        return self.frames[symbol].iloc[: self.i + 1].tail(limit)

    def price(self, symbol):
        return float(self.frames[symbol]["close"].iloc[self.i])


def run(frames, lo, hi, order, balance=1000.0, partial=0.0) -> dict:
    home = tempfile.mkdtemp(prefix="ord-")
    os.environ["TGTRADER_HOME"] = home
    from trader.config import Settings
    from trader.db import Database
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker

    s = Settings()
    s.mode = "paper"
    s.symbols = sorted(frames)
    s.use_llm_for_decisions = False
    s.timeframe = "1d"
    s.paper_start_balance = balance
    s.risk.capital_limit = balance
    s.risk.partial_take_r = partial

    db = Database(pathlib.Path(home) / "s.db")
    broker = PaperBroker(balance, allow_short=False)
    broker.reset(balance)
    eng = Engine(s, db, broker=broker)
    eng.log = lambda m, lvl="info": None
    eng._rank_candidates = order

    mk = Replay(frames, lo)
    eng.market = mk
    # The replay's own clock. Without it the daily loss cap becomes a LIFETIME cap - the whole
    # run happens inside one real UTC day, so `pnl_since(day_start)` returns every trade the
    # session ever closed. Measured before this was wired: it tripped on the third closed trade
    # of eighty and refused 121 entries for the rest of the run.
    index = frames[sorted(frames)[0]].index
    db.clock = lambda: float(index[min(mk.i, len(index) - 1)].timestamp())

    for i in range(lo, hi):
        mk.i = i
        try:
            eng.loop_once()
        except Exception:
            pass
    eng.close_all("end")

    closed = db.closed_trades("paper", limit=100000)
    n = len(closed)
    wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
    rs = [float(t["r_multiple"]) for t in closed if t["r_multiple"] is not None]
    out = {"n": n, "win": wins / n * 100 if n else 0.0,
           "net": sum(float(t["pnl"] or 0) for t in closed),
           "R": sum(rs) if rs else 0.0}
    db.close()
    return out


# ---------------------------------------------------------------- the orderings
def by_list(c):
    """What the engine did before any of this: the symbol list's own order."""
    return sorted(c, key=lambda x: x["symbol"])


def _atr_pct(x):
    atr = x["snap"].get("atr14") or 0.0
    return (atr / x["price"]) if x["price"] else 0.0


def by_calm(c):
    return sorted(c, key=_atr_pct)


def by_wild(c):
    return sorted(c, key=lambda x: -_atr_pct(x))


def by_adx(c):
    return sorted(c, key=lambda x: -(x["snap"].get("adx14") or 0.0))


def mk_rotate():
    """No preference at all, just no PERMANENT one: the list, turned one place each bar.

    This is the control that matters most. If a fixed alphabetical order loses to this, the
    problem was never which coin is better - it was that the same names were always asked
    first, so with four slots and sixteen symbols the other twelve were starved.
    """
    turn = {"n": 0}

    def f(c):
        c = sorted(c, key=lambda x: x["symbol"])
        turn["n"] += 1
        k = turn["n"] % max(1, len(c))
        return c[k:] + c[:k]
    return f


def mk_random(seed):
    def f(c):
        r = random.Random(seed * 1000 + len(c))
        c = sorted(c, key=lambda x: x["symbol"])
        r.shuffle(c)
        return c
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=1000)
    ap.add_argument("--symbols", type=int, default=16)
    ap.add_argument("--seeds", type=int, default=4, help="how many random controls")
    args = ap.parse_args()

    from trader.market.data import ohlcv_to_frame
    frames = {}
    for f in sorted(BARS_DIR.glob("*.json")):
        df = ohlcv_to_frame(json.loads(f.read_text()))
        if len(df) >= args.bars:
            frames[f.stem.replace("_", "/")] = df.tail(args.bars)
        if len(frames) >= args.symbols:
            break
    if len(frames) < 2:
        print(f"need at least 2 symbols with {args.bars} bars in {BARS_DIR}; "
              f"run scripts/paper_session.py once to fill it")
        return 2

    n = args.bars
    windows = {
        "whole": (250, n),
        "half 1": (250, (250 + n) // 2), "half 2": ((250 + n) // 2, n),
        "third 1": (250, 250 + (n - 250) // 3),
        "third 2": (250 + (n - 250) // 3, 250 + 2 * (n - 250) // 3),
        "third 3": (250 + 2 * (n - 250) // 3, n),
    }
    orders = ([("list", by_list), ("calm", by_calm), ("wild", by_wild), ("adx", by_adx),
               ("rotate", mk_rotate())]
              + [(f"rand{i}", mk_random(i)) for i in range(1, args.seeds + 1)])

    print(f"{len(frames)} symbols, {args.bars} bars: {', '.join(sorted(frames))}\n")
    table: dict = {}
    for label, (lo, hi) in windows.items():
        for name, fn in orders:
            table[(label, name)] = run(frames, lo, hi, fn)
        row = "".join(f"{table[(label, o)]['net']:>+8.0f}/{table[(label, o)]['n']:<4}"
                      for o, _ in orders)
        print(f"{label:<9}" + row, flush=True)

    print("\n(net $ / trades)          " + "".join(f"{o:>12}" for o, _ in orders))
    print()
    for label in windows:
        print(f"{label:<9}" + "".join(f"{table[(label, o)]['win']:>11.1f}%"
                                      for o, _ in orders))
    print("(win rate)                " + "".join(f"{o:>12}" for o, _ in orders))

    # The control is the point: an ordering only beat chance if it is outside the random spread.
    print()
    rands = [f"rand{i}" for i in range(1, args.seeds + 1)]
    for label in windows:
        lo_r = min(table[(label, r)]["net"] for r in rands)
        hi_r = max(table[(label, r)]["net"] for r in rands)
        verdicts = []
        for o, _ in orders:
            if o in rands:
                continue
            v = table[(label, o)]["net"]
            verdicts.append(f"{o} " + ("above" if v > hi_r else "below" if v < lo_r else "inside"))
        print(f"{label:<9} random spread {lo_r:+.0f}..{hi_r:+.0f}   " + ", ".join(verdicts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
