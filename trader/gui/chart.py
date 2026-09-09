"""Candlestick chart drawn with QPainter - no plotting library, no extra 40 MB in the build.

Shows candles, EMA20/EMA50, volume, the last price, the open position's entry / stop /
target lines, closed-trade markers, a crosshair with OHLC readout, wheel zoom and drag pan.
"""
from __future__ import annotations

import math
import time
from typing import Any

import pandas as pd
from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QFont, QPainterPath
from PySide6.QtWidgets import QWidget

BG = QColor("#0f131a"); GRID = QColor("#1f2633"); TEXT = QColor("#9aa3b2")
UP = QColor("#2ecc71"); DOWN = QColor("#e74c3c"); GOLD = QColor("#E9C46A"); BLUE = QColor("#4ea1ff")
WHITE = QColor("#e6e6e6"); VOL = QColor(120, 130, 150, 90)


class CandleChart(QWidget):
    hovered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.df: pd.DataFrame | None = None
        self.symbol = ""
        self.timeframe = ""
        self.position: dict[str, Any] | None = None
        self.trades: list[dict[str, Any]] = []
        self.visible = 120
        self.offset = 0          # bars hidden on the right (0 = latest bar visible)
        self._mouse: QPointF | None = None
        self._drag_x: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self._font = QFont("Segoe UI", 8)

    # ------------------------------------------------------------ data
    def set_data(self, df: pd.DataFrame, symbol: str, timeframe: str,
                 position: dict[str, Any] | None = None, trades: list[dict[str, Any]] | None = None) -> None:
        self.df, self.symbol, self.timeframe = df, symbol, timeframe
        self.position, self.trades = position, trades or []
        self.visible = min(self.visible, len(df)) if len(df) else self.visible
        self.offset = 0
        self.update()

    # ------------------------------------------------------------ interaction
    def wheelEvent(self, ev):
        if self.df is None:
            return
        step = max(5, self.visible // 8)
        self.visible = max(20, min(len(self.df), self.visible - step if ev.angleDelta().y() > 0 else self.visible + step))
        self.offset = min(self.offset, max(0, len(self.df) - self.visible))
        self.update()

    def mousePressEvent(self, ev):
        self._drag_x = ev.position().x()

    def mouseReleaseEvent(self, ev):
        self._drag_x = None

    def mouseMoveEvent(self, ev):
        self._mouse = ev.position()
        if self._drag_x is not None and self.df is not None:
            w = self._plot_rect().width() / max(1, self.visible)
            bars = int((ev.position().x() - self._drag_x) / max(w, 1e-6))
            if bars:
                self.offset = max(0, min(len(self.df) - self.visible, self.offset + bars))
                self._drag_x = ev.position().x()
        self.update()

    def leaveEvent(self, ev):
        self._mouse = None; self.update()

    # ------------------------------------------------------------ geometry
    def _plot_rect(self) -> QRectF:
        return QRectF(8, 24, self.width() - 72, self.height() * 0.72 - 24)

    def _vol_rect(self) -> QRectF:
        p = self._plot_rect()
        return QRectF(p.left(), p.bottom() + 6, p.width(), self.height() - p.bottom() - 30)

    def _window(self) -> pd.DataFrame:
        end = len(self.df) - self.offset
        return self.df.iloc[max(0, end - self.visible):end]

    # ------------------------------------------------------------ paint
    def paintEvent(self, ev):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setFont(self._font)
        p.fillRect(self.rect(), BG)
        if self.df is None or len(self.df) < 2:
            p.setPen(TEXT); p.drawText(self.rect(), Qt.AlignCenter, "در حال دریافت داده…")
            return
        win = self._window()
        plot, vol = self._plot_rect(), self._vol_rect()
        lo = float(win["low"].min()); hi = float(win["high"].max())
        for k in ("ema20", "ema50"):
            if k in win:
                s = win[k].dropna()
                if len(s):
                    lo, hi = min(lo, float(s.min())), max(hi, float(s.max()))
        if self.position:
            for k in ("entry_price", "stop_price", "take_profit"):
                v = self.position.get(k)
                if v:
                    lo, hi = min(lo, float(v)), max(hi, float(v))
        pad = (hi - lo) * 0.06 or 1.0
        lo, hi = lo - pad, hi + pad
        n = len(win); bw = plot.width() / n
        vmax = float(win["volume"].max()) or 1.0

        def y(v: float) -> float:
            return plot.bottom() - (v - lo) / (hi - lo) * plot.height()

        def x(i: int) -> float:
            return plot.left() + (i + 0.5) * bw

        # grid + price axis
        p.setPen(QPen(GRID, 1))
        ticks = 6
        for t in range(ticks + 1):
            v = lo + (hi - lo) * t / ticks
            yy = y(v)
            p.drawLine(QPointF(plot.left(), yy), QPointF(plot.right(), yy))
            p.setPen(TEXT); p.drawText(QRectF(plot.right() + 4, yy - 8, 66, 16), Qt.AlignLeft | Qt.AlignVCenter, self._fmt(v)); p.setPen(QPen(GRID, 1))
        # time axis
        idx = win.index
        every = max(1, n // 6)
        for i in range(0, n, every):
            ts = idx[i]
            label = ts.strftime("%m-%d %H:%M") if self.timeframe and self.timeframe[-1] in "mh" else ts.strftime("%Y-%m-%d")
            p.setPen(TEXT); p.drawText(QRectF(x(i) - 40, self.height() - 22, 80, 16), Qt.AlignCenter, label)
            p.setPen(QPen(GRID, 1)); p.drawLine(QPointF(x(i), plot.top()), QPointF(x(i), vol.bottom()))

        # volume
        for i, (_, r) in enumerate(win.iterrows()):
            h = float(r["volume"]) / vmax * vol.height()
            col = UP if r["close"] >= r["open"] else DOWN
            c = QColor(col); c.setAlpha(90)
            p.fillRect(QRectF(x(i) - bw * 0.35, vol.bottom() - h, bw * 0.7, h), c)

        # candles
        wick = QPen(); wick.setWidthF(max(1.0, bw * 0.12))
        for i, (_, r) in enumerate(win.iterrows()):
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            col = UP if c >= o else DOWN
            wick.setColor(col); p.setPen(wick)
            p.drawLine(QPointF(x(i), y(h)), QPointF(x(i), y(l)))
            top, bot = y(max(o, c)), y(min(o, c))
            p.fillRect(QRectF(x(i) - bw * 0.35, top, bw * 0.7, max(1.0, bot - top)), col)

        # EMAs
        for k, col in (("ema20", GOLD), ("ema50", BLUE)):
            if k in win:
                path, started = QPainterPath(), False
                for i, v in enumerate(win[k].tolist()):
                    if v is None or (isinstance(v, float) and math.isnan(v)):
                        continue
                    pt = QPointF(x(i), y(float(v)))
                    if not started:
                        path.moveTo(pt); started = True
                    else:
                        path.lineTo(pt)
                p.setPen(QPen(col, 1.4)); p.drawPath(path)

        # last price
        last = float(win["close"].iloc[-1])
        pen = QPen(WHITE, 1, Qt.DashLine); p.setPen(pen)
        p.drawLine(QPointF(plot.left(), y(last)), QPointF(plot.right(), y(last)))
        p.fillRect(QRectF(plot.right() + 2, y(last) - 8, 68, 16), QBrush(WHITE))
        p.setPen(BG); p.drawText(QRectF(plot.right() + 4, y(last) - 8, 66, 16), Qt.AlignLeft | Qt.AlignVCenter, self._fmt(last))

        # open position lines
        if self.position:
            for k, col, name in (("entry_price", WHITE, "entry"), ("stop_price", DOWN, "stop"), ("take_profit", UP, "target")):
                v = self.position.get(k)
                if not v:
                    continue
                yy = y(float(v)); p.setPen(QPen(col, 1.2, Qt.DotLine))
                p.drawLine(QPointF(plot.left(), yy), QPointF(plot.right(), yy))
                p.setPen(col); p.drawText(QRectF(plot.left() + 4, yy - 16, 200, 16), Qt.AlignLeft | Qt.AlignVCenter,
                                          f"{name} {self._fmt(float(v))} ({self.position.get('side','')})")

        # closed-trade markers
        if self.trades and n:
            t0, t1 = idx[0].timestamp(), idx[-1].timestamp()
            span = (idx[-1] - idx[0]).total_seconds() / max(1, n - 1)
            for tr in self.trades:
                for ts, price, col, up in ((tr.get("opened_at"), tr.get("entry_price"), GOLD, tr.get("side") == "long"),
                                           (tr.get("closed_at"), tr.get("exit_price"), WHITE, tr.get("side") != "long")):
                    if not ts or not price or ts < t0 or ts > t1 + span:
                        continue
                    i = int((ts - t0) / span); i = max(0, min(n - 1, i))
                    cx, cy = x(i), y(float(price))
                    tri = QPainterPath()
                    if up:
                        tri.moveTo(cx, cy - 8); tri.lineTo(cx - 5, cy + 1); tri.lineTo(cx + 5, cy + 1)
                    else:
                        tri.moveTo(cx, cy + 8); tri.lineTo(cx - 5, cy - 1); tri.lineTo(cx + 5, cy - 1)
                    tri.closeSubpath(); p.fillPath(tri, col)

        # crosshair
        if self._mouse and plot.contains(self._mouse):
            i = int((self._mouse.x() - plot.left()) / bw); i = max(0, min(n - 1, i))
            r = win.iloc[i]
            p.setPen(QPen(TEXT, 1, Qt.DashLine))
            p.drawLine(QPointF(x(i), plot.top()), QPointF(x(i), vol.bottom()))
            p.drawLine(QPointF(plot.left(), self._mouse.y()), QPointF(plot.right(), self._mouse.y()))
            info = (f"{idx[i].strftime('%Y-%m-%d %H:%M')}  O {self._fmt(r['open'])}  H {self._fmt(r['high'])}  "
                    f"L {self._fmt(r['low'])}  C {self._fmt(r['close'])}  V {float(r['volume']):,.0f}")
            for k, lab in (("ema20", "EMA20"), ("ema50", "EMA50"), ("rsi14", "RSI")):
                if k in win and not pd.isna(r.get(k)):
                    info += f"  {lab} {self._fmt(float(r[k])) if k != 'rsi14' else f'{float(r[k]):.0f}'}"
            self.hovered.emit(info)

        # title
        p.setPen(GOLD); p.setFont(QFont("Segoe UI", 10, QFont.Bold))
        chg = (last / float(win["open"].iloc[0]) - 1) * 100
        title = f"{self.symbol}  {self.timeframe}   {self._fmt(last)}   {chg:+.2f}%"
        if plot.width() > 620:
            title += f"  ({n} bars)"
        p.drawText(QRectF(plot.left() + 4, 2, plot.width() - 170, 20), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setFont(self._font); p.setPen(GOLD); p.drawText(QRectF(plot.right() - 150, 2, 70, 20), Qt.AlignLeft | Qt.AlignVCenter, "— EMA20")
        p.setPen(BLUE); p.drawText(QRectF(plot.right() - 75, 2, 70, 20), Qt.AlignLeft | Qt.AlignVCenter, "— EMA50")

    @staticmethod
    def _fmt(v: float) -> str:
        v = float(v)
        if v >= 1000:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:.4g}"
        return f"{v:.6g}"
