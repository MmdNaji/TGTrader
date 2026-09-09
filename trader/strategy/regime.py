"""Market regime detection: trend_up | trend_down | range | volatile.

Trend following loses money in a range and mean reversion loses money in a
trend, so knowing which one we are in matters more than any single indicator.
"""
from __future__ import annotations

import pandas as pd


def detect_regime(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    adx = last.get("adx14")
    atr_pct = last.get("atr_pct")
    e20, e50, e200, close = last.get("ema20"), last.get("ema50"), last.get("ema200"), last["close"]
    if pd.isna(adx) or pd.isna(e50):
        return "unknown"
    # Very wide bars relative to price: stand aside or size down.
    if atr_pct is not None and not pd.isna(atr_pct):
        recent = df["atr_pct"].tail(100)
        if len(recent) >= 50 and atr_pct > recent.quantile(0.9) * 1.1:
            return "volatile"
    if adx >= 22:
        if e20 > e50 and close > e50 and (pd.isna(e200) or close > e200):
            return "trend_up"
        if e20 < e50 and close < e50 and (pd.isna(e200) or close < e200):
            return "trend_down"
    return "range"
