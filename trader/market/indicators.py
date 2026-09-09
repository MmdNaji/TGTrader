"""Technical indicators on a pandas OHLCV frame (columns: open high low close volume)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_up / avg_down.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(avg_down != 0, 100.0).where(avg_up != 0, 0.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def donchian(df: pd.DataFrame, n: int = 20) -> tuple[pd.Series, pd.Series]:
    """Highest high / lowest low of the PREVIOUS n bars (shifted so today cannot see itself)."""
    return df["high"].rolling(n).max().shift(1), df["low"].rolling(n).min().shift(1)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the indicator set every strategy and the LLM snapshot rely on."""
    out = df.copy()
    c = out["close"]
    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["ema200"] = ema(c, 200)
    out["rsi14"] = rsi(c, 14)
    out["atr14"] = atr(out, 14)
    out["adx14"] = adx(out, 14)
    out["bb_lo"], out["bb_mid"], out["bb_hi"] = bollinger(c, 20, 2.0)
    out["macd"], out["macd_sig"], out["macd_hist"] = macd(c)
    out["dc_hi"], out["dc_lo"] = donchian(out, 20)
    out["vol_sma20"] = sma(out["volume"], 20)
    out["ret1"] = c.pct_change()
    out["atr_pct"] = out["atr14"] / c
    return out


def snapshot(df: pd.DataFrame) -> dict[str, float]:
    """Latest indicator values, rounded, for logging and for the LLM prompt."""
    last = df.iloc[-1]
    keys = ["open", "high", "low", "close", "volume", "ema20", "ema50", "ema200", "rsi14", "atr14", "adx14",
            "bb_lo", "bb_mid", "bb_hi", "macd", "macd_sig", "macd_hist", "dc_hi", "dc_lo", "vol_sma20", "atr_pct"]
    out: dict[str, float] = {}
    for k in keys:
        if k in df.columns:
            v = last[k]
            out[k] = None if pd.isna(v) else float(round(float(v), 6))
    # a little context: recent returns
    for n in (5, 20, 50):
        if len(df) > n:
            out[f"ret_{n}"] = float(round(float(df["close"].iloc[-1] / df["close"].iloc[-1 - n] - 1), 5))
    return out
