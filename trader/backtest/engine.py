"""Bar-by-bar backtest of the rule strategies with the same risk manager the live loop uses.

It is deliberately simple: one position per symbol, market fills at the next bar's open,
stops and targets checked against the bar's high/low, fees and slippage applied. Good
enough to tell a rule that loses money from one that does not; not a substitute for
paper trading before real money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..config import RiskSettings
from ..market.indicators import enrich
from ..strategy.base import Strategy
from ..strategy.builtin import DEFAULT_STRATEGIES, evaluate_all
from ..strategy.regime import detect_regime


@dataclass
class BtTrade:
    side: str
    entry: float
    stop: float
    tp: float
    qty: float
    entry_i: int
    strategy: str
    exit: float = 0.0
    exit_i: int = 0
    pnl: float = 0.0
    r: float = 0.0
    reason: str = ""
    entry_fee: float = 0.0


@dataclass
class BtResult:
    symbol: str
    bars: int
    trades: list[BtTrade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    start_equity: float = 0.0

    def stats(self) -> dict[str, Any]:
        n = len(self.trades)
        end = self.equity[-1] if self.equity else self.start_equity
        peak, mdd = self.start_equity, 0.0
        for e in self.equity:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak if peak else 0)
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [-t.pnl for t in self.trades if t.pnl < 0]
        rs = [t.r for t in self.trades]
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "trades": n,
            "win_rate": round(len(wins) / n, 3) if n else 0.0,
            "return_pct": round((end / self.start_equity - 1) * 100, 2) if self.start_equity else 0.0,
            "max_drawdown_pct": round(mdd * 100, 2),
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else (float("inf") if wins else 0.0),
            "avg_r": round(sum(rs) / n, 3) if n else 0.0,
            "expectancy": round(sum(t.pnl for t in self.trades) / n, 4) if n else 0.0,
        }


def run_backtest(symbol: str, df: pd.DataFrame, risk: RiskSettings, start_equity: float = 1000.0,
                 strategies: list[Strategy] | None = None, fee_rate: float = 0.001, slippage: float = 0.0005,
                 warmup: int = 60, allow_short: bool = True) -> BtResult:
    data = enrich(df)
    res = BtResult(symbol=symbol, bars=len(data), start_equity=start_equity)
    equity = start_equity
    open_t: BtTrade | None = None
    pending: tuple[Any, float] | None = None   # (signal, stop_distance) to fill at next open
    strategies = strategies or DEFAULT_STRATEGIES
    rr, atr_mult = risk.reward_risk, risk.atr_stop_mult

    for i in range(warmup, len(data)):
        bar = data.iloc[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

        # 1. fill a pending entry at this bar's open
        if pending and open_t is None:
            sig, sd = pending
            px = o * (1 + slippage) if sig.side == "long" else o * (1 - slippage)
            base = min(equity, risk.capital_limit)
            qty = (risk.risk_per_trade * base) / sd
            qty = min(qty, risk.max_position_frac * risk.capital_limit / px)
            if qty > 0:
                stop = px - sd if sig.side == "long" else px + sd
                tp = px + rr * sd if sig.side == "long" else px - rr * sd
                entry_fee = qty * px * fee_rate
                equity -= entry_fee
                open_t = BtTrade(sig.side, px, stop, tp, qty, i, sig.strategy, reason=sig.reason, entry_fee=entry_fee)
            pending = None

        # 2. manage the open trade on this bar
        if open_t:
            t = open_t
            exit_px, why = None, ""
            if t.side == "long":
                if l <= t.stop:
                    exit_px, why = t.stop, "stop"
                elif h >= t.tp:
                    exit_px, why = t.tp, "target"
            else:
                if h >= t.stop:
                    exit_px, why = t.stop, "stop"
                elif l <= t.tp:
                    exit_px, why = t.tp, "target"
            if exit_px is None and risk.trail_after_r > 0:
                r_dist = abs(t.entry - t.stop) or 1e-12
                if t.side == "long" and c >= t.entry + risk.trail_after_r * r_dist:
                    t.stop = max(t.stop, c - r_dist)
                elif t.side == "short" and c <= t.entry - risk.trail_after_r * r_dist:
                    t.stop = min(t.stop, c + r_dist)
            if exit_px is not None:
                pnl = (exit_px - t.entry) * t.qty if t.side == "long" else (t.entry - exit_px) * t.qty
                pnl -= t.qty * exit_px * fee_rate + t.entry_fee   # both fees belong to the trade
                t.exit, t.exit_i, t.pnl = exit_px, i, pnl
                r_dist = abs(t.entry - (t.entry - (t.tp - t.entry) / rr)) if rr else 1e-12
                t.r = pnl / (t.qty * r_dist) if r_dist else 0.0
                t.reason += f" -> {why}"
                equity += pnl + t.entry_fee   # entry fee was already taken from equity when the trade opened
                res.trades.append(t)
                open_t = None

        # 3. look for a new signal on the closed bar
        if open_t is None and pending is None:
            window = data.iloc[: i + 1]
            regime = detect_regime(window)
            if regime not in ("volatile", "unknown"):
                sigs = evaluate_all(symbol, window, regime, strategies)
                sigs = [s for s in sigs if allow_short or s.side == "long"]
                if sigs:
                    sig = max(sigs, key=lambda s: s.strength)
                    sd = sig.stop_distance or atr_mult * float(bar["atr14"])
                    if sd and sd > 0:
                        pending = (sig, sd)

        # mark to market
        mtm = equity
        if open_t:
            mtm += (c - open_t.entry) * open_t.qty if open_t.side == "long" else (open_t.entry - c) * open_t.qty
        res.equity.append(mtm)

    return res
