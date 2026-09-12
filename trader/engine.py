"""The trading loop.

Every ``loop_seconds`` for every symbol:
  candles -> indicators -> regime -> rule signals -> (optional) Claude decision using skills
  -> risk gate and sizing -> broker -> journal.
Open positions are managed every loop: stop, target, trailing stop.

Rules this file must keep, because each one was a real bug:
  * one symbol failing must never stop the other symbols being managed;
  * a position is managed until it is closed, even if its symbol is removed from the list;
  * stops and targets are tested against the LIVE price, never against the whole in-progress
    bar (that bar contains price action from before the entry and before the stop moved);
  * fees are part of the P&L, on entry as well as on exit;
  * R is measured from the ORIGINAL stop, not from the trailed one;
  * open and close are serialised, so the GUI and the loop cannot close the same trade twice.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable

from . import analysis
from .config import Settings
from .db import Database
from .execution.base import Broker
from .execution.paper import PaperBroker
from .knowledge.skills import skills_prompt_block, load_seed_skills
from .market.data import MarketData
from .market.indicators import enrich, snapshot
from .risk.manager import RiskManager
from .strategy.builtin import evaluate_all, DEFAULT_STRATEGIES, Scalp
from .strategy.regime import detect_regime

# Length of one bar, per timeframe.
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
              "1d": 86400, "1w": 604800}

# How long a symbol is left alone after it stopped us out, IN BARS. Re-entering the same losing
# idea immediately is the most expensive habit a fast loop has. Counted in bars rather than in
# seconds on purpose: a fixed number of seconds is three bars on a 1m chart and a sixth of a bar
# on a daily one, so it silently meant something different on every timeframe.
COOLDOWN_BARS = 2.0

# The market leader every other crypto follows. Its trend is a filter, not a detail.
LEADER_SYMBOLS = ("BTC/USDT", "BTC/USD", "BTC/USDT:USDT", "BTC/BUSD")


class Engine:
    def __init__(self, settings: Settings, db: Database, broker: Broker | None = None,
                 brain: Any | None = None, on_event: Callable[[str], None] | None = None):
        # What is actually IN FORCE, which is not the same thing as what the owner typed: with
        # autopilot on, `effective()` hands back a copy carrying the values this project
        # measured. Resolved here, once, so every caller - the window, the CLI, the session
        # scripts, the tests - gets the same answer and nobody can forget to ask.
        settings = settings.effective()
        self.settings = settings
        self.db = db
        self.mode = settings.mode
        self.market = MarketData(settings, on_notice=lambda m: self.log(m, "warn"))
        self.risk = RiskManager(settings.risk, db, self.mode)
        self.broker = broker or self._make_broker()
        self.brain = brain
        self.on_event = on_event or (lambda s: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Serialises every open and close. The GUI closes trades on the Qt thread while this
        # loop closes them on its own; without it both can pass the "still open" check.
        self._trade_lock = threading.RLock()
        self.last_prices: dict[str, float] = {}
        # WHEN each of those was read. Without it a price has no age, and a cache with no age is
        # indistinguishable from a live quote - which is how a network outage stopped looking
        # like one: `last_prices` kept yesterday's numbers, the heartbeat went on reporting
        # every symbol as "priced", and `close_all` booked positions at prices from hours ago.
        self._price_at: dict[str, float] = {}
        self._last_bar: dict[str, float] = {}      # last candle a symbol was evaluated on
        self._entered_bar: dict[str, float] = {}   # last candle a symbol was entered on
        self._llm_bar: dict[str, float] = {}       # last candle the model was asked about
        self._cooldown: dict[str, float] = {}      # symbol -> "do not re-enter before" timestamp
        self._order_err: dict[str, str] = {}       # symbol -> last order error (de-duplicates the log)
        self._reconciled = False
        self._leader_regime: str | None = None   # trend of BTC on this pass
        self._unmanaged: dict[str, float] = {}   # symbol -> when its price last went missing
        self._last_beat = 0.0                    # when the engine last said it was alive
        # Whole-market watch. The sweep costs ~13 seconds of requests, so it runs on its own
        # thread and the trading loop only ever READS the result. Doing it inline would leave
        # every open position's stop unchecked for those seconds, once an hour, forever.
        self._watch: Any | None = None           # the last completed sweep
        self._watch_thread: threading.Thread | None = None
        self._watch_at = 0.0
        self._pruned_at = 0.0                    # when old analysis bars were last cleared
        self.status: dict[str, Any] = {"running": False, "last_loop": 0.0, "error": ""}
        load_seed_skills(db)
        self.strategies = ([Scalp()] + list(DEFAULT_STRATEGIES)
                           if settings.aggressiveness == "scalp" else list(DEFAULT_STRATEGIES))

    # ------------------------------------------------------------ setup
    def _make_broker(self) -> Broker:
        s = self.settings
        if s.mode == "paper":
            # Mirror the broker this is standing in for: forex through MT5 can short, crypto
            # through a spot ccxt account cannot. Paper that shorts on crypto reports a result
            # the live account could never have produced.
            return PaperBroker(s.paper_start_balance,
                               allow_short=(s.market == "forex" or s.paper_allow_short))
        if s.market == "forex":
            from .execution.mt5_broker import Mt5Broker
            return Mt5Broker(s)
        from .execution.ccxt_broker import CcxtBroker
        return CcxtBroker(s)

    def log(self, msg: str, level: str = "info") -> None:
        self.db.log(msg, level)
        self.on_event(f"[{level}] {msg}")

    def bar_seconds(self) -> float:
        return float(TF_SECONDS.get(self.settings.timeframe, 3600))

    def fee_rate(self) -> float:
        """Taker fee per side as a fraction. The paper broker knows its own; for a live
        exchange assume a normal taker fee rather than zero - assuming zero is what makes a
        scalping preset look profitable on paper and bleed in reality."""
        return float(getattr(self.broker, "fee_rate", 0.001) or 0.001)

    # ------------------------------------------------------------ thread control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 0.0) -> bool:
        """Ask the loop to stop. With ``wait`` seconds, also wait for it to actually finish.

        The thread is a daemon, so a caller that stops the engine and then lets the process go
        can cut the loop between placing a real exchange order and writing it to the journal -
        the trade would exist on the exchange and nowhere else. Returns True if it stopped.
        """
        self._stop.set()
        th = self._thread
        if wait > 0 and th is not None and th.is_alive():
            th.join(timeout=wait)
        # The market sweep is a SECOND thread and it sits in network requests for about
        # thirteen seconds at a time. Leaving it running is precisely the shape that made the
        # Windows test run exit 0xC0000005: a live thread inside OpenSSL when the process was
        # torn down. It is asked to stop through the same event - watchlist.choose() takes
        # `abort` and the fallback chain checks it between sources - so this join is short.
        wt = self._watch_thread
        if wait > 0 and wt is not None and wt.is_alive():
            wt.join(timeout=wait)
            if wt.is_alive():
                self.log("دیده‌بان بازار هنوز در حال درخواست شبکه است و جا نماند", "warn")
        alive = [t for t in (th, wt) if t is not None and t.is_alive()]
        return not alive

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        self.status["running"] = True
        self.log(f"engine started: mode={self.mode} market={self.settings.market} symbols={self.settings.symbols}")
        err = getattr(self.broker, "load_error", None)
        if err:
            self.log(f"paper account: {err}", "warn")
        while not self._stop.is_set():
            try:
                self.loop_once()
                self.status["error"] = ""
            except Exception as exc:
                self.status["error"] = str(exc)
                self.log(f"loop error: {exc}\n{traceback.format_exc(limit=3)}", "error")
            self.status["last_loop"] = time.time()
            self._stop.wait(self.settings.loop_seconds)
        self.status["running"] = False
        self.log("engine stopped")

    # ------------------------------------------------------------ reconciliation
    def _reconcile(self) -> None:
        """A journal row whose position the broker does not hold can never be closed, so it
        would be "managed" forever and would block its symbol from ever trading again."""
        held = getattr(self.broker, "positions", None)
        if not callable(held):
            return
        try:
            have = set(held().keys())
        except Exception:
            return
        for row in [dict(r) for r in self.db.open_trades(self.mode)]:
            if row["symbol"] in have:
                continue
            price = float(row["entry_price"])
            # Under the same lock as every other write to this table. Without it, a manual
            # close running on the GUI thread could have its real P&L overwritten with zero
            # between its own status check and its db.close_trade.
            with self._trade_lock:
                if not self.db.close_trade(row["id"], price, 0.0, None):
                    continue
            self.log(f"reconciled {row['symbol']}: the broker holds no such position, "
                     f"trade #{row['id']} marked closed at entry", "warn")

    # ------------------------------------------------------------ one pass
    # ------------------------------------------------------------ whole-market watch
    def watch_symbols(self) -> list[str]:
        """What the last market sweep picked, or the typed list when the watch is off."""
        w = self._watch
        if self.settings.auto_symbols and w and w.symbols:
            return list(w.symbols)
        return list(self.settings.symbols)

    def _maybe_sweep(self, held: list[str]) -> None:
        """Kick off a market sweep if one is due and none is running."""
        if not self.settings.auto_symbols or self._stop.is_set():
            return
        if self._watch_thread and self._watch_thread.is_alive():
            return
        every = max(5, int(self.settings.auto_symbols_every_min)) * 60.0
        if self._watch_at and time.time() - self._watch_at < every:
            return
        self._watch_at = time.time()      # stamped BEFORE the work, so a sweep that throws
                                          # cannot re-fire on every single loop

        def run() -> None:
            from .market import watchlist
            try:
                from .market import scanner as _scanner
                r = self.settings.risk
                cap = float(r.capital_limit) * float(r.max_position_frac or 1.0)
                w = watchlist.choose(
                    self.settings, self.market,
                    want=int(self.settings.auto_symbols_count),
                    pool=int(self.settings.auto_symbols_pool),
                    timeframe=self.settings.timeframe, keep=held,
                    allow_short=self.broker.supports_short(),
                    # what THIS account can get in and out of, rather than a fixed $3M that
                    # happened to leave 39 of 390 pairs
                    min_volume=_scanner.volume_floor(cap),
                    abort=self._stop.is_set)
                if self._stop.is_set():
                    return
                before = set(self.watch_symbols())
                self._watch = w
                self.log(f"دیده‌بان بازار: {w.note}")
                added = [x for x in w.symbols if x not in before]
                dropped = [x for x in before if x not in w.symbols and x not in held]
                if added or dropped:
                    self.log("فهرست کاری عوض شد"
                             + (f" · اضافه: {'، '.join(added)}" if added else "")
                             + (f" · حذف: {'، '.join(dropped)}" if dropped else ""))
            except Exception as exc:
                self.log(f"دیده‌بان بازار انجام نشد: {exc}", "warn")

        self._watch_thread = threading.Thread(target=run, name="market-watch", daemon=True)
        self._watch_thread.start()

    def _maybe_prune(self) -> None:
        """Once a day, drop the stored bars from analyses older than the retention window.

        A trade's analysis costs 25.8 KB, almost all of it candles - 1.3 MB a month at 50
        trades. The words are kept for ever; the picture is only worth keeping while the trade
        is recent enough to argue about."""
        now = time.time()
        if now - self._pruned_at < 86400.0:
            return
        self._pruned_at = now
        try:
            days = int(getattr(self.settings, "analysis_keep_days", 180))
            if days <= 0:
                return
            n = self.db.prune_analysis_candles(days)
            if n:
                self.log(f"کندل‌های {n} تحلیل قدیمی‌تر از {days} روز پاک شد "
                         f"(متن تحلیل‌ها سر جایشان است) · حجم دیتابیس "
                         f"{self.db.size_bytes()/1024/1024:.1f} مگابایت")
        except Exception as exc:
            self.log(f"پاک‌سازی تحلیل‌های قدیمی انجام نشد: {exc}", "warn")

    def loop_once(self) -> None:
        if not self._reconciled:
            self._reconcile()
            self._reconciled = True
        self._maybe_prune()
        open_positions = [dict(r) for r in self.db.open_trades(self.mode)]
        self._maybe_sweep([p["symbol"] for p in open_positions])
        # An open position is managed whatever the symbol list says now. Removing a symbol from
        # settings must not abandon a live trade with a stop nobody is watching.
        symbols = self.watch_symbols()
        for p in open_positions:
            if p["symbol"] not in symbols:
                symbols.append(p["symbol"])
        self._leader_regime = self._read_leader(symbols)
        candidates: list[dict[str, Any]] = []
        for symbol in symbols:
            if self._stop.is_set():
                return
            try:
                df = enrich(self.market.candles(symbol, limit=400))
                price = float(df["close"].iloc[-1])
                self.last_prices[symbol] = price
                self._price_at[symbol] = self.db.clock()
                pos = next((p for p in open_positions if p["symbol"] == symbol), None)
                if pos:
                    if self._manage(pos, df, price):
                        open_positions = [p for p in open_positions if p["id"] != pos["id"]]
                    continue
                cand = self._scout(symbol, df, price)
                if cand is not None:
                    candidates.append(cand)
            except Exception as exc:
                # One bad symbol must not leave every other position unmanaged for the whole pass.
                msg = f"{symbol}: {exc}"
                if self._order_err.get(f"loop:{symbol}") != msg:
                    self.log(f"symbol pass failed - {msg}", "warn")
                    self._order_err[f"loop:{symbol}"] = msg
                # An OPEN position still needs its stop checked. Candles come from the kline
                # endpoint and the price from the ticker endpoint - they fail independently, and
                # a rate limit usually hits the heavier one first. Skipping the whole symbol
                # meant a live position sat for hours with nobody watching its stop.
                pos = next((p for p in open_positions if p["symbol"] == symbol), None)
                if pos and self._manage_on_price_alone(pos):
                    open_positions = [p for p in open_positions if p["id"] != pos["id"]]
                continue
            self._order_err.pop(f"loop:{symbol}", None)

        for cand in self._rank_candidates(candidates):
            if self._stop.is_set():
                return
            try:
                self._enter(cand, open_positions)
            except Exception as exc:
                msg = f"{cand['symbol']}: {exc}"
                if self._order_err.get(f"enter:{cand['symbol']}") != msg:
                    self.log(f"entry failed - {msg}", "warn")
                    self._order_err[f"enter:{cand['symbol']}"] = msg

        if self.last_prices:
            try:
                self.db.record_equity(self.mode, self.broker.equity(self.last_prices))
            except Exception as exc:
                self.log(f"equity read failed: {exc}", "warn")
        self._heartbeat(open_positions, symbols)

    def price_age(self, symbol: str) -> float | None:
        """Seconds since this symbol's cached price was read, or None if there is none."""
        at = self._price_at.get(symbol)
        return None if at is None else max(0.0, self.db.clock() - at)

    def fresh_price(self, symbol: str, max_age: float = 120.0) -> float:
        """A price we are willing to act on: live if we can get one, cached only if it is young.

        The cached value is the FALLBACK, not the first choice. `close_all` had it the other way
        round - `last_prices.get(sym) or market.price(sym)` - so after an outage it closed every
        position at whatever price was last seen before the network went away, and wrote that
        into the journal as what happened. On paper that corrupts the record; live, the exchange
        fills at the real price and the journal disagrees with the account.
        """
        try:
            price = float(self.market.price(symbol))
            self.last_prices[symbol] = price
            self._price_at[symbol] = self.db.clock()
            return price
        except Exception:
            age = self.price_age(symbol)
            cached = self.last_prices.get(symbol)
            if cached is not None and age is not None and age <= max_age:
                return float(cached)
            raise

    def _heartbeat(self, open_positions: list[dict], symbols: list[str]) -> None:
        """Say we are alive, even when nothing happened.

        A quiet engine and a dead one look identical from outside, and that is not a cosmetic
        problem: measured on the owner's machine, eight minutes passed with five positions open
        and not one line printed. Nothing distinguished "watching five stops, nothing hit" from
        "the loop died" or "the network went away" - the only negative signal was the ABSENCE
        of an UNMANAGED warning, which itself only appears in one specific failure.
        """
        now = time.time()
        if now - self._last_beat < max(60.0, float(self.settings.loop_seconds)):
            return
        self._last_beat = now
        held = len(open_positions)
        # FRESH, not "we have a number for it". A cached price never expired, so through a
        # network outage this line went on saying "10 of 10 symbols have prices" while none of
        # them had been read for hours - the one line whose whole job is to say whether the bot
        # can still see the market was the line hiding that it could not.
        fresh_for = max(300.0, 5.0 * float(self.settings.loop_seconds))
        ages = {s: self.price_age(s) for s in symbols}
        priced = sum(1 for a in ages.values() if a is not None and a <= fresh_for)
        try:
            eq = self.broker.equity(self.last_prices)
            money = f"، سرمایه {eq:,.2f}"
        except Exception:
            money = ""
        old_ones = sorted(((a, s) for s, a in ages.items() if a is None or a > fresh_for),
                          key=lambda pair: -(pair[0] or 10 ** 9))
        tail = ""
        if old_ones:
            names = "، ".join(f"{sym} ({int(a / 60)}د)" if a else f"{sym} (هیچ‌وقت)"
                              for a, sym in old_ones[:4])
            tail = f"، بدون قیمت تازه: {names}"
        self.log(f"زنده‌ام · {held} پوزیشن باز، {priced} از {len(symbols)} نماد قیمت تازه دارند{money}{tail}")

    def _leader(self, symbols: list[str]) -> str | None:
        if self.settings.market != "crypto" or not self.settings.align_with_leader:
            return None
        for s in symbols:
            if s.upper() in LEADER_SYMBOLS:
                return s
        return LEADER_SYMBOLS[0]

    def _read_leader(self, symbols: list[str]) -> str | None:
        """Bitcoin's trend on this pass, or None when the filter is off or unavailable.

        Unavailable must mean "do not filter", never "refuse everything": a data hiccup on one
        symbol is not a reason to stop trading the other nine."""
        lead = self._leader(symbols)
        if not lead:
            return None
        try:
            return detect_regime(enrich(self.market.candles(lead, limit=400)))
        except Exception:
            return None

    # ------------------------------------------------------------ entries
    def _consider_entry(self, symbol: str, df, price: float, open_positions: list[dict]) -> None:
        """Look at one symbol and take the trade if there is one. Kept for direct callers."""
        cand = self._scout(symbol, df, price)
        if cand is not None:
            self._enter(cand, open_positions)

    def _scout(self, symbol: str, df, price: float) -> dict[str, Any] | None:
        """Read the chart and report what it found, WITHOUT spending a slot on it.

        Split out of ``_consider_entry`` so the pass can rank candidates before any of them
        takes capital - see ``loop_once``. Nothing here touches the account, so a symbol that
        is scouted and then not entered costs nothing but the arithmetic.
        """
        agg = self.settings.aggressiveness
        # Outside scalping, evaluate on the CLOSED bar. The last row of the frame is the bar
        # still forming: a signal read off it can un-happen before the bar closes, which is a
        # signal that backtests beautifully and does not exist in real time.
        eval_df = df if agg == "scalp" or len(df) < 80 else df.iloc[:-1]
        regime = detect_regime(eval_df)
        signals = evaluate_all(symbol, eval_df, regime, self.strategies)
        if not self.broker.supports_short():
            signals = [s for s in signals if s.side == "long"]
        bar_ts = float(eval_df.index[-1].timestamp())
        # Never take the same bar twice: after a quick stop-out the same closed-bar signal is
        # still sitting there and would be re-entered immediately.
        if self._entered_bar.get(symbol) == bar_ts:
            return
        stopped_at = self._cooldown.get(symbol)
        if stopped_at is not None and bar_ts - stopped_at < COOLDOWN_BARS * self.bar_seconds():
            return
        # One evaluation per candle unless a fresh rule signal appears: a 1h/1d snapshot barely
        # changes within the same bar, so re-asking the model every loop only burns API cost.
        if not signals and self._last_bar.get(symbol) == bar_ts:
            return
        self._last_bar[symbol] = bar_ts
        snap = snapshot(eval_df)
        return {"symbol": symbol, "df": df, "eval_df": eval_df, "price": price,
                "regime": regime, "signals": signals, "snap": snap, "bar_ts": bar_ts,
                "strength": max((s.strength for s in signals), default=0.0)}

    def _headlines(self, symbol: str) -> list[dict[str, Any]]:
        """What was published about this coin, from the server sweep. Never fatal, never slow.

        Read out of the watch the sweep already fetched rather than asking the network here: a
        decision that waits on an HTTP request is a decision taken at a price that has moved on.
        """
        try:
            data = getattr(self._watch, "feed", None)
            if not data:
                return []
            from .market import feed as _feed
            return _feed.headlines_for(data, symbol)
        except Exception:
            return []

    def _rank_candidates(self, candidates: list[dict]) -> list[dict]:
        """Decide which setup gets the money when more want a slot than there are slots.

        This is a real question, not a detail: over 16 symbols and 750 daily bars, 343 entries
        were refused because all four slots were taken. On most bars the engine is not asking
        "is there a setup", it is asking "which of these".

        It is a separate method so the answer can be MEASURED rather than argued about - the
        experiment swaps it and replays the same bars.

        It returns them in the order they were scouted, which is the symbol list's own order -
        exactly what the engine did before the scout/enter split, so the split changed no
        behaviour. Ranking by `strength` was tried first and is NOT what this returns: the
        built-in strategies hand out CONSTANTS (EmaTrend 0.6, DonchianBreakout 0.55,
        RsiReversion 0.5) and over 750 bars only two distinct values were ever seen, so sorting
        by it ranks by which rule fired and then falls back to the symbol order anyway. A
        ranking key that cannot tell two setups apart is not a ranking.
        """
        return candidates

    def _enter(self, cand: dict[str, Any], open_positions: list[dict]) -> None:
        """Take the trade a scout found, if the risk layer still has room for it."""
        symbol, price = cand["symbol"], cand["price"]
        eval_df, regime, signals = cand["eval_df"], cand["regime"], cand["signals"]
        snap, bar_ts = cand["snap"], cand["bar_ts"]
        agg = self.settings.aggressiveness
        equity = self.broker.equity(self.last_prices)

        refuse = self.risk.check(symbol, open_positions, equity, self.last_prices)
        if refuse:
            if signals:
                self.db.add_decision(symbol, "hold", None, "risk", refuse, {"regime": regime})
            return

        min_conf = {"high": 0.4, "scalp": 0.0}.get(agg, 0.55)
        # In scalp mode we act on the rule signals directly: the model is deliberately patient
        # ("hold is usually right"), which is the opposite of what this preset is for.
        use_llm = self.settings.use_llm_for_decisions and self.brain is not None and agg != "scalp"
        if use_llm and self._llm_bar.get(symbol) == bar_ts:
            # Already asked about this bar. The once-per-bar gate above only skips a QUIET bar,
            # so a signal that persists across a whole daily candle used to buy a full model
            # call every loop_seconds - hundreds of paid calls for one decision that cannot
            # change until the bar does.
            return
        decision: dict[str, Any] | None = None
        source = "rules"
        if use_llm and (signals or regime in ("trend_up", "trend_down")):
            try:
                knowledge = [r["content"] for r in self.db.search_knowledge(f"{regime} {' '.join(s.strategy for s in signals)}", limit=3)]
                self._llm_bar[symbol] = bar_ts
                decision = self.brain.decide(
                    symbol, snap, regime,
                    [{"strategy": s.strategy, "side": s.side, "strength": s.strength, "reason": s.reason} for s in signals],
                    None, skills_prompt_block(self.db), knowledge,
                    headlines=self._headlines(symbol),
                )
                source = "llm"
            except Exception as exc:
                # Fail CLOSED. Falling through to the raw rules would silently trade a different,
                # untested system every time the API is unreachable - and the user asked for the
                # model's judgement precisely because the rules alone are not the plan.
                self.log(f"LLM decision failed for {symbol}: {exc}", "warn")
                self.db.add_decision(symbol, "hold", None, "llm", f"model unavailable: {exc}",
                                     {"regime": regime})
                return

        if decision is None:
            if not signals:
                return
            best = max(signals, key=lambda s: s.strength)
            decision = {"action": "buy" if best.side == "long" else "sell", "confidence": best.strength,
                        "reason": best.reason, "stop_distance_atr": self.settings.risk.atr_stop_mult,
                        "stop_distance": best.stop_distance,
                        "skills_used": [], "strategy": best.strategy}

        action = decision["action"]
        payload = {"regime": regime, "snapshot": snap, "signals": [s.reason for s in signals], "decision": decision}
        if action not in ("buy", "sell") or decision["confidence"] < min_conf:
            self.db.add_decision(symbol, "hold", decision.get("confidence"), source, decision.get("reason", ""), payload)
            return
        side = "long" if action == "buy" else "short"
        # Almost every coin is a leveraged bet on Bitcoin, and in a sell-off the correlation
        # goes to nearly one. Ten longs on ten alts while BTC breaks down is one very large
        # long on BTC with ten sets of fees.
        lead = self._leader(list(self.settings.symbols))
        if (lead and symbol.upper() not in LEADER_SYMBOLS and self._leader_regime
                and ((side == "long" and self._leader_regime == "trend_down")
                     or (side == "short" and self._leader_regime == "trend_up"))):
            self.db.add_decision(symbol, "hold", decision["confidence"], "risk",
                                 f"against the market leader: {lead} is in {self._leader_regime}", payload)
            return
        if side == "short" and not self.broker.supports_short():
            self.db.add_decision(symbol, "hold", decision["confidence"], source, "short not supported by broker", payload)
            return

        atr_v = snap.get("atr14") or 0.0
        # The strategy's own stop distance is the one its entry was designed around; the ATR
        # multiple is the fallback for the model path and for rules that do not set one.
        stop_distance = float(decision.get("stop_distance") or 0.0)
        if stop_distance <= 0:
            stop_distance = float(decision.get("stop_distance_atr", 2.0)) * atr_v
        # A stop closer than the round trip in fees is not a stop, it is a donation.
        min_stop = price * self.fee_rate() * 4.0
        stop_distance = max(stop_distance, min_stop)
        if stop_distance <= 0:
            return

        # Fee-aware filter. Reward has to clear the round trip by a real margin, or the strategy
        # is paying the exchange to take its edge. This is the actual reason a fast preset bleeds.
        round_trip = 2.0 * self.fee_rate() * price
        gross_target = self.settings.risk.reward_risk * stop_distance
        if gross_target <= 3.0 * round_trip:
            self.db.add_decision(symbol, "hold", decision.get("confidence"), "risk",
                                 f"target {gross_target:.6g} does not clear the round-trip fee "
                                 f"{round_trip:.6g} by 3x", payload)
            return

        min_qty, step = self.broker.limits(symbol)
        if getattr(self.market, "is_kcex", False) and not (min_qty or step):
            try:
                min_qty, step = self.market.kcex.limits(symbol)
            except Exception:
                pass
        try:
            cash = self.broker.cash()
        except Exception:
            cash = equity
        sizing = self.risk.size(side, price, stop_distance, equity, min_qty, step, cash=cash,
                                position_pct=getattr(self.settings, "position_pct", 0.0),
                                open_positions=open_positions)
        if sizing is None:
            self.db.add_decision(symbol, "hold", decision["confidence"], "risk",
                                 "not enough free cash or open-risk budget for a new position", payload)
            return
        strategy = decision.get("strategy") or ("llm" if source == "llm" else "rules")
        self.db.add_decision(symbol, action, decision["confidence"], source, decision.get("reason", ""), payload)
        with self._trade_lock:
            # Re-check under the lock: another thread may have opened this symbol meanwhile.
            if any(r["symbol"] == symbol for r in self.db.open_trades(self.mode)):
                return
            try:
                fill = self.broker.market_order(symbol, "buy" if side == "long" else "sell", sizing.qty, price)
            except Exception as exc:
                # de-duplicate PER SYMBOL: one symbol out of cash used to silence the log for all
                msg = f"order failed on {symbol}: {exc}"
                if self._order_err.get(symbol) != msg:
                    self.log(msg, "warn")
                    self._order_err[symbol] = msg
                return
            self._order_err.pop(symbol, None)
            # stops are recomputed from the actual fill price
            stop = fill.price - stop_distance if side == "long" else fill.price + stop_distance
            tp = fill.price + self.settings.risk.reward_risk * stop_distance if side == "long" \
                else fill.price - self.settings.risk.reward_risk * stop_distance
            tid = self.db.open_trade(self.mode, symbol, side, fill.qty, fill.price, stop, tp,
                                     strategy, decision.get("reason", ""), entry_fee=fill.fee)
        # Written AFTER the fill, because it records the stop and target computed from the
        # price actually paid - not the ones planned off the pre-order price.
        try:
            self.db.add_trade_analysis(
                tid, "open", symbol, self.settings.timeframe,
                analysis.entry_analysis(
                    symbol=symbol, timeframe=self.settings.timeframe, side=side, regime=regime,
                    snap=snap, signals=signals, decision=decision, source=source,
                    price=fill.price, stop=stop, target=tp, stop_distance=stop_distance,
                    qty=fill.qty, fee_rate=self.fee_rate(), round_trip=round_trip,
                    gross_target=gross_target, reward_risk=self.settings.risk.reward_risk,
                    equity=equity, risk_amount=getattr(sizing, "risk_amount", None),
                    leader=lead, leader_regime=self._leader_regime),
                analysis.bars(eval_df))
        except Exception as exc:
            # A missing analysis must never cost a trade that is already open on the exchange.
            self.log(f"analysis not stored for {symbol}: {exc}", "warn")
        self._entered_bar[symbol] = bar_ts
        open_positions.append({"id": tid, "symbol": symbol, "side": side, "qty": fill.qty,
                               "entry_price": fill.price, "stop_price": stop, "init_stop": stop})
        self.log(f"OPEN {side} {symbol} qty={fill.qty:g} @ {fill.price:g} stop={stop:g} tp={tp:g} "
                 f"fee={fill.fee:.6g} ({strategy})")

    # ------------------------------------------------------------ position management
    def _manage(self, pos: dict, df, price: float) -> bool:
        """Returns True if the position was closed on this pass."""
        side, entry = pos["side"], float(pos["entry_price"])
        stop, tp = float(pos["stop_price"]), float(pos["take_profit"] or 0)
        # Only the LIVE price may trigger a stop or a target. The in-progress bar's high and low
        # include price action from before this trade was opened and from before the stop was
        # last moved, so testing against them closes trades at prices that never existed for us.
        why = None
        if side == "long":
            if price <= stop:
                why = "stop"
            elif tp and price >= tp:
                why = "target"
        else:
            if price >= stop:
                why = "stop"
            elif tp and price <= tp:
                why = "target"
        if why is None:
            # trend-change exit for trend trades, read off the CLOSED bar. Entries were moved
            # off the forming bar in this same file because a signal on it can un-happen before
            # the bar closes; an EXIT decided that way is the same mistake, and it closes a
            # position for real money.
            regime = detect_regime(df.iloc[:-1] if len(df) > 80 else df)
            if (side == "long" and regime == "trend_down") or (side == "short" and regime == "trend_up"):
                why = "regime flipped"
        if why is None and self._maybe_scale_out(pos, price):
            return False          # part banked, the rest keeps running with a break-even stop
        if why is None:
            init_stop = pos.get("init_stop")
            new_stop = self.risk.trail_stop(side, entry, stop, price,
                                            float(init_stop) if init_stop else None)
            if new_stop != stop:
                self.db.update_stop(pos["id"], new_stop)
                self.log(f"trail {pos['symbol']} stop {stop:g} -> {new_stop:g}")
            return False
        # The CLOSED bar, to match what _consider_entry compares against. Stamping the forming
        # bar made a two-bar cooldown last three outside scalp mode.
        closed_ts = float(df.index[-2].timestamp()) if len(df) > 1 else float(df.index[-1].timestamp())
        return self.close_position(pos, price, why, bar_ts=closed_ts, df=df)

    def _maybe_scale_out(self, pos: dict, price: float) -> bool:
        """Sell part of a winner at a set multiple of its risk and move the stop to break-even.

        OFF unless partial_take_r is set. It is a trade-off rather than an improvement, and the
        numbers are in config.py: taking half at 1R raised the win rate in every one of four
        splits and cut the worst drawdown by 23%, and cost about 12% of the return.

        Two things here are load-bearing. The sale goes through the SAME lock as every other
        open and close, because it is a partial close and two of them racing would sell the
        position twice. And the database write is what decides it happened - if the row does
        not update, the fill is still real, so the trade is closed rather than left with the
        journal and the broker disagreeing about how much is held.
        """
        want_r = float(getattr(self.settings.risk, "partial_take_r", 0.0) or 0.0)
        if want_r <= 0 or pos.get("part_qty"):
            return False                      # off, or this trade has already scaled out
        side, entry = pos["side"], float(pos["entry_price"])
        init_stop = pos.get("init_stop") or pos.get("stop_price")
        r_dist = abs(entry - float(init_stop)) if init_stop else 0.0
        if r_dist <= 0:
            return False
        gain = (price - entry) if side == "long" else (entry - price)
        if gain < want_r * r_dist:
            return False
        frac = float(getattr(self.settings.risk, "partial_take_frac", 0.5) or 0.5)
        qty = float(pos["qty"])
        sell = qty * frac
        min_qty, step = self.broker.limits(pos["symbol"])
        if step:
            sell = (int(sell / step)) * step
        if sell <= 0 or (min_qty and sell < min_qty) or qty - sell <= 0:
            return False                      # not enough to split without leaving dust
        # The gain on the part being sold has to clear what selling it COSTS, by a real margin.
        # Without this, a small partial_take_r banks a loss every time and calls it taking
        # profit: driven on the real engine at 0.02R, the sale booked -0.045 - the gross gain
        # was 0.017 and the fees were 0.06. It is the same guard the entry already applies to a
        # target that does not clear the round trip, and it is why that guard exists.
        round_trip = 2.0 * self.fee_rate() * price * sell
        if gain * sell <= 2.0 * round_trip:
            return False
        with self._trade_lock:
            row = self.db.one("SELECT status, qty, part_qty FROM trades WHERE id=?", (pos["id"],))
            if not row or row["status"] != "open" or row["part_qty"] is not None:
                return False
            try:
                fill = self.broker.market_order(pos["symbol"], "sell" if side == "long" else "buy",
                                                sell, price, close=True)
            except Exception as exc:
                self.log(f"scale-out failed on {pos['symbol']}: {exc}", "warn")
                return False
            gross = ((fill.price - entry) if side == "long" else (entry - fill.price)) * fill.qty
            entry_fee = float(pos.get("entry_fee") or 0.0)
            # This sale takes its SHARE of the entry fee, and the row keeps the rest. Both
            # halves matter: subtracting the share here without lowering the row charged the
            # fee one and a half times, so every scaled trade reported less profit than the
            # account actually made.
            sold_share = fill.qty / qty if qty else frac
            fee_taken = entry_fee * sold_share
            banked = gross - fill.fee - fee_taken
            if not self.db.scale_out(int(pos["id"]), fill.qty, banked, entry,
                                     entry_fee - fee_taken):
                # The sale happened and the journal did not record it. Closing the rest is the
                # only state both sides can agree on.
                self.log(f"{pos['symbol']}: scale-out sold {fill.qty:g} but the journal did not "
                         f"record it - closing the remainder", "error")
                self.close_position(pos, price, "scale-out mismatch")
                return True
        pos["qty"] = qty - fill.qty
        pos["part_qty"] = qty
        pos["stop_price"] = entry
        pos["entry_fee"] = entry_fee - fee_taken
        self.db.add_decision(pos["symbol"], "close", None, "risk",
                             f"نصف پوزیشن در {want_r:g}R برداشته شد، حد ضرر روی نقطه‌ی سربه‌سر",
                             {"banked": banked, "sold": fill.qty, "left": pos["qty"]})
        self.log(f"SCALE-OUT {pos['symbol']} sold {fill.qty:g} of {qty:g} @ {fill.price:g} "
                 f"banked={banked:+.4f} stop -> break-even {entry:g}")
        return True

    def _manage_on_price_alone(self, pos: dict) -> bool:
        """Stop and target only, from the live price, when candles are unavailable.

        No regime exit and no trailing here: both need candles, and inventing them from a
        single tick would be worse than waiting. Returns True if the position was closed.
        """
        sym = pos["symbol"]
        try:
            price = float(self.market.price(sym))
        except Exception as exc:
            # The DATABASE's clock, for the same reason the daily loss cap uses it: this is
            # "how long has this market been dark to us", and in a replay that is replay time.
            # Live it is `time.time` and nothing changes. Before this, the blackout scenario
            # could never reach five minutes - the whole two-year replay takes eighty seconds -
            # so the one warning that says a stop is not being watched was the one thing the
            # heavy session could not exercise.
            now = self.db.clock()
            first = self._unmanaged.setdefault(sym, now)
            mins = (now - first) / 60.0
            key = f"unmanaged:{sym}"
            # Say it again when the wait has DOUBLED, not every time it changes. The de-dup key
            # used to be the whole minute, which says "warn once per minute for as long as this
            # lasts" - on a one-minute live loop a symbol dark for a day writes 1,440 identical
            # error lines, and the first heavy session that could actually reach the warning
            # produced 699 of them. A journal of one repeated sentence is a journal nobody
            # reads, which costs the warning its whole purpose. This ladders 5, 10, 20, 40 ...
            # minutes: nine lines in the first day, and the wait in each one is news.
            said = float(self._order_err.get(key) or 0.0)
            if mins > 5 and mins >= max(5.0, said * 2):
                self._order_err[key] = f"{mins:.0f}"
                # one language per line: this one has always been English, and "1.0 روز" inside
                # an English sentence reads as a bug in the message rather than as a warning
                wait = (f"{mins/1440:.1f} days" if mins >= 1440 else
                        (f"{mins/60:.1f} hours" if mins >= 60 else f"{mins:.0f} minutes"))
                self.log(f"UNMANAGED: {sym} has had no price for {wait} - its stop "
                         f"is not being checked ({exc})", "error")
            return False
        self._unmanaged.pop(sym, None)
        self._order_err.pop(f"unmanaged:{sym}", None)
        self.last_prices[sym] = price
        self._price_at[sym] = self.db.clock()
        side, stop = pos["side"], float(pos["stop_price"])
        tp = float(pos["take_profit"] or 0)
        why = None
        if side == "long":
            if price <= stop:
                why = "stop"
            elif tp and price >= tp:
                why = "target"
        else:
            if price >= stop:
                why = "stop"
            elif tp and price <= tp:
                why = "target"
        if why is None:
            return False
        self.log(f"{sym}: closing on the live price alone - candles are unavailable", "warn")
        return self.close_position(pos, price, why)

    def close_position(self, pos: dict, price: float, why: str, bar_ts: float | None = None,
                       df=None) -> bool:
        """`df` is the frame this close was decided on, kept with the analysis so the exit can
        be looked at on the same chart as the entry. It is optional: a close driven by the live
        price alone genuinely has no frame, and an exit with no chart is better than no exit."""
        side = pos["side"]
        with self._trade_lock:
            # Idempotency starts here: if the row is no longer open, somebody else closed it.
            row = self.db.one("SELECT status FROM trades WHERE id=?", (pos["id"],))
            if not row or row["status"] != "open":
                return False
            try:
                fill = self.broker.market_order(pos["symbol"], "sell" if side == "long" else "buy",
                                                float(pos["qty"]), price, close=True)
            except Exception as exc:
                if "no open" in str(exc).lower():
                    # The broker does not hold it. Leaving the row open means managing a ghost
                    # forever and blocking the symbol; close it flat and say so.
                    self.db.close_trade(pos["id"], price, 0.0, None)
                    self.log(f"{pos['symbol']}: broker holds no position, trade #{pos['id']} "
                             f"closed flat in the journal", "warn")
                    return True
                self.log(f"close failed on {pos['symbol']}: {exc}", "error")
                return False
            entry = float(pos["entry_price"])
            gross = (fill.price - entry) * fill.qty if side == "long" else (entry - fill.price) * fill.qty
            # BOTH fees. Charging only the exit made every trade look better than it was, and
            # the daily-loss limit was measured against those inflated numbers.
            entry_fee = float(pos.get("entry_fee") or 0.0)
            # Whatever a scale-out already banked is part of THIS trade's result. Without it a
            # trade that took half off at 1R and then stopped at break-even reports a small loss
            # while the account is up.
            banked = float(pos.get("part_pnl") or 0.0)
            pnl = gross - fill.fee - entry_fee + banked
            # R is measured from the ORIGINAL stop. From the trailed stop, a winner that trailed
            # to break-even reports an infinite R and the statistics become meaningless.
            init_stop = pos.get("init_stop") or pos.get("stop_price")
            r_dist = abs(entry - float(init_stop)) if init_stop else 0.0
            # ...and against the size the trade was OPENED with. Dividing by what is LEFT after
            # a scale-out doubles the reported R of the same move, and every statistic built on
            # R - average R, the backtest comparison, the whole trades page - inflates with it.
            risk_qty = float(pos.get("part_qty") or fill.qty)
            r = pnl / (risk_qty * r_dist) if r_dist and risk_qty else None
            if not self.db.close_trade(pos["id"], fill.price, pnl, r):
                return False
        if why == "stop":
            # Stamped with the BAR this happened on (epoch seconds either way), so the cooldown
            # is measured in market time and behaves identically live and in a replay.
            self._cooldown[pos["symbol"]] = bar_ts if bar_ts else time.time()
        self.db.add_decision(pos["symbol"], "close", None, "risk", why,
                             {"pnl": pnl, "r": r, "fees": fill.fee + entry_fee})
        try:
            opened = pos.get("opened_at")
            self.db.add_trade_analysis(
                int(pos["id"]), "close", pos["symbol"], self.settings.timeframe,
                analysis.exit_analysis(
                    symbol=pos["symbol"], timeframe=self.settings.timeframe, side=side, why=why,
                    entry=entry, exit_price=fill.price, stop=pos.get("stop_price"),
                    init_stop=init_stop, target=pos.get("take_profit"), qty=fill.qty,
                    pnl=pnl, r_multiple=r, fees=fill.fee + entry_fee,
                    # `opened_at` was stamped through db.clock, so the other end of this
                    # subtraction has to be the same clock or the two drift apart - in a replay
                    # it would read a wall-clock "now" minus a 2024 stamp and call every trade
                    # four hundred days long. Live, db.clock IS time.time.
                    held_seconds=(self.db.clock() - float(opened)) if opened else None,
                    regime=detect_regime(df) if df is not None and len(df) > 50 else None,
                    snap=snapshot(df) if df is not None and len(df) else None),
                analysis.bars(df) if df is not None else None)
        except Exception as exc:
            self.log(f"exit analysis not stored for {pos['symbol']}: {exc}", "warn")
        self.log(f"CLOSE {side} {pos['symbol']} @ {fill.price:g} pnl={pnl:+.4f} "
                 f"fees={fill.fee + entry_fee:.6g} ({why})")
        return True

    def close_all(self, why: str = "manual") -> None:
        for pos in [dict(r) for r in self.db.open_trades(self.mode)]:
            try:
                price = self.fresh_price(pos["symbol"])
                self.close_position(pos, price, why)
            except Exception as exc:
                self.log(f"close_all failed on {pos['symbol']}: {exc}", "error")
