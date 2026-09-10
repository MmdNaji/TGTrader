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
        self._last_bar: dict[str, float] = {}      # last candle a symbol was evaluated on
        self._entered_bar: dict[str, float] = {}   # last candle a symbol was entered on
        self._llm_bar: dict[str, float] = {}       # last candle the model was asked about
        self._cooldown: dict[str, float] = {}      # symbol -> "do not re-enter before" timestamp
        self._order_err: dict[str, str] = {}       # symbol -> last order error (de-duplicates the log)
        self._reconciled = False
        self._leader_regime: str | None = None   # trend of BTC on this pass
        self._unmanaged: dict[str, float] = {}   # symbol -> when its price last went missing
        self.status: dict[str, Any] = {"running": False, "last_loop": 0.0, "error": ""}
        load_seed_skills(db)
        self.strategies = ([Scalp()] + list(DEFAULT_STRATEGIES)
                           if settings.aggressiveness == "scalp" else list(DEFAULT_STRATEGIES))

    # ------------------------------------------------------------ setup
    def _make_broker(self) -> Broker:
        s = self.settings
        if s.mode == "paper":
            return PaperBroker(s.paper_start_balance)
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
        return not (th is not None and th.is_alive())

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
    def loop_once(self) -> None:
        if not self._reconciled:
            self._reconcile()
            self._reconciled = True
        open_positions = [dict(r) for r in self.db.open_trades(self.mode)]
        # An open position is managed whatever the symbol list says now. Removing a symbol from
        # settings must not abandon a live trade with a stop nobody is watching.
        symbols = list(self.settings.symbols)
        for p in open_positions:
            if p["symbol"] not in symbols:
                symbols.append(p["symbol"])
        self._leader_regime = self._read_leader(symbols)
        for symbol in symbols:
            if self._stop.is_set():
                return
            try:
                df = enrich(self.market.candles(symbol, limit=400))
                price = float(df["close"].iloc[-1])
                self.last_prices[symbol] = price
                pos = next((p for p in open_positions if p["symbol"] == symbol), None)
                if pos:
                    if self._manage(pos, df, price):
                        open_positions = [p for p in open_positions if p["id"] != pos["id"]]
                    continue
                self._consider_entry(symbol, df, price, open_positions)
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
        if self.last_prices:
            try:
                self.db.record_equity(self.mode, self.broker.equity(self.last_prices))
            except Exception as exc:
                self.log(f"equity read failed: {exc}", "warn")

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
        equity = self.broker.equity(self.last_prices)

        refuse = self.risk.check(symbol, open_positions, equity)
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
        return self.close_position(pos, price, why, bar_ts=closed_ts)

    def _manage_on_price_alone(self, pos: dict) -> bool:
        """Stop and target only, from the live price, when candles are unavailable.

        No regime exit and no trailing here: both need candles, and inventing them from a
        single tick would be worse than waiting. Returns True if the position was closed.
        """
        sym = pos["symbol"]
        try:
            price = float(self.market.price(sym))
        except Exception as exc:
            first = self._unmanaged.setdefault(sym, time.time())
            mins = (time.time() - first) / 60.0
            key = f"unmanaged:{sym}"
            if mins > 5 and self._order_err.get(key) != f"{int(mins)}":
                self._order_err[key] = f"{int(mins)}"
                self.log(f"UNMANAGED: {sym} has had no price for {mins:.0f} minutes - its stop "
                         f"is not being checked ({exc})", "error")
            return False
        self._unmanaged.pop(sym, None)
        self._order_err.pop(f"unmanaged:{sym}", None)
        self.last_prices[sym] = price
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

    def close_position(self, pos: dict, price: float, why: str, bar_ts: float | None = None) -> bool:
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
            pnl = gross - fill.fee - entry_fee
            # R is measured from the ORIGINAL stop. From the trailed stop, a winner that trailed
            # to break-even reports an infinite R and the statistics become meaningless.
            init_stop = pos.get("init_stop") or pos.get("stop_price")
            r_dist = abs(entry - float(init_stop)) if init_stop else 0.0
            r = pnl / (fill.qty * r_dist) if r_dist and fill.qty else None
            if not self.db.close_trade(pos["id"], fill.price, pnl, r):
                return False
        if why == "stop":
            # Stamped with the BAR this happened on (epoch seconds either way), so the cooldown
            # is measured in market time and behaves identically live and in a replay.
            self._cooldown[pos["symbol"]] = bar_ts if bar_ts else time.time()
        self.db.add_decision(pos["symbol"], "close", None, "risk", why,
                             {"pnl": pnl, "r": r, "fees": fill.fee + entry_fee})
        self.log(f"CLOSE {side} {pos['symbol']} @ {fill.price:g} pnl={pnl:+.4f} "
                 f"fees={fill.fee + entry_fee:.6g} ({why})")
        return True

    def close_all(self, why: str = "manual") -> None:
        for pos in [dict(r) for r in self.db.open_trades(self.mode)]:
            try:
                price = self.last_prices.get(pos["symbol"]) or self.market.price(pos["symbol"])
                self.close_position(pos, price, why)
            except Exception as exc:
                self.log(f"close_all failed on {pos['symbol']}: {exc}", "error")
