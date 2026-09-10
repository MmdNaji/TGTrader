"""Forex execution through the official MetaTrader 5 Python package (Windows only)."""
from __future__ import annotations

from .base import Broker, Fill
from ..config import Settings


class Mt5Broker(Broker):
    name = "mt5"

    def __init__(self, settings: Settings):
        import MetaTrader5 as mt5  # type: ignore
        self.mt5 = mt5
        self.settings = settings
        if not mt5.initialize(login=settings.mt5_login or None, password=settings.mt5_password or None,
                              server=settings.mt5_server or None):
            raise RuntimeError(f"MetaTrader5 initialize failed: {mt5.last_error()}")

    def supports_short(self) -> bool:
        return True

    def _contract(self, sym: str) -> float:
        """Units per lot. EURUSD is 100,000, so a "qty" of 10,000 units is 0.1 lots - sending
        10,000 as the volume would be a 1,000,000,000 unit order. The engine sizes in units
        because that is what a price times a quantity means; MT5 trades in lots."""
        si = self.mt5.symbol_info(sym)
        return float(getattr(si, "trade_contract_size", 0) or 0) or 100000.0

    def cash(self) -> float:
        info = self.mt5.account_info()
        return float(info.margin_free) if info else 0.0

    def equity(self, prices: dict[str, float]) -> float:
        info = self.mt5.account_info()
        return float(info.equity) if info else 0.0

    def limits(self, symbol: str) -> tuple[float, float]:
        """Reported in UNITS, the same currency as the qty the risk manager produces. Returning
        lots here made the sizing check compare 0.01 lots against tens of thousands of units, so
        every order passed the minimum-size test whatever its real size."""
        sym = symbol.replace("/", "")
        si = self.mt5.symbol_info(sym)
        if not si:
            return (0.0, 0.0)
        c = self._contract(sym)
        return (float(si.volume_min) * c, float(si.volume_step) * c)

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float,
                     close: bool = False) -> Fill:
        mt5 = self.mt5
        sym = symbol.replace("/", "")
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "buy" else tick.bid
        si = mt5.symbol_info(sym)
        contract = self._contract(sym)
        step = float(getattr(si, "volume_step", 0.01) or 0.01)
        vmin = float(getattr(si, "volume_min", step) or step)
        vmax = float(getattr(si, "volume_max", 0) or 0)
        lots = float(qty) / contract
        ticket = 0
        if close:
            # A plain opposite deal only nets out on a NETTING account. On a hedging account -
            # which is what most retail brokers hand out - it opens a second, opposite position
            # and the original stop is still sitting there. The ticket is what actually closes it.
            for pos in (mt5.positions_get(symbol=sym) or ()):
                if getattr(pos, "magic", 0) == 777001:
                    ticket = int(pos.ticket)
                    lots = min(lots, float(pos.volume))   # pos.volume is already in lots
                    break
            if not ticket:
                raise RuntimeError(f"MT5: no open {sym} position to close")
        # round DOWN to the broker's step, so rounding can never make an order bigger
        lots = (int(lots / step) * step) if step > 0 else lots
        lots = round(lots, 8)
        if vmax and lots > vmax:
            lots = vmax
        if lots < vmin:
            raise RuntimeError(f"MT5: {qty:g} units is {lots:g} lots, below this symbol's minimum {vmin:g}")
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": 777001,
            "comment": "TGTrader",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if ticket:
            req["position"] = ticket
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 order failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
        # The broker's commission and swap are on the DEAL, not on the order result.
        fee = 0.0
        try:
            deal = (mt5.history_deals_get(ticket=int(res.deal)) or ())
            for d in deal:
                fee += abs(float(getattr(d, "commission", 0.0))) + abs(float(getattr(d, "swap", 0.0)))
        except Exception:
            pass
        # Report the fill back in UNITS: everything above this layer - P&L, R, the journal -
        # multiplies quantity by price and would be out by the contract size otherwise.
        return Fill(symbol, side, float(res.volume) * contract, float(res.price), fee,
                    order_id=str(res.order))
