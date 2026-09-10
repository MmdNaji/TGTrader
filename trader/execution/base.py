"""Broker interface. Paper, ccxt (crypto API), MT5 (forex) and the computer-use
executor all present the same four calls so the engine does not care which is live."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    symbol: str
    side: str        # buy | sell
    qty: float
    price: float
    fee: float = 0.0
    order_id: str = ""


class Broker:
    name = "base"

    def equity(self, prices: dict[str, float]) -> float:  # total value in quote currency
        raise NotImplementedError

    def cash(self) -> float:
        raise NotImplementedError

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float,
                     close: bool = False) -> Fill:
        """close=True means "reduce/flatten an existing position". A broker must never turn a
        close into a new opposite position - that is how a flat account ends up short."""
        raise NotImplementedError

    def limits(self, symbol: str) -> tuple[float, float]:
        """(min_qty, qty_step) for the symbol; (0, 0) when unknown."""
        return 0.0, 0.0

    def supports_short(self) -> bool:
        return False
