"""Paper broker: fills at the given price with a small slippage and fee, keeps cash in the DB settings table."""
from __future__ import annotations

import json
import time

from .base import Broker, Fill
from ..config import data_dir


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, start_balance: float, fee_rate: float = 0.001, slippage: float = 0.0005):
        self.fee_rate = fee_rate
        self.slippage = slippage
        self._state_file = data_dir() / "paper_state.json"
        self._positions: dict[str, dict] = {}
        self._cash = start_balance
        self._load(start_balance)

    def _load(self, start_balance: float) -> None:
        if self._state_file.exists():
            try:
                st = json.loads(self._state_file.read_text())
                self._cash = float(st.get("cash", start_balance))
                self._positions = st.get("positions", {})
            except Exception:
                pass

    def _save(self) -> None:
        self._state_file.write_text(json.dumps({"cash": self._cash, "positions": self._positions, "ts": time.time()}))

    def reset(self, start_balance: float) -> None:
        self._cash = start_balance
        self._positions = {}
        self._save()

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, dict]:
        return dict(self._positions)

    def equity(self, prices: dict[str, float]) -> float:
        eq = self._cash
        for sym, p in self._positions.items():
            px = prices.get(sym, p["price"])
            eq += p["qty"] * px if p["side"] == "long" else p["qty"] * (2 * p["price"] - px)
        return eq

    def supports_short(self) -> bool:
        return True

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float) -> Fill:
        px = price_hint * (1 + self.slippage) if side == "buy" else price_hint * (1 - self.slippage)
        fee = qty * px * self.fee_rate
        pos = self._positions.get(symbol)
        if pos is None:
            # opening
            if side == "buy":
                cost = qty * px + fee
                if cost > self._cash:
                    raise RuntimeError(f"paper: insufficient cash ({self._cash:.2f} < {cost:.2f})")
                self._cash -= cost
                self._positions[symbol] = {"side": "long", "qty": qty, "price": px}
            else:
                # short: reserve the notional as margin
                margin = qty * px
                if margin + fee > self._cash:
                    raise RuntimeError("paper: insufficient cash for short margin")
                self._cash -= margin + fee
                self._positions[symbol] = {"side": "short", "qty": qty, "price": px}
        else:
            # closing (the engine always closes the whole position)
            if pos["side"] == "long" and side == "sell":
                self._cash += qty * px - fee
            elif pos["side"] == "short" and side == "buy":
                self._cash += pos["qty"] * pos["price"] + (pos["price"] - px) * qty - fee
            else:
                raise RuntimeError("paper: adding to a position is not supported")
            del self._positions[symbol]
        self._save()
        return Fill(symbol, side, qty, px, fee, order_id=f"paper-{int(time.time()*1000)}")
