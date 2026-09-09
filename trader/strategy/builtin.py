"""Built-in rule strategies. Each is deliberately simple and well understood;
the point is not that any of them is a secret edge, but that the bot always
has a transparent, backtestable baseline the LLM layer can agree or disagree with.
"""
from __future__ import annotations

import pandas as pd

from .base import Signal, Strategy


def _ok(*vals) -> bool:
    return all(v is not None and not pd.isna(v) for v in vals)


class EmaTrend(Strategy):
    """Pullback entry in the direction of the trend: price above EMA50, EMA20 > EMA50,
    RSI cooled off below 60, and the current bar closes back above EMA20."""
    name = "ema_trend"
    regimes = ("trend_up", "trend_down")

    def evaluate(self, symbol, df, regime):
        if len(df) < 60:
            return None
        cur, prev = df.iloc[-1], df.iloc[-2]
        if not _ok(cur["ema20"], cur["ema50"], cur["rsi14"], cur["atr14"]):
            return None
        stop = 2.0 * float(cur["atr14"])
        if regime == "trend_up" and prev["close"] < prev["ema20"] <= cur["close"] and cur["rsi14"] < 60:
            return Signal(symbol, "long", 0.6, self.name,
                          f"uptrend pullback: close reclaimed EMA20 ({cur['ema20']:.4g}), RSI {cur['rsi14']:.0f}",
                          stop_distance=stop)
        if regime == "trend_down" and prev["close"] > prev["ema20"] >= cur["close"] and cur["rsi14"] > 40:
            return Signal(symbol, "short", 0.6, self.name,
                          f"downtrend pullback: close lost EMA20 ({cur['ema20']:.4g}), RSI {cur['rsi14']:.0f}",
                          stop_distance=stop)
        return None


class RsiReversion(Strategy):
    """Mean reversion inside a range: RSI extreme plus a close outside the Bollinger band
    that comes back inside. Only in 'range' - in a trend this is catching knives."""
    name = "rsi_reversion"
    regimes = ("range",)

    def evaluate(self, symbol, df, regime):
        if len(df) < 40:
            return None
        cur, prev = df.iloc[-1], df.iloc[-2]
        if not _ok(cur["rsi14"], cur["bb_lo"], cur["bb_hi"], cur["atr14"]):
            return None
        stop = 1.5 * float(cur["atr14"])
        if prev["close"] < prev["bb_lo"] and cur["close"] > cur["bb_lo"] and cur["rsi14"] < 35:
            return Signal(symbol, "long", 0.5, self.name,
                          f"range: re-entered lower band, RSI {cur['rsi14']:.0f}", stop_distance=stop)
        if prev["close"] > prev["bb_hi"] and cur["close"] < cur["bb_hi"] and cur["rsi14"] > 65:
            return Signal(symbol, "short", 0.5, self.name,
                          f"range: re-entered upper band, RSI {cur['rsi14']:.0f}", stop_distance=stop)
        return None


class DonchianBreakout(Strategy):
    """Close beyond the 20-bar channel with above-average volume. Works in trends and
    at the moment a range ends; skipped in 'volatile' because breakouts there are noise."""
    name = "donchian_breakout"
    regimes = ("trend_up", "trend_down", "range")

    def evaluate(self, symbol, df, regime):
        if len(df) < 40:
            return None
        cur = df.iloc[-1]
        if not _ok(cur["dc_hi"], cur["dc_lo"], cur["atr14"], cur["vol_sma20"]):
            return None
        vol_ok = cur["volume"] > 1.2 * cur["vol_sma20"]
        stop = 2.0 * float(cur["atr14"])
        if cur["close"] > cur["dc_hi"] and vol_ok and regime != "trend_down":
            return Signal(symbol, "long", 0.55, self.name,
                          f"breakout above 20-bar high {cur['dc_hi']:.4g} on volume", stop_distance=stop)
        if cur["close"] < cur["dc_lo"] and vol_ok and regime != "trend_up":
            return Signal(symbol, "short", 0.55, self.name,
                          f"breakdown below 20-bar low {cur['dc_lo']:.4g} on volume", stop_distance=stop)
        return None


DEFAULT_STRATEGIES: list[Strategy] = [EmaTrend(), RsiReversion(), DonchianBreakout()]


def evaluate_all(symbol: str, df: pd.DataFrame, regime: str, strategies: list[Strategy] | None = None) -> list[Signal]:
    out: list[Signal] = []
    for s in strategies or DEFAULT_STRATEGIES:
        if not s.wants_regime(regime):
            continue
        try:
            sig = s.evaluate(symbol, df, regime)
        except Exception as exc:  # a broken strategy must not stop the loop
            sig = None
            print(f"strategy {s.name} failed: {exc}")
        if sig:
            out.append(sig)
    return out
