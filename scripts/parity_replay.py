"""Release gate: does the LIVE engine trade exactly what run_backtest reports, on the same prices?

Drives the real Engine.loop_once() - model off, long-only, one symbol at a time, a throwaway
TGTRADER_HOME per run - through every day of cached daily bars, walking each day as a PRICE PATH,
and pairs its trades one to one with run_backtest over the same bars. Run it for any change to
exits, sizing, entries or strategies before a release.

WHY A PATH, AND WHY THIS ONE
  * Engine.loop_once takes its price from the LAST CANDLE's close (engine.py, loop_once), not from
    market.price(). Live, that candle is the bar still forming, so its close IS the live price. A
    replay that returns the finished bar shows the engine the day's final close at every step,
    whatever price() says: it enters a bar late, and checks and fills stops only at closes. So the
    last candle here is built as the forming bar - its open, the high/low reached SO FAR, and the
    current path price as the close.
  * --path hourly walks each day through its REAL closed hourly bars (open -> extremes -> close
    per hour). --path daily walks an assumed open -> extremes -> close for the whole day, which
    leaves the intraday order a guess; use it only when hourly bars are unavailable.
  * Inside an hour (or a day) the order of the high and the low is unknowable. --order
    conventional is open->low->high->close for an up bar; trail-first is the reverse, the worst case
    for a long's trailing and break-even stops. Run both.
  * Every price at which the open position acts - stop, target, the scale-out mark, break-even -
    is inserted into the path when a leg crosses it, so the engine meets it NEAR its level the way
    a one-minute live loop does. Without that a coarse path fills stops at the step after the
    level (-1.20R for a -1.02R stop). The scale-out mark is inserted a hair (1e-9) past its level:
    the engine's own comparison used to refuse a price exactly on it by one float ulp.
  * With no trailing stop, nothing the engine does inside a day depends on a price that is not a
    level, so loop_once is called only at the day's first price, at each level crossing and at
    the day's last price ("sparse"). With a trail every step can move the stop, so every step is
    called. PARITY_DENSE=1 forces every step; sparse and dense must give identical trades.

CRITERIA (fixed 2026-09-14, before the first hourly run; the 0.15.3 gate passed all of them with
113/113 trades paired, the same exit reason on every one, and a largest |R difference| of 0.000000)
  H1 control - a config with no scale-out and no trail (V19) is order-independent: its engine
     total R under the two orders differs by <= 1.0R, and is within max(2R, 5%) of the backtest.
     If H1 fails, nothing else is interpreted: the instrument is broken.
  H2 resolution - for the gated config, the two orders give engine total R within max(2R, 10%) of
     each other; otherwise the hourly data does not resolve it and it is reported as unresolved.
  H3 cross-check - engine total R within max(2R, 5%) of the hourly backtest, and >= 90% of trades
     pair by (symbol, entry bar) both ways.
  H4 - zero engine exceptions; days falling back to the daily path reported per symbol.

HOW TO RUN (against the server's pinned data)
  python scripts/parity_replay.py --fetch-mirror http://91.107.163.109:40002
      downloads SHA256SUMS and every bars_1d/bars_1h file it lists into --data, keeping only files
      whose hash matches
  python scripts/parity_replay.py --variant shipped --path hourly --order conventional
  python scripts/parity_replay.py --variant shipped --path hourly --order trail-first
  python scripts/parity_replay.py --variant V19     --path hourly --order conventional   (H1)
  python scripts/parity_replay.py --pair-only --variant shipped --path hourly --order conventional
      re-pairs the engine trades saved by an earlier run against run_backtest from the CURRENT
      checkout, without replaying the engine (minutes instead of an hour)

Engine trades are written to --out as <commit>_<variant>_<path>_<order>_<SYMBOL>.json.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["TGTRADER_NO_AUTOUPDATE"] = "1"

SYMBOLS = ["AAVE/USDT", "ADA/USDT", "ARB/USDT", "BNB/USDT", "BTC/USDT", "DOGE/USDT", "ETH/USDT", "LINK/USDT"]
DAY_MS = 86_400_000


# ---------------------------------------------------------------- data
def fetch_mirror(base: str, data: pathlib.Path) -> int:
    """Everything SHA256SUMS lists, verified. A file whose hash does not match is not kept."""
    base = base.rstrip("/")
    sums = urllib.request.urlopen(base + "/SHA256SUMS", timeout=60).read().decode()
    bad = 0
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        want, name = parts[0].lower(), parts[1].lstrip("*").lstrip("./")
        dest = data / name
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == want:
            print(f"  {name}: cached, ok")
            continue
        body = urllib.request.urlopen(f"{base}/{name}", timeout=300).read()
        if hashlib.sha256(body).hexdigest() != want:
            print(f"  {name}: SHA256 MISMATCH - not kept")
            bad += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        print(f"  {name}: ok ({len(body):,} bytes)")
    return bad


def load_daily(data: pathlib.Path, sym: str, bars: int):
    from trader.market.data import ohlcv_to_frame
    f = data / "bars_1d" / (sym.replace("/", "_") + ".json")
    if not f.exists():
        raise SystemExit(f"{f} is missing - run with --fetch-mirror first")
    df = ohlcv_to_frame(json.loads(f.read_text()))
    if len(df) < bars:
        raise SystemExit(f"{f} has {len(df)} daily bars, {bars} needed")
    return df.tail(bars)


def load_hourly(data: pathlib.Path, sym: str) -> list:
    f = data / "bars_1h" / (sym.replace("/", "_") + ".json")
    if not f.exists():
        # a run labelled hourly must be hourly: no silent fallback for a whole symbol
        raise SystemExit(f"--path hourly: {f} is missing")
    return sorted(json.loads(f.read_text()), key=lambda r: r[0])


def hours_of_day(rows: list, keys: list, day_ms: int) -> list:
    a = bisect.bisect_left(keys, day_ms)
    b = bisect.bisect_left(keys, day_ms + DAY_MS)
    return rows[a:b]


def intraday_for_backtest(rows: list, index) -> dict:
    """run_backtest's intraday= input: {daily bar timestamp: [(high, low, close) per closed hour]},
    the same rule as scripts/winrate_exp.py (a day with fewer than 20 hours is left out).

    Keyed in the FRAME'S OWN timezone. run_backtest looks a day up with data.index[i], and
    ohlcv_to_frame builds a UTC-aware index while winrate_exp's frames are naive - keyed naive here,
    not one of 750 days matched and the backtest silently walked the daily path instead (ETH read
    12 trades / +3.78R against the real-hours +5.49R). So the keys follow the index, and a run
    labelled hourly in which no day matches refuses to go on."""
    import pandas as pd
    tz = getattr(index, "tz", None)
    days: dict = {}
    for ms, _o, hh, ll, cc, *_ in rows:
        day = pd.Timestamp((ms // DAY_MS) * DAY_MS, unit="ms", tz="UTC")
        day = day if tz is not None else day.tz_localize(None)
        days.setdefault(day, []).append((float(hh), float(ll), float(cc)))
    out = {d: hs for d, hs in days.items() if len(hs) >= 20}
    if not any(t in out for t in index):
        raise SystemExit("hourly bars match no daily bar of the frame - timestamps do not line up")
    return out


def _legs(corners: list[float], steps: int, out: list[float]) -> None:
    if not out:
        out.append(corners[0])
    for x, y in zip(corners, corners[1:]):
        out += [x + (y - x) * k / steps for k in range(1, steps + 1)]


def day_path(row, order: str, steps: int, hours: list | None) -> tuple[list[float], bool]:
    """(prices, walked_real_hours). Fewer than 20 hours falls back to the daily path."""
    out: list[float] = []
    if hours and len(hours) >= 20:
        for _ms, o, h, l, c, *_ in hours:
            o, h, l, c = float(o), float(h), float(l), float(c)
            up = (c >= o) != (order == "trail-first")
            _legs([o, l, h, c] if up else [o, h, l, c], steps, out)
        return out, True
    o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
    up = (c >= o) != (order == "trail-first")
    _legs([o, l, h, c] if up else [o, h, l, c], steps, out)
    return out, False


# ---------------------------------------------------------------- configs
def variant(name: str):
    """(settings, strategies). 'shipped' is exactly what this checkout ships; the named ones pin
    every exit field so a comparison never depends on what the defaults happen to be today."""
    from trader.config import Settings
    from trader.strategy.builtin import DEFAULT_STRATEGIES, DonchianBreakout, EmaTrend, RsiReversion
    s = Settings()
    # the replay harness, not the config under test
    s.mode, s.use_llm_for_decisions, s.timeframe = "paper", False, "1d"
    s.auto_symbols, s.use_market_feed, s.autopilot = False, False, False
    s.align_with_leader, s.aggressiveness = False, "normal"
    strategies = list(DEFAULT_STRATEGIES)
    pinned = {
        "V15": (1.5, 1.0, 0.0, False), "V16": (2.5, 1.0, 0.0, False), "V19": (1.5, 0.0, 0.0, False),
        "OLD": (2.5, 0.0, 1.0, True),
    }
    if name != "shipped":
        if name not in pinned:
            raise SystemExit(f"unknown variant {name!r}: shipped, {', '.join(pinned)}")
        rr, part, trail, ema = pinned[name]
        s.risk.reward_risk, s.risk.partial_take_r, s.risk.trail_after_r = rr, part, trail
        s.risk.partial_take_frac = 0.5
        strategies = ([EmaTrend()] if ema else []) + [RsiReversion(), DonchianBreakout()]
    return s, strategies


# ---------------------------------------------------------------- the engine side
class FormingBarMarket:
    """Closed bars up to i-1, then bar i as it looks mid-day: open, high/low so far, close = px."""
    active_source = "replay"
    notice = None
    is_kcex = False
    abort = staticmethod(lambda: False)

    def __init__(self, sym: str, df):
        self.sym, self.df, self.i = sym, df, 0
        self.px = self.hi = self.lo = None

    def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
        sub = self.df.iloc[max(0, self.i + 1 - limit): self.i + 1].copy()
        if self.px is not None:
            col = sub.columns.get_loc
            sub.iat[-1, col("high")], sub.iat[-1, col("low")], sub.iat[-1, col("close")] = self.hi, self.lo, self.px
        return sub

    def price(self, symbol):
        return float(self.px if self.px is not None else self.df["close"].iloc[self.i])


def run_engine(sym, df, lo, s, strategies, balance, order, steps, hourly_rows):
    home = tempfile.mkdtemp(prefix="parity-")
    os.environ["TGTRADER_HOME"] = home
    try:
        from trader.db import Database
        from trader.engine import Engine
        from trader.execution.paper import PaperBroker
        s = copy.deepcopy(s)
        s.symbols, s.paper_start_balance, s.risk.capital_limit = [sym], balance, balance
        db = Database(pathlib.Path(home) / "p.db")
        broker = PaperBroker(balance, allow_short=False)
        broker.reset(balance)
        eng = Engine(s, db, broker=broker)
        eng.strategies = list(strategies)
        eng.log = lambda m, lvl="info": None
        mk = FormingBarMarket(sym, df)
        eng.market = mk
        index = df.index
        db.clock = lambda: float(index[min(mk.i, len(index) - 1)].timestamp())
        keys = [int(r[0]) for r in hourly_rows] if hourly_rows else []
        sparse = float(s.risk.trail_after_r or 0) <= 0 and not os.environ.get("PARITY_DENSE")
        errors: list[str] = []
        fallback = 0

        def levels() -> list[float]:
            out = []
            for p in db.open_trades("paper"):
                entry = float(p["entry_price"])
                init = float(p["init_stop"] or p["stop_price"] or 0)
                out += [float(p["stop_price"] or 0), float(p["take_profit"] or 0), entry]
                want_r = float(s.risk.partial_take_r or 0)
                if want_r > 0 and init:
                    out.append((entry + want_r * abs(entry - init)) * (1 + 1e-9))
            return [x for x in out if x > 0]

        def call(q: float) -> None:
            mk.px = q
            mk.hi, mk.lo = max(mk.hi, q), min(mk.lo, q)
            try:
                eng.loop_once()
            except Exception:
                errors.append(traceback.format_exc(limit=4))

        for i in range(lo, len(df)):
            mk.i = i
            day_ms = int(index[i].timestamp() * 1000)
            hours = hours_of_day(hourly_rows, keys, day_ms) if hourly_rows else None
            path, real = day_path(df.iloc[i], order, steps, hours)
            fallback += bool(hourly_rows) and not real
            mk.hi = mk.lo = path[0]
            call(path[0])
            last = len(path) - 1
            for n in range(1, len(path)):
                prev, px = path[n - 1], path[n]
                lv = levels()
                a, b = sorted((prev, px))
                for q in sorted((x for x in lv if a < x < b), reverse=px < prev):
                    call(q)
                lands = any(abs(x - px) <= 1e-12 * max(1.0, abs(px)) for x in lv)
                if not sparse or lands or n == last:
                    call(px)
                else:
                    mk.hi, mk.lo = max(mk.hi, px), min(mk.lo, px)
        mk.px = None
        closes = [(float(r["ts"]), r["reason"]) for r in db.query(
            "SELECT ts, reason FROM decisions WHERE action='close' AND source='risk' ORDER BY id")]
        ts_to_i = {float(t.timestamp()): k for k, t in enumerate(index)}
        trades = []
        for r in db.query("SELECT * FROM trades WHERE mode='paper' ORDER BY id"):
            why = "open"
            if r["closed_at"]:
                # the LAST close decision at that bar: a scale-out also writes a 'close' row
                c = [w for t, w in closes if abs(t - float(r["closed_at"])) < 1]
                why = c[-1] if c else "?"
            trades.append({"entry_i": ts_to_i.get(float(r["opened_at"])),
                           "entry_ts": int(float(r["opened_at"])),
                           "exit_i": ts_to_i.get(float(r["closed_at"])) if r["closed_at"] else None,
                           "entry": float(r["entry_price"]), "exit": float(r["exit_price"] or 0),
                           "r": r["r_multiple"], "why": why, "strategy": r["strategy"],
                           "scaled": r["part_qty"] is not None})
        db.close()
        return trades, errors, fallback
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------- the backtest side
def run_bt(sym, df, lo, s, strategies, balance, order, intraday):
    from trader.backtest.engine import engine_params, run_backtest
    params = dict(engine_params(s))
    params["strategies"] = list(strategies)
    params.setdefault("partial_at_r", s.risk.partial_take_r)
    params.setdefault("partial_frac", s.risk.partial_take_frac)
    res = run_backtest(sym, df, s.risk, start_equity=balance, warmup=lo - 1, allow_short=False,
                       trail_intrabar=(order == "trail-first"), intraday=intraday, **params)
    return [{"entry_i": t.entry_i, "entry_ts": int(df.index[t.entry_i].timestamp()), "exit_i": t.exit_i,
             "entry": t.entry, "exit": t.exit, "r": t.r,
             "why": t.reason.rsplit("-> ", 1)[-1] if "-> " in t.reason else "open",
             "strategy": t.strategy, "scaled": t.scaled} for t in res.trades]


# ---------------------------------------------------------------- pairing
def closed(ts: list) -> list:
    return [t for t in ts if t["r"] is not None and t["why"] != "open"]


def pair(eng: list, bt: list) -> dict:
    e = {t["entry_i"]: t for t in closed(eng)}
    b = {t["entry_i"]: t for t in closed(bt)}
    both = sorted(set(e) & set(b))
    diffs = [abs(float(e[k]["r"]) - float(b[k]["r"])) for k in both]
    return {"eng": len(e), "bt": len(b), "paired": len(both),
            "same_reason": sum(e[k]["why"] == b[k]["why"] for k in both),
            "same_exit": sum(e[k]["why"] == b[k]["why"] and e[k]["exit_i"] == b[k]["exit_i"] for k in both),
            "max_r_diff": max(diffs) if diffs else 0.0,
            "eng_r": sum(float(t["r"]) for t in e.values()), "bt_r": sum(float(t["r"]) for t in b.values()),
            "eng_win": sum(float(t["r"]) > 0 for t in e.values()), "bt_win": sum(float(t["r"]) > 0 for t in b.values()),
            "eng_only": sorted(set(e) - set(b)), "bt_only": sorted(set(b) - set(e)),
            "reason_diff": [k for k in both if e[k]["why"] != b[k]["why"]]}


def commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--variant", default="shipped")
    ap.add_argument("--path", choices=("daily", "hourly"), default="hourly")
    ap.add_argument("--order", choices=("conventional", "trail-first"), default="conventional")
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=1000)
    ap.add_argument("--lo", type=int, default=250, help="first bar the engine may enter on")
    ap.add_argument("--steps", type=int, default=1, help="prices per leg (levels are inserted anyway)")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--data", type=pathlib.Path, default=REPO / ".parity" / "data")
    ap.add_argument("--out", type=pathlib.Path, default=REPO / ".parity" / "trades")
    ap.add_argument("--fetch-mirror", metavar="URL")
    ap.add_argument("--pair-only", action="store_true", help="re-pair saved engine trades; no engine replay")
    ap.add_argument("--from-commit", default="", help="with --pair-only: whose saved engine trades (default: this checkout)")
    ap.add_argument("--show", type=int, default=5)
    args = ap.parse_args()

    if args.fetch_mirror:
        return 1 if fetch_mirror(args.fetch_mirror, args.data) else 0

    os.environ["TGTRADER_OFFLINE"] = "1"
    os.environ.setdefault("TGTRADER_HOME", tempfile.mkdtemp(prefix="parity-home-"))
    s, strategies = variant(args.variant)
    head = commit()
    src = args.from_commit or head
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"commit {head} | variant {args.variant}: rr {s.risk.reward_risk} partial {s.risk.partial_take_r}"
          f"x{s.risk.partial_take_frac} trail {s.risk.trail_after_r} strategies {[x.name for x in strategies]}"
          f" | path {args.path} order {args.order} | {'PAIR-ONLY, engine trades from ' + src if args.pair_only else 'engine replay'}",
          flush=True)

    total = {k: 0 for k in ("eng", "bt", "paired", "same_reason", "same_exit", "eng_win", "bt_win")}
    total.update(eng_r=0.0, bt_r=0.0, max_r_diff=0.0)
    exceptions = 0
    for sym in args.symbols:
        df = load_daily(args.data, sym, args.bars)
        hourly = load_hourly(args.data, sym) if args.path == "hourly" else []
        dump = args.out / f"{src}_{args.variant}_{args.path}_{args.order}_{sym.replace('/', '_')}.json"
        if args.pair_only:
            if not dump.exists():
                raise SystemExit(f"--pair-only: {dump} not found")
            doc = json.loads(dump.read_text())
            eng, n_err, fallback = doc["trades"], doc.get("exceptions", 0), doc.get("fallback_days", 0)
        else:
            eng, errors, fallback = run_engine(sym, df, args.lo, s, strategies, args.balance,
                                               args.order, args.steps, hourly)
            n_err = len(errors)
            dump.write_text(json.dumps({"commit": head, "variant": args.variant, "path": args.path,
                                        "order": args.order, "steps": args.steps, "symbol": sym, "lo": args.lo,
                                        "bars": args.bars, "fallback_days": fallback, "exceptions": n_err,
                                        "trades": eng}, indent=1))
            for e in errors[:2]:
                print("    ENGINE EXCEPTION:\n" + e)
        bt = run_bt(sym, df, args.lo, s, strategies, args.balance, args.order,
                    intraday_for_backtest(hourly, df.index) if hourly else None)
        p = pair(eng, bt)
        exceptions += n_err
        for k in total:
            total[k] = max(total[k], p[k]) if k == "max_r_diff" else total[k] + p[k]
        print(f"  {sym}: engine {p['eng']} tr {p['eng_r']:+.2f}R | backtest {p['bt']} tr {p['bt_r']:+.2f}R | "
              f"paired {p['paired']} same reason {p['same_reason']} same exit bar {p['same_exit']} | "
              f"max |dR| {p['max_r_diff']:.6f} | fallback days {fallback} | exceptions {n_err}", flush=True)
        for label, ks in (("ENGINE ONLY", p["eng_only"]), ("BACKTEST ONLY", p["bt_only"]),
                          ("REASON DIFFERS", p["reason_diff"])):
            for k in ks[: args.show]:
                print(f"    {label} at bar {k} ({df.index[k].date()})")

    n_e, n_b = max(1, total["eng"]), max(1, total["bt"])
    print(f"\nTOTAL engine {total['eng']} tr, win {total['eng_win'] / n_e * 100:.1f}%, {total['eng_r']:+.2f}R | "
          f"backtest {total['bt']} tr, win {total['bt_win'] / n_b * 100:.1f}%, {total['bt_r']:+.2f}R")
    print(f"paired {total['paired']} = {total['paired'] / n_b * 100:.0f}% of backtest, "
          f"{total['paired'] / n_e * 100:.0f}% of engine | same exit reason {total['same_reason']} | "
          f"same exit bar {total['same_exit']} | largest |R difference| {total['max_r_diff']:.6f} | "
          f"engine exceptions {exceptions}")
    tol = max(2.0, 0.05 * abs(total["bt_r"]))
    ok = (abs(total["eng_r"] - total["bt_r"]) <= tol and total["paired"] / n_b >= 0.90
          and total["paired"] / n_e >= 0.90 and exceptions == 0)
    print(f"H3/H4 for this run: {'PASS' if ok else 'FAIL'} (|dR| {abs(total['eng_r'] - total['bt_r']):.2f} "
          f"<= {tol:.2f}, pairing >= 90% both ways, no exceptions). H1/H2 compare runs across orders and variants.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
