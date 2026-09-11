"""Run the real engine over real bars, then AUDIT what it did.

Not a backtest. The backtest has its own simplified loop; this drives `Engine.loop_once()` -
the same code the live bot runs - against real exchange data replayed bar by bar, and then
cross-checks the engine's own bookkeeping against the broker's.

The audit is the point. A backtest that returns a number cannot tell you that the journal and
the account disagree about how much is held, or that a position was left with nobody watching
its stop, or that R was computed against the wrong quantity. Those are the failures that cost
money in the live account and never show up in a return figure.

    .venv/bin/python scripts/paper_session.py [--bars 1000] [--symbols 12] [--partial 0]

It writes to a throwaway TGTRADER_HOME and never touches a real account.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time
import traceback
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BARS_DIR = pathlib.Path(os.environ.get("TGTRADER_BARS", REPO / ".bars"))


def fetch_bars(want: int, bars: int, timeframe: str = "1d") -> None:
    """Fill the cache from the exchange. Runs BEFORE the session goes offline.

    The first version defaulted BARS_DIR to the scratchpad of the machine it was written on, so
    it worked there and nowhere else: the Windows session got "no symbol has 750 bars" and no
    way to fix it, because the session sets TGTRADER_OFFLINE and cannot fetch. A script that
    only runs where it was written is not a tool, it is a note to self.
    """
    import ccxt
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    have = [f for f in BARS_DIR.glob("*.json")
            if len(json.loads(f.read_text())) >= bars]
    if len(have) >= want:
        return
    print(f"cache has {len(have)} symbols with {bars}+ bars, fetching more...")
    ex = ccxt.bybit({"enableRateLimit": True, "timeout": 20000,
                     "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}})
    ex.load_markets()
    tk = ex.fetch_tickers()
    stable = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD", "USD1", "RLUSD", "USDP", "PYUSD"}
    pairs = sorted(((tk[sy].get("quoteVolume") or 0.0), sy) for sy in tk
                   if sy.endswith("/USDT") and (ex.markets.get(sy) or {}).get("spot")
                   and (ex.markets.get(sy) or {}).get("active")
                   and sy.split("/")[0] not in stable)[::-1]
    got = len(have)
    for _vol, sym in pairs:
        if got >= want:
            break
        f = BARS_DIR / (sym.replace("/", "_") + ".json")
        if f.exists() and len(json.loads(f.read_text())) >= bars:
            continue
        try:
            rows = ex.fetch_ohlcv(sym, timeframe, limit=max(bars, 1000))
        except Exception as exc:
            print(f"  skip {sym}: {type(exc).__name__}")
            continue
        if len(rows) >= bars:
            f.write_text(json.dumps(rows))
            got += 1
            print(f"  {sym}: {len(rows)} bars")
        time.sleep(0.05)
    print(f"cache ready: {got} symbols in {BARS_DIR}")


class Replay:
    """A MarketData stand-in that hands out real bars one at a time.

    `candles` returns everything up to the current bar and `price` the last close, which is
    what the live engine gets from a running exchange. Every symbol advances together, so a
    position opened on one day is managed on the next for every symbol at once - the same
    interleaving the live loop has.
    """

    def __init__(self, frames: dict, start: int, chaos: float = 0.0, seed: int = 7):
        self.frames = frames
        self.i = start
        # An exchange that misbehaves, which is what a live one does. A session where every
        # request succeeds tests the happy path and nothing else; the failures that cost money
        # are the ones where candles stop arriving while a position is open and its stop is
        # nobody's job any more.
        self.chaos = chaos
        self._rng = __import__("random").Random(seed)
        # A symbol that goes completely dark from a given bar - halted, delisted, or just
        # dropped by the data source. This is the one that matters: a position is open, no
        # price arrives, and its stop is nobody's job any more. The engine is supposed to say
        # UNMANAGED out loud rather than fail quietly.
        self.blackout: str | None = None
        self.blackout_from = 10 ** 9
        self.active_source = "replay"
        self.notice = None
        self.abort = lambda: False
        self.is_kcex = False
        self.calls = Counter()

    def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
        self.calls["candles"] += 1
        if symbol == self.blackout and self.i >= self.blackout_from:
            self.calls["blacked_out"] += 1
            raise RuntimeError(f"{symbol}: simulated blackout (no candles)")
        if self.chaos and self._rng.random() < self.chaos:
            self.calls["candles_failed"] += 1
            raise RuntimeError(f"{symbol}: simulated candle failure")
        df = self.frames[symbol]
        return df.iloc[: self.i + 1].tail(limit)

    def price(self, symbol):
        self.calls["price"] += 1
        if symbol == self.blackout and self.i >= self.blackout_from:
            self.calls["blacked_out"] += 1
            raise RuntimeError(f"{symbol}: simulated blackout (no price)")
        if self.chaos and self._rng.random() < self.chaos * 0.5:
            self.calls["price_failed"] += 1
            raise RuntimeError(f"{symbol}: simulated ticker failure")
        return float(self.frames[symbol]["close"].iloc[self.i])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=1000)
    ap.add_argument("--symbols", type=int, default=12)
    ap.add_argument("--partial", type=float, default=0.0)
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--chaos", type=float, default=0.0,
                    help="fraction of exchange calls that fail, 0..1")
    ap.add_argument("--blackout", type=int, default=0,
                    help="bar at which one symbol goes completely dark while holding a position")
    args = ap.parse_args()

    # Fetch BEFORE going offline: the session must not reach the network, but building its
    # data must be possible on a fresh clone.
    try:
        fetch_bars(args.symbols, args.bars)
    except Exception as exc:
        print(f"could not fill the bar cache ({type(exc).__name__}: {exc}); "
              f"using whatever is in {BARS_DIR}")

    home = tempfile.mkdtemp(prefix="tgtrader-session-")
    os.environ["TGTRADER_HOME"] = home
    os.environ["TGTRADER_OFFLINE"] = "1"        # the replay is the only data source
    os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

    from trader.config import Settings
    from trader.db import Database
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker
    from trader.market.data import ohlcv_to_frame

    # Only symbols with the FULL window. One short listing used to drag the whole session down
    # to its own length - eleven symbols and a thousand bars ran a hundred and six days and
    # took three trades, which is not a test of anything.
    frames, short = {}, []
    for f in sorted(BARS_DIR.glob("*.json")):
        df = ohlcv_to_frame(json.loads(f.read_text()))
        if len(df) >= args.bars:
            frames[f.stem.replace("_", "/")] = df.tail(args.bars)
        else:
            short.append((f.stem.replace("_", "/"), len(df)))
        if len(frames) >= args.symbols:
            break
    if not frames:
        print(f"no symbol in {BARS_DIR} has {args.bars} bars")
        return 2
    if short:
        print(f"skipped (too little history): {', '.join(f'{s} {k}' for s, k in short)}")
    n = args.bars
    syms = sorted(frames)

    s = Settings()
    s.mode = "paper"
    s.symbols = syms
    s.use_llm_for_decisions = False              # the rules alone, so this is repeatable
    s.timeframe = "1d"
    s.paper_start_balance = args.balance
    s.risk.capital_limit = args.balance
    s.risk.partial_take_r = args.partial

    db = Database(pathlib.Path(home) / "session.db")
    broker = PaperBroker(args.balance, allow_short=False)
    broker.reset(args.balance)
    eng = Engine(s, db, broker=broker)
    market = Replay(frames, start=250, chaos=args.chaos)
    eng.market = market

    # THE REPLAY'S OWN CLOCK, and this is not a detail. Every row is stamped through
    # `db.clock`, and the risk layer measures a day through the same clock, so with the wall
    # clock in place a two-year replay runs inside one real UTC day: `day_start()` never moves,
    # `pnl_since(day_start)` returns EVERY trade the session ever closed, and the daily loss
    # cap becomes a lifetime one. Measured before this was wired: the cap tripped on the third
    # closed trade of eighty and refused 121 entries over the remaining two years. The session
    # was reporting what a bot does with entries switched off for most of its life.
    index = frames[syms[0]].index
    db.clock = lambda: float(index[min(market.i, len(index) - 1)].timestamp())

    logs: list[str] = []
    eng.log = lambda m, lvl="info": logs.append(f"[{lvl}] {m}")

    print(f"{len(syms)} symbols x {n} bars  ({frames[syms[0]].index[250].date()} .. "
          f"{frames[syms[0]].index[-1].date()})")
    print(f"balance {args.balance}  scale-out {'off' if not args.partial else f'{args.partial}R'}"
          + (f"  chaos {args.chaos:.0%} of exchange calls fail" if args.chaos else ""))

    crashes = []
    t0 = time.time()
    blacked = None
    for i in range(250, n):
        market.i = i
        # At or AFTER the requested bar, the first moment a position is actually open. The
        # first version wanted that exact bar and the loop starts at 250, so --blackout 200
        # silently did nothing: the Windows session ran it and got numbers identical to the run
        # without it, which is the signature of a switch that is not in the path.
        if args.blackout and i >= max(args.blackout, 250) and market.blackout is None:
            live = db.open_trades("paper")
            if live:
                blacked = live[0]["symbol"]
                market.blackout, market.blackout_from = blacked, i
                print(f"  bar {i}: {blacked} goes dark while a position is open on it")
        try:
            eng.loop_once()
        except Exception:
            crashes.append(f"bar {i} ({frames[syms[0]].index[i].date()}):\n"
                           + traceback.format_exc())
    took = time.time() - t0

    # close whatever is still open at the last price, the way stopping the engine does
    eng.close_all("end of session")
    print(f"ran {n - 250} bars in {took:.1f}s  "
          f"({market.calls['candles']} candle reads, {market.calls['price']} price reads"
          + (f", {market.calls['candles_failed']} + {market.calls['price_failed']} failed"
             if args.chaos else "") + ")\n")

    # ---------------------------------------------------------------- audit
    problems: list[str] = []
    closed = db.closed_trades("paper", limit=100000)
    still_open = db.open_trades("paper")
    equity = broker.equity({sym: float(frames[sym]["close"].iloc[n - 1]) for sym in syms})

    if crashes:
        problems.append(f"{len(crashes)} loop crashes")
    if still_open:
        problems.append(f"{len(still_open)} trades still open after close_all")
    if broker._positions:
        problems.append(f"broker still holds {list(broker._positions)} after close_all")

    # the journal's realised P&L has to explain the account's cash
    booked = sum(float(t["pnl"] or 0) for t in closed)
    drift = (broker.cash() - args.balance) - booked
    if abs(drift) > max(0.05, abs(booked) * 0.01):
        problems.append(f"journal says {booked:+.4f} realised, account moved "
                        f"{broker.cash() - args.balance:+.4f} - off by {drift:+.4f}")

    # R has to be consistent with P&L and the risk the trade was opened with
    bad_r = 0
    for t in closed:
        init = t["init_stop"] or t["stop_price"]
        qty = t["part_qty"] or t["qty"]
        if not init or not qty:
            continue
        risk = abs(float(t["entry_price"]) - float(init)) * float(qty)
        if risk <= 0 or t["r_multiple"] is None:
            continue
        want = float(t["pnl"]) / risk
        if abs(want - float(t["r_multiple"])) > 0.02:
            bad_r += 1
    if bad_r:
        problems.append(f"{bad_r} trades whose R does not match their P&L and original risk")

    # --- the invariants the risk layer exists to hold. A return figure cannot show any of
    # these, and every one of them is a way to lose real money in a live account.
    opens = db.query("SELECT * FROM trades WHERE mode='paper' ORDER BY opened_at", ())
    events = []
    for t in opens:
        events.append((float(t["opened_at"]), 1, t["symbol"],
                       float(t["qty"]) * float(t["entry_price"])))
        if t["closed_at"]:
            events.append((float(t["closed_at"]), -1, t["symbol"], 0.0))
    events.sort()
    held, peak_open, doubled, over_frac = set(), 0, 0, 0
    cap = args.balance * s.risk.max_position_frac
    for _ts, kind, sym, notional in events:
        if kind == 1:
            if sym in held:
                doubled += 1
            held.add(sym)
            peak_open = max(peak_open, len(held))
            if notional > cap * 1.02:
                over_frac += 1
        else:
            held.discard(sym)
    if peak_open > s.risk.max_open_positions:
        problems.append(f"held {peak_open} positions at once, the cap is "
                        f"{s.risk.max_open_positions}")
    if doubled:
        problems.append(f"{doubled} times a second position was opened on a symbol already held")
    if over_frac:
        problems.append(f"{over_frac} positions were larger than max_position_frac "
                        f"({cap:.2f}) allows")

    # every trade must carry the analysis the owner is shown
    missing = [int(t["id"]) for t in closed if not db.trade_analysis(int(t["id"]))]
    if missing:
        problems.append(f"{len(missing)} closed trades have no stored analysis (e.g. #{missing[0]})")

    curve = db.equity_curve("paper", limit=100000)
    if len(curve) < (n - 250) * 0.5:
        problems.append(f"only {len(curve)} equity points for {n - 250} bars")
    if broker.cash() < 0:
        problems.append(f"cash went negative: {broker.cash():.4f}")

    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    rs = [float(t["r_multiple"]) for t in closed if t["r_multiple"] is not None]
    # exits come from the DECISION rows - trades.reason holds why it was ENTERED
    exits = Counter(d["reason"] for d in db.query(
        "SELECT reason FROM decisions WHERE action='close'", ()))
    warn = Counter(l.split("]")[0].strip("[") for l in logs if not l.startswith("[info]"))

    print(f"trades {len(closed)}   win {len(wins)/len(closed)*100 if closed else 0:.1f}%   "
          f"net {booked:+.2f}   equity {equity:.2f}   "
          f"avg R {sum(rs)/len(rs) if rs else 0:+.3f}")
    print(f"exits: {dict(exits.most_common(8))}")
    print(f"most open at once {peak_open}/{s.risk.max_open_positions}   "
          f"biggest position cap {cap:.2f}   equity points {len(curve)}   "
          f"cash {broker.cash():.2f}")
    print(f"log levels other than info: {dict(warn) or 'none'}")
    if warn:
        kinds = Counter()
        for l in logs:
            if l.startswith("[info]"):
                continue
            body = l.split("] ", 1)[-1]
            kinds[body.split(":")[0][:58] if ":" in body else body[:58]] += 1
        for k, c in kinds.most_common(5):
            print(f"    {c:>5}x  {k}")
    # The daily cap must behave like a DAILY one. If it fires and never releases, the run
    # measured a bot with entries switched off, whatever its return figure says.
    cap_holds = db.query("SELECT ts FROM decisions WHERE action='hold' AND source='risk'"
                         " AND reason LIKE 'daily loss%' ORDER BY ts", ())
    if cap_holds:
        days = len({int(float(r["ts"]) // 86400) for r in cap_holds})
        span = (float(cap_holds[-1]["ts"]) - float(cap_holds[0]["ts"])) / 86400.0
        print(f"daily loss cap: {len(cap_holds)} entries refused across {days} separate days "
              f"(first to last: {span:.0f} days)")
        if days == 1 and span > 2:
            problems.append(f"the daily loss cap refused {len(cap_holds)} entries spread over "
                            f"{span:.0f} days but they all fell on ONE day by the clock - the "
                            f"replay clock is not reaching the risk layer, so the cap never "
                            f"reset and this run measured a bot that had stopped entering")

    if args.blackout and not blacked:
        problems.append(f"--blackout {args.blackout} never fired - no position was open at or "
                        f"after bar {max(args.blackout, 250)}, so this run tested nothing "
                        f"a plain run does not")
    if blacked:
        # Read the engine's STATE, not the log. The UNMANAGED warning is gated on five minutes
        # of WALL-CLOCK time, which is right for a live bot and unreachable in a replay that
        # crosses a year in twenty seconds. What has to be true either way is that the engine
        # knows the symbol has gone dark.
        tracked = blacked in getattr(eng, "_unmanaged", {})
        unmanaged_logs = [l for l in logs if "UNMANAGED" in l and blacked in l]
        if not tracked and not unmanaged_logs:
            problems.append(f"{blacked} went dark holding a position and the engine never "
                            f"noticed - no UNMANAGED state and no warning")
        else:
            print(f"blackout: {blacked} tracked as unmanaged={tracked}, "
                  f"{len(unmanaged_logs)} warnings, "
                  f"{'still open at the end' if any(t['symbol'] == blacked for t in still_open) else 'closed out'}")
    for kind in ("UNMANAGED", "order failed", "close failed", "analysis not stored",
                 "scale-out", "broker holds no position"):
        hits = [l for l in logs if kind in l]
        if hits:
            print(f"  {kind}: {len(hits)}  e.g. {hits[0][:110]}")

    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print("  ✗ " + p)
        for c in crashes[:2]:
            print("\n" + c)
    else:
        print("audit clean: journal and account agree, nothing left open, every R checks out")
    print(f"\nsession data: {home}")
    db.close()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
