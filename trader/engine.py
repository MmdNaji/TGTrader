"""The trading loop.

Every ``loop_seconds`` for every symbol:
  candles -> indicators -> regime -> rule signals -> (optional) Claude decision using skills
  -> risk gate and sizing -> broker -> journal.
Open positions are managed every loop: stop, target, trailing stop.
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
from .strategy.builtin import evaluate_all
from .strategy.regime import detect_regime


class Engine:
    def __init__(self, settings: Settings, db: Database, broker: Broker | None = None,
                 brain: Any | None = None, on_event: Callable[[str], None] | None = None):
        self.settings = settings
        self.db = db
        self.mode = settings.mode
        self.market = MarketData(settings)
        self.risk = RiskManager(settings.risk, db, self.mode)
        self.broker = broker or self._make_broker()
        self.brain = brain
        self.on_event = on_event or (lambda s: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_prices: dict[str, float] = {}
        self.status: dict[str, Any] = {"running": False, "last_loop": 0.0, "error": ""}
        load_seed_skills(db)

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

    # ------------------------------------------------------------ thread control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        self.status["running"] = True
        self.log(f"engine started: mode={self.mode} market={self.settings.market} symbols={self.settings.symbols}")
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

    # ------------------------------------------------------------ one pass
    def loop_once(self) -> None:
        open_positions = [dict(r) for r in self.db.open_trades(self.mode)]
        for symbol in self.settings.symbols:
            if self._stop.is_set():
                return
            df = enrich(self.market.candles(symbol, limit=400))
            price = float(df["close"].iloc[-1])
            self.last_prices[symbol] = price
            pos = next((p for p in open_positions if p["symbol"] == symbol), None)
            if pos:
                self._manage(pos, df, price)
                continue
            self._consider_entry(symbol, df, price, open_positions)
        try:
            self.db.record_equity(self.mode, self.broker.equity(self.last_prices))
        except Exception as exc:
            self.log(f"equity read failed: {exc}", "warn")

    # ------------------------------------------------------------ entries
    def _consider_entry(self, symbol: str, df, price: float, open_positions: list[dict]) -> None:
        regime = detect_regime(df)
        signals = evaluate_all(symbol, df, regime)
        if not self.broker.supports_short():
            signals = [s for s in signals if s.side == "long"]
        snap = snapshot(df)
        equity = self.broker.equity(self.last_prices)

        refuse = self.risk.check(symbol, open_positions, equity)
        if refuse:
            if signals:
                self.db.add_decision(symbol, "hold", None, "risk", refuse, {"regime": regime})
            return

        decision: dict[str, Any] | None = None
        source = "rules"
        if self.settings.use_llm_for_decisions and self.brain is not None and (signals or regime in ("trend_up", "trend_down")):
            try:
                knowledge = [r["content"] for r in self.db.search_knowledge(f"{regime} {' '.join(s.strategy for s in signals)}", limit=3)]
                decision = self.brain.decide(
                    symbol, snap, regime,
                    [{"strategy": s.strategy, "side": s.side, "strength": s.strength, "reason": s.reason} for s in signals],
                    None, skills_prompt_block(self.db), knowledge,
                )
                source = "llm"
            except Exception as exc:
                self.log(f"LLM decision failed for {symbol}: {exc}", "warn")
                decision = None

        if decision is None:
            if not signals:
                return
            best = max(signals, key=lambda s: s.strength)
            decision = {"action": "buy" if best.side == "long" else "sell", "confidence": best.strength,
                        "reason": best.reason, "stop_distance_atr": self.settings.risk.atr_stop_mult,
                        "skills_used": [], "strategy": best.strategy}

        action = decision["action"]
        payload = {"regime": regime, "snapshot": snap, "signals": [s.reason for s in signals], "decision": decision}
        if action not in ("buy", "sell") or decision["confidence"] < 0.55:
            self.db.add_decision(symbol, "hold", decision.get("confidence"), source, decision.get("reason", ""), payload)
            return
        side = "long" if action == "buy" else "short"
        if side == "short" and not self.broker.supports_short():
            self.db.add_decision(symbol, "hold", decision["confidence"], source, "short not supported by broker", payload)
            return

        atr_v = snap.get("atr14") or 0.0
        stop_distance = float(decision.get("stop_distance_atr", 2.0)) * atr_v
        if stop_distance <= 0:
            return
        min_qty, step = self.broker.limits(symbol)
        sizing = self.risk.size(side, price, stop_distance, equity, min_qty, step)
        if sizing is None:
            self.db.add_decision(symbol, "hold", decision["confidence"], "risk",
                                 "position too small for the exchange minimum or the capital limit", payload)
            return
        strategy = decision.get("strategy") or ("llm" if source == "llm" else "rules")
        self.db.add_decision(symbol, action, decision["confidence"], source, decision.get("reason", ""), payload)
        try:
            fill = self.broker.market_order(symbol, "buy" if side == "long" else "sell", sizing.qty, price)
        except Exception as exc:
            self.log(f"order failed on {symbol}: {exc}", "error")
            return
        # stops are recomputed from the actual fill price
        stop = fill.price - stop_distance if side == "long" else fill.price + stop_distance
        tp = fill.price + self.settings.risk.reward_risk * stop_distance if side == "long" \
            else fill.price - self.settings.risk.reward_risk * stop_distance
        tid = self.db.open_trade(self.mode, symbol, side, fill.qty, fill.price, stop, tp, strategy, decision.get("reason", ""))
        open_positions.append({"id": tid, "symbol": symbol, "side": side})
        self.log(f"OPEN {side} {symbol} qty={fill.qty:g} @ {fill.price:g} stop={stop:g} tp={tp:g} ({strategy})")

    # ------------------------------------------------------------ position management
    def _manage(self, pos: dict, df, price: float) -> None:
        side, entry, stop, tp = pos["side"], float(pos["entry_price"]), float(pos["stop_price"]), float(pos["take_profit"] or 0)
        bar_h, bar_l = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
        why = None
        if side == "long":
            if bar_l <= stop or price <= stop:
                why = "stop"
            elif tp and (bar_h >= tp or price >= tp):
                why = "target"
        else:
            if bar_h >= stop or price >= stop:
                why = "stop"
            elif tp and (bar_l <= tp or price <= tp):
                why = "target"
        if why is None:
            # trend-change exit for trend trades
            regime = detect_regime(df)
            if (side == "long" and regime == "trend_down") or (side == "short" and regime == "trend_up"):
                why = "regime flipped"
        if why is None:
            new_stop = self.risk.trail_stop(side, entry, stop, price)
            if new_stop != stop:
                self.db.update_stop(pos["id"], new_stop)
                self.log(f"trail {pos['symbol']} stop {stop:g} -> {new_stop:g}")
            return
        self.close_position(pos, price, why)

    def close_position(self, pos: dict, price: float, why: str) -> None:
        side = pos["side"]
        try:
            fill = self.broker.market_order(pos["symbol"], "sell" if side == "long" else "buy", float(pos["qty"]), price)
        except Exception as exc:
            self.log(f"close failed on {pos['symbol']}: {exc}", "error")
            return
        pnl = (fill.price - float(pos["entry_price"])) * fill.qty if side == "long" \
            else (float(pos["entry_price"]) - fill.price) * fill.qty
        pnl -= fill.fee
        r_dist = abs(float(pos["entry_price"]) - float(pos["stop_price"])) if pos.get("stop_price") else 0.0
        r = pnl / (fill.qty * r_dist) if r_dist else None
        self.db.close_trade(pos["id"], fill.price, pnl, r)
        self.db.add_decision(pos["symbol"], "close", None, "risk", why, {"pnl": pnl})
        self.log(f"CLOSE {side} {pos['symbol']} @ {fill.price:g} pnl={pnl:+.4f} ({why})")

    def close_all(self, why: str = "manual") -> None:
        for pos in [dict(r) for r in self.db.open_trades(self.mode)]:
            price = self.last_prices.get(pos["symbol"]) or self.market.price(pos["symbol"])
            self.close_position(pos, price, why)
