"""Risk manager - the part of the bot that is never allowed to be clever.

Every order passes through ``check`` and ``size``. Nothing the strategies or the
LLM say can raise the limits set here; they can only decide not to trade.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..config import RiskSettings, data_dir


# The smallest position worth opening, as a fraction of the capital limit.
MIN_NOTIONAL_FRAC = 0.02


def _field(row, name: str):
    """Read a column from a dict OR a sqlite3.Row.

    sqlite3.Row supports row["x"] and raises IndexError for a missing key, but has no .get().
    Rows arrive here straight from db.open_trades() in some call paths and as dicts in others,
    and a .get() on the first crashed the dashboard refresh.
    """
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


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
                entry = float(_field(p, "entry_price") or 0.0)
                stop = float(_field(p, "init_stop") or _field(p, "stop_price") or 0.0)
                qty = float(_field(p, "qty") or 0.0)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if stop and qty:
                total += abs(entry - stop) * qty
        return total

    def capacity(self, open_positions: list) -> tuple[int, str]:
        """How many positions can REALLY be open, and what decides it.

        The dashboard was showing "of at most 20" while the simultaneous-risk budget allowed
        11, so it advertised a number the bot could never reach. This measures the ceiling from
        the risk each OPEN position is actually carrying, rather than from a setting or a guess
        about a stop distance nobody has taken yet.
        """
        cap = int(self.risk.max_open_positions)
        budget = abs(getattr(self.risk, "max_open_risk", 0.0) * self.risk.capital_limit)
        held = list(open_positions or [])
        if budget <= 0 or not held:
            return cap, "تنظیمات"
        used = self.open_risk(held)
        if used <= 0:
            return cap, "تنظیمات"
        per = used / len(held)                      # what one position is costing, measured
        by_risk = int(budget / per)
        if by_risk < cap:
            return max(by_risk, len(held)), "سقف ریسک همزمان"
        return cap, "تنظیمات"

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
            # Explicit "spend this percent of capital on each trade". It stays inside the hard
            # caps - it scales with the account (base, not capital_limit) and never exceeds
            # max_position_frac - but it deliberately OVERRIDES risk_per_trade, which is the
            # whole point of the knob and is why it is the most damaging setting in the app.
            # The only loss ceiling left on this path is the daily budget, applied below; at the
            # shipped defaults that is three times risk_per_trade, not equal to it.
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
        if cash is not None and cash > 0:
            # never try to spend more than is actually available (leave room for fee + slippage)
            max_notional = min(max_notional, cash * 0.97)
        if qty * price > max_notional:
            qty = max_notional / price

        # Total open risk across every position at once. Deliberately its own setting and NOT
        # the daily loss limit: that one is about losses already realised today, this is about
        # how much can be lost simultaneously if a correlated market takes every stop together.
        #
        # It runs HERE, after every other cap, because it has to judge the position that will
        # actually be traded. Run earlier it saw the raw risk_per_trade size - and with a
        # capital limit that caps the notional far below it, that number is fiction. Measured
        # on the owner's own settings (risk 10%, open-risk cap 6%, capital 1000): the real
        # second position risked $28 of a $60 budget with $31 free, and was refused because the
        # check was comparing against an imaginary $100. One position, for hours, and nothing
        # said why.
        risk_budget = abs(getattr(self.risk, "max_open_risk", 0.0) * self.risk.capital_limit)
        if risk_budget > 0:
            left = risk_budget - self.open_risk(open_positions or [])
            want = qty * stop_distance
            if want > left:
                # Trim to what is left, then refuse if the remainder is not worth a round trip:
                # a token position pays full fees for a fraction of the edge.
                if left < want * 0.5:
                    return None
                qty = left / stop_distance

        if qty_step > 0:
            qty = (qty // qty_step) * qty_step
        if qty <= 0 or (min_qty and qty < min_qty):
            return None
        # A dust position is not a small trade, it is a pointless one. Once the cash is nearly
        # spent the caps above happily produce a $15 or a $0.40 position: it pays a full round
        # trip and two spreads to put a rounding error to work, and it clutters the journal
        # with trades that cannot move the account either way.
        if qty * price < MIN_NOTIONAL_FRAC * self.risk.capital_limit:
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
