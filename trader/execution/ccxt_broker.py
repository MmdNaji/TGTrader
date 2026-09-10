"""Live crypto execution through a ccxt exchange (spot; long only unless the exchange
account is a margin/futures one, which is deliberately not enabled here)."""
from __future__ import annotations

from .base import Broker, Fill
from ..config import Settings
from ..market.data import _ccxt_exchange


class CcxtBroker(Broker):
    name = "ccxt"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ex = _ccxt_exchange(settings, authenticated=True)
        self.ex.load_markets()

    def _quote(self, symbol: str) -> str:
        return symbol.split("/")[1].split(":")[0]

    def cash(self) -> float:
        bal = self.ex.fetch_balance()
        quotes = {self._quote(s) for s in self.settings.symbols}
        return float(sum(float(bal.get(q, {}).get("free", 0) or 0) for q in quotes))

    def equity(self, prices: dict[str, float]) -> float:
        bal = self.ex.fetch_balance()
        total = 0.0
        quotes = {self._quote(s) for s in self.settings.symbols}
        for q in quotes:
            total += float(bal.get(q, {}).get("total", 0) or 0)
        for sym, px in prices.items():
            base = sym.split("/")[0]
            total += float(bal.get(base, {}).get("total", 0) or 0) * px
        return total

    def limits(self, symbol: str) -> tuple[float, float]:
        m = self.ex.market(symbol)
        min_qty = float((m.get("limits", {}).get("amount", {}) or {}).get("min") or 0)
        step = float(m.get("precision", {}).get("amount") or 0)
        # ccxt precision may be number of decimals (int) rather than a step size
        if step and step >= 1:
            step = 10 ** (-int(step))
        return min_qty, step

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float,
                     close: bool = False) -> Fill:
        amount = float(self.ex.amount_to_precision(symbol, qty))
        order = self.ex.create_order(symbol, "market", side, amount)
        # Some exchanges return the fill lazily; fetch it once to get the average price.
        try:
            order = self.ex.fetch_order(order["id"], symbol)
        except Exception:
            pass
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            # Never report a fill the exchange did not make. Falling back to the requested
            # amount opened a journal position that does not exist, and the next close then
            # tried to sell coins that were never bought.
            raise RuntimeError(f"{symbol}: exchange reported no fill (status={order.get('status')})")
        price = float(order.get("average") or order.get("price") or price_hint)
        # Fee.cost carries a CURRENCY, and on a spot buy most exchanges charge it in the BASE
        # asset - so the number is a quantity of coins, not money. Everything above this layer
        # subtracts Fill.fee from a quote-currency P&L, so a base-currency fee must be
        # converted at the fill price, and a fee in some third currency (a BNB/KCS discount)
        # is not a cost against this trade's quote balance at all.
        fee = 0.0
        f = order.get("fee") or {}
        cost = f.get("cost")
        if cost:
            cur = (f.get("currency") or "").upper()
            quote = self._quote(symbol).upper()
            base = symbol.split("/")[0].upper()
            if cur in ("", quote):
                fee = abs(float(cost))
            elif cur == base:
                fee = abs(float(cost)) * price
            # any other currency: the trade's quote balance did not pay it, so it is not
            # this trade's cost. Left at 0 deliberately rather than guessing a rate.
        return Fill(symbol, side, filled, price, fee, order_id=str(order.get("id", "")))
