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
        t = time.gmtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0)) - time.timezone

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
    def size(self, side: str, price: float, stop_distance: float, equity: float,
             min_qty: float = 0.0, qty_step: float = 0.0) -> Sizing | None:
        """Position size from the money at risk, never from conviction.

        risk_amount = risk_per_trade * min(equity, capital_limit)
        qty         = risk_amount / stop_distance
        capped so the notional never exceeds max_position_frac * capital_limit.
        """
        if price <= 0 or stop_distance <= 0:
            return None
        base = min(equity, self.risk.capital_limit)
        risk_amount = self.risk.risk_per_trade * base
        qty = risk_amount / stop_distance
        max_notional = self.risk.max_position_frac * self.risk.capital_limit
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
    def trail_stop(self, side: str, entry: float, stop: float, price: float) -> float:
        """Move the stop to break-even and then trail it, once the trade is trail_after_r in profit."""
        if self.risk.trail_after_r <= 0:
            return stop
        r = abs(entry - stop)
        if r <= 0:
            return stop
        if side == "long":
            if price >= entry + self.risk.trail_after_r * r:
                return max(stop, price - r)
        else:
            if price <= entry - self.risk.trail_after_r * r:
                return min(stop, price + r)
        return stop
