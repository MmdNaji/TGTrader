"""Bar-by-bar backtest of the rule strategies.

It calls the REAL ``RiskManager.size`` and applies the same fee-aware entry filter as the live
loop, because a backtest that is more permissive than the engine is worse than no backtest: it
green-lights a setting the engine will then refuse, or reports an edge the fees will eat.

One position per symbol, market fills at the next bar's open, stops and targets checked against
the bar's high/low, both fees and slippage applied. Good enough to tell a rule that loses money
from one that does not; not a substitute for paper trading before real money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..config import RiskSettings
from ..risk.manager import RiskManager
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
    init_stop: float = 0.0     # the stop the trade was opened with; R is measured from this
    part_pnl: float = 0.0      # booked by a scale-out, before the rest of the trade closed
    scaled: bool = False


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


def engine_params(settings) -> dict:
    """Everything about the LIVE engine that changes what a backtest would do.

    One place on purpose. These arguments were being passed differently by the Backtest page,
    the self-test page and the CLI, so the same app reported three different backtests for one
    set of settings - and the scalp preset was measured against a strategy list that did not
    include Scalp, which is the only strategy that preset exists for.
    """
    from ..strategy.builtin import DEFAULT_STRATEGIES, Scalp
    agg = getattr(settings, "aggressiveness", "normal")
    return {
        "min_confidence": {"high": 0.4, "scalp": 0.0}.get(agg, 0.55),
        "position_pct": getattr(settings, "position_pct", 0.0),
        "strategies": ([Scalp()] + list(DEFAULT_STRATEGIES)) if agg == "scalp" else list(DEFAULT_STRATEGIES),
    }


def run_backtest(symbol: str, df: pd.DataFrame, risk: RiskSettings, start_equity: float = 1000.0,
                 strategies: list[Strategy] | None = None, fee_rate: float = 0.001, slippage: float = 0.0005,
                 warmup: int = 60, allow_short: bool = True,
                 leader_regimes: pd.Series | None = None, min_confidence: float = 0.0,
                 position_pct: float = 0.0, cooldown_bars: float = 2.0,
                 partial_at_r: float = 0.0, partial_frac: float = 0.5) -> BtResult:
    """``leader_regimes`` is the market leader's (Bitcoin's) regime per timestamp. When given,
    a long is refused while the leader is in ``trend_down`` and a short while it is in
    ``trend_up`` - the same filter the live engine applies, so it can be measured rather than
    assumed.

    ``min_confidence`` is the live engine's confidence gate (0.55 on "normal", 0.4 on "high",
    0 on "scalp"). Without it the backtest trades signals the engine refuses, which is how a
    backtest ends up describing a strategy nobody is running.

    ``partial_at_r`` sells ``partial_frac`` of the position once it is that many R in profit
    and moves the stop to break-even, letting the rest run. OFF by default. It is here to be
    MEASURED, not because it is known to help: it should raise the win rate, because a trade
    that reaches 1R books something either way, and it should cut the big winners short, which
    is where a trend system makes its money. Which of those wins is a question for the numbers.

    Within one bar the order of events is unknowable, so the stop is checked BEFORE the
    scale-out: if a bar's range covers both, the trade is assumed to have lost. Assuming the
    other way makes every ambiguous bar a win and is how a scale-out flatters itself.

    ``position_pct`` mirrors the setting of the same name. It matters more than it looks:
    sizing by notional ignores the stop distance, and measured over 8 coins it turned -4% into
    -29%. The page has to show that rather than quietly backtesting a different sizing rule."""
    data = enrich(df)
    res = BtResult(symbol=symbol, bars=len(data), start_equity=start_equity)
    equity = start_equity
    open_t: BtTrade | None = None
    pending: tuple[Any, float] | None = None   # (signal, stop_distance) to fill at next open
    strategies = strategies or DEFAULT_STRATEGIES
    rr, atr_mult = risk.reward_risk, risk.atr_stop_mult
    rm = RiskManager(risk, None, "backtest")     # the same sizing code the live loop runs
    cooldown_until = -1                          # bar index before which no new entry is taken

    for i in range(warmup, len(data)):
        bar = data.iloc[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

        # 1. fill a pending entry at this bar's open
        if pending and open_t is None:
            sig, sd = pending
            px = o * (1 + slippage) if sig.side == "long" else o * (1 - slippage)
            sizing = rm.size(sig.side, px, sd, equity, position_pct=position_pct)
            if sizing:
                qty = sizing.qty
                stop = px - sd if sig.side == "long" else px + sd
                tp = px + rr * sd if sig.side == "long" else px - rr * sd
                entry_fee = qty * px * fee_rate
                equity -= entry_fee
                open_t = BtTrade(sig.side, px, stop, tp, qty, i, sig.strategy, reason=sig.reason,
                                 entry_fee=entry_fee, init_stop=stop)
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
            if exit_px is None:
                # The engine closes a trend trade when the regime flips against it, on every
                # pass. Without it here the backtest models a system that holds every trade to
                # its stop or its target, which is not the system being run.
                reg_now = detect_regime(data.iloc[: i + 1])
                if (t.side == "long" and reg_now == "trend_down") or \
                   (t.side == "short" and reg_now == "trend_up"):
                    exit_px, why = c, "regime flipped"
            if exit_px is None and partial_at_r > 0 and not t.scaled:
                r_dist0 = abs(t.entry - (t.init_stop or t.stop))
                mark = (t.entry + partial_at_r * r_dist0) if t.side == "long" \
                    else (t.entry - partial_at_r * r_dist0)
                hit = (h >= mark) if t.side == "long" else (l <= mark)
                if r_dist0 and hit:
                    px_out = mark * (1 - slippage) if t.side == "long" else mark * (1 + slippage)
                    part_qty = t.qty * partial_frac
                    gain = (px_out - t.entry) * part_qty if t.side == "long" \
                        else (t.entry - px_out) * part_qty
                    # the entry fee was charged on the whole position; this half pays its share
                    t.part_pnl = gain - part_qty * px_out * fee_rate - t.entry_fee * partial_frac
                    t.qty -= part_qty
                    t.entry_fee *= (1 - partial_frac)
                    t.stop = t.entry          # the rest cannot lose money from here
                    t.scaled = True
                    equity += t.part_pnl
            if exit_px is None:
                # R from the ORIGINAL stop. Measuring it from the already-trailed stop shrinks it
                # every bar, so the stop walks into the price and closes every winner for nothing.
                t.stop = rm.trail_stop(t.side, t.entry, t.stop, c, t.init_stop or t.stop)
            if exit_px is not None:
                # Exits pay slippage too. Filling every stop at exactly the stop price makes
                # the worst fills in the sample free, which is precisely backwards: a stop is
                # hit in a fast move, and that is when the fill is worst.
                exit_px = exit_px * (1 - slippage) if t.side == "long" else exit_px * (1 + slippage)
                pnl = (exit_px - t.entry) * t.qty if t.side == "long" else (t.entry - exit_px) * t.qty
                pnl -= t.qty * exit_px * fee_rate + t.entry_fee   # both fees belong to the trade
                pnl += t.part_pnl          # whatever a scale-out already banked
                t.exit, t.exit_i, t.pnl = exit_px, i, pnl
                # R against the risk the trade was OPENED with, not what is left of it after a
                # scale-out - otherwise selling half doubles the reported R of the same move.
                r_dist = abs(t.entry - (t.init_stop or t.stop))
                full_qty = t.qty / (1 - partial_frac) if (t.scaled and partial_frac < 1) else t.qty
                t.r = pnl / (full_qty * r_dist) if r_dist else 0.0
                t.reason += f" -> {why}"
                equity += pnl + t.entry_fee   # entry fee was already taken from equity when the trade opened
                res.trades.append(t)
                open_t = None
                if why == "stop":
                    # The engine sits out COOLDOWN_BARS after a stop-out; without the same rule
                    # here the backtest re-enters the losing idea on the next bar and reports a
                    # trade count the engine will never produce.
                    cooldown_until = i + int(cooldown_bars)

        # 3. look for a new signal on the closed bar
        if open_t is None and pending is None and i >= cooldown_until:
            window = data.iloc[: i + 1]
            regime = detect_regime(window)
            if regime not in ("volatile", "unknown"):
                sigs = evaluate_all(symbol, window, regime, strategies)
                sigs = [s for s in sigs if allow_short or s.side == "long"]
                if min_confidence > 0:
                    sigs = [s for s in sigs if s.strength >= min_confidence]
                if sigs and leader_regimes is not None:
                    lr = leader_regimes.get(window.index[-1])
                    if lr == "trend_down":
                        sigs = [x for x in sigs if x.side == "short"]
                    elif lr == "trend_up":
                        sigs = [x for x in sigs if x.side == "long"]
                if sigs:
                    sig = max(sigs, key=lambda s: s.strength)
                    sd = sig.stop_distance or atr_mult * float(bar["atr14"])
                    # The same two cost gates the live engine applies, so a backtest cannot
                    # promise trades the engine would refuse.
                    sd = max(sd or 0.0, c * fee_rate * 4.0)
                    round_trip = 2.0 * fee_rate * c
                    if sd > 0 and rr * sd > 3.0 * round_trip:
                        pending = (sig, sd)

        # mark to market
        mtm = equity
        if open_t:
            mtm += (c - open_t.entry) * open_t.qty if open_t.side == "long" else (open_t.entry - c) * open_t.qty
        res.equity.append(mtm)

    return res
