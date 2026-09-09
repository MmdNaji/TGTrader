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

    def cash(self) -> float:
        info = self.mt5.account_info()
        return float(info.margin_free) if info else 0.0

    def equity(self, prices: dict[str, float]) -> float:
        info = self.mt5.account_info()
        return float(info.equity) if info else 0.0

    def limits(self, symbol: str) -> tuple[float, float]:
        si = self.mt5.symbol_info(symbol.replace("/", ""))
        return (float(si.volume_min), float(si.volume_step)) if si else (0.01, 0.01)

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float) -> Fill:
        mt5 = self.mt5
        sym = symbol.replace("/", "")
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "buy" else tick.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(qty),
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": 777001,
            "comment": "TGTrader",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 order failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
        return Fill(symbol, side, float(res.volume), float(res.price), 0.0, order_id=str(res.order))
