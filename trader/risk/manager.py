"""Risk manager - the part of the bot that is never allowed to be clever.

Every order passes through ``check`` and ``size``. Nothing the strategies or the
LLM say can raise the limits set here; they can only decide not to trade.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..config import RiskSettings, data_dir


@dataclass
class Sizing:
    qty: float
    stop_price: float
    take_profit: float
    risk_amount: float
    notional: float


class RiskManager:
    def __init__(self, risk: RiskSettings, db, mode: str):
        self.risk = risk
        self.db = db
        self.mode = mode
        self._kill_file: Path = data_dir() / "KILL_SWITCH"

    # ------------------------------------------------------------ kill switch
    def kill_switch_on(self) -> bool:
        return self._kill_file.exists()

    def set_kill_switch(self, on: bool) -> None:
        if on:
            self._kill_file.write_text(str(time.time()))
        elif self._kill_file.exists():
            self._kill_file.unlink()

    # ------------------------------------------------------------ daily loss
    def day_start(self) -> float:
        """UTC midnight, computed straight from the epoch.

        The previous version built a tuple from gmtime, passed it to mktime (which reads a tuple
        as LOCAL time) and then subtracted time.timezone to undo that. It lands on the right
        second in a fixed-offset zone and is off by an hour in a DST one - which silently moves
        the daily loss limit's reset by an hour twice a year. The epoch has no such ambiguity."""
        now = time.time()
        return now - (now % 86400.0)

    def daily_pnl(self) -> float:
        return self.db.pnl_since(self.mode, self.day_start())

    def daily_loss_hit(self) -> bool:
        return self.daily_pnl() <= -abs(self.risk.max_daily_loss * self.risk.capital_limit)

    # ------------------------------------------------------------ gate
    def check(self, symbol: str, open_positions: list, equity: float) -> str | None:
        """Return a reason to refuse, or None if a new trade is allowed."""
        if self.kill_switch_on():
            return "kill switch is on"
        if self.daily_loss_hit():
            return f"daily loss limit reached ({self.daily_pnl():.2f})"
        if len(open_positions) >= self.risk.max_open_positions:
            return f"max open positions ({self.risk.max_open_positions}) reached"
        if any(p["symbol"] == symbol for p in open_positions):
            return f"already in a position on {symbol}"
        if equity <= 0:
            return "no equity"
        return None

    # ------------------------------------------------------------ sizing
    def open_risk(self, open_positions: list) -> float:
        """Money currently at risk across every open position, measured to its ORIGINAL stop.

        Ten positions each correctly sized at 1% is a 10% bet on one market, and crypto is
        correlated enough that a bad hour hits all of them together."""
        total = 0.0
        for p in open_positions or []:
            try:
                entry = float(p["entry_price"])
                stop = float(p.get("init_stop") or p.get("stop_price") or 0.0)
                qty = float(p["qty"])
            except (KeyError, TypeError, ValueError):
                continue
            if stop and qty:
                total += abs(entry - stop) * qty
        return total

    def size(self, side: str, price: float, stop_distance: float, equity: float,
             min_qty: float = 0.0, qty_step: float = 0.0, cash: float | None = None,
             position_pct: float = 0.0, open_positions: list | None = None) -> Sizing | None:
        """Position size from the money at risk, never from conviction.

        risk_amount = risk_per_trade * min(equity, capital_limit)
        qty         = risk_amount / stop_distance
        capped so the notional never exceeds max_position_frac * capital_limit.
        """
        if price <= 0 or stop_distance <= 0:
            return None
        base = min(equity, self.risk.capital_limit)
        hard_cap = self.risk.max_position_frac * self.risk.capital_limit
        if position_pct and position_pct > 0:
            # explicit "spend this percent of capital on each trade" - still inside the hard caps:
            # it scales with the account (base, not capital_limit), never exceeds max_position_frac,
            # and can never risk more than risk_per_trade if the stop is hit.
            max_notional = min((position_pct / 100.0) * base, hard_cap)
            qty = max_notional / price
            # An explicit percent deliberately overrides risk_per_trade - that is what the knob is
            # for - but one trade must still never be able to lose the WHOLE daily budget.
            worst_loss_cap = abs(self.risk.max_daily_loss * self.risk.capital_limit)
            if worst_loss_cap > 0:
                qty = min(qty, worst_loss_cap / stop_distance)
            max_notional = min(max_notional, qty * price)
        else:
            # automatic: size from the money at risk and the stop distance
            risk_amount = self.risk.risk_per_trade * base
            qty = risk_amount / stop_distance
            max_notional = hard_cap
        # Total open risk across every position at once. Deliberately its own setting and NOT
        # the daily loss limit: that one is about losses already realised today, this is about
        # how much can be lost simultaneously if a correlated market takes every stop together.
        risk_budget = abs(getattr(self.risk, "max_open_risk", 0.0) * self.risk.capital_limit)
        if risk_budget > 0:
            left = risk_budget - self.open_risk(open_positions or [])
            want = qty * stop_distance
            # Refuse rather than shrink to a token size. A position sized down to a fraction of
            # normal still pays a full round trip in fees, and shrinking makes a trade's size
            # depend on what happened to be open when it arrived rather than on the setup.
            if left < min(want, self.risk.risk_per_trade * base) * 0.5:
                return None
            if want > left:
                qty = left / stop_distance
                max_notional = min(max_notional, qty * price)
        if cash is not None and cash > 0:
            # never try to spend more than is actually available (leave room for fee + slippage)
            max_notional = min(max_notional, cash * 0.97)
        if qty * price > max_notional:
            qty = max_notional / price
        if qty_step > 0:
            qty = (qty // qty_step) * qty_step
        if qty <= 0 or (min_qty and qty < min_qty):
            return None
        if side == "long":
            stop = price - stop_distance
            tp = price + self.risk.reward_risk * stop_distance
        else:
            stop = price + stop_distance
            tp = price - self.risk.reward_risk * stop_distance
        return Sizing(qty=qty, stop_price=stop, take_profit=tp,
                      risk_amount=qty * stop_distance, notional=qty * price)

    # ------------------------------------------------------------ trailing
    def trail_stop(self, side: str, entry: float, stop: float, price: float,
                   init_stop: float | None = None) -> float:
        """Move the stop to break-even and then trail it, once the trade is trail_after_r in profit.

        R must be measured from the trade's ORIGINAL stop. Measuring it from the stop that was
        already trailed shrinks R on every pass, so the stop walks into the price and closes
        every winner for almost nothing."""
        if self.risk.trail_after_r <= 0:
            return stop
        r = abs(entry - (init_stop if init_stop else stop))
        if r <= 0:
            return stop
        if side == "long":
            if price >= entry + self.risk.trail_after_r * r:
                return max(stop, price - r)
        else:
            if price <= entry - self.risk.trail_after_r * r:
                return min(stop, price + r)
        return stop
