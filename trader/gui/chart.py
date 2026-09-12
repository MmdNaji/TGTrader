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
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QFont, QFontMetricsF, QPainterPath
from PySide6.QtWidgets import QWidget

BG = QColor("#0f131a"); GRID = QColor("#1f2633"); TEXT = QColor("#9aa3b2")
UP = QColor("#2ecc71"); DOWN = QColor("#e74c3c"); GOLD = QColor("#E9C46A"); BLUE = QColor("#4ea1ff")
WHITE = QColor("#e6e6e6"); VOL = QColor(120, 130, 150, 90)


def _spread(items: list, plot) -> list:
    """Move overlapping right-axis pills apart, keeping rank 0 exactly where it is.

    They collide in ordinary trades: a stop 6.7% under the price is a few pixels away on this
    scale, and the live-price pill then covered half of the stop - leaving the top of its digits
    showing, which still LOOKS like a whole number. That is the same lie as a truncated price,
    by hiding instead of cutting, and every scalp trade with a tight stop has that shape.

    Rank 0 is the live price: it is the only one that must line up with the axis, so everything
    else gives way to it. The rest are pushed OUTWARD from it - a stop below the price moves
    further down, a target above moves further up - so a pill never crosses the line it belongs
    to and ends up labelling the wrong one.
    """
    if len(items) < 2:
        return items
    fixed = [it for it in items if it[4] == 0]
    anchor_y = fixed[0][0] if fixed else items[0][0]

    # The gap is the HEIGHT OF THE TWO PILLS INVOLVED, not a constant. It was 21 - "pill height
    # 18 plus a little air" - and the live-price pill is 30 tall because it carries the
    # countdown on a second line, so it needs 15 + 9 + air = 27 from a neighbour. At 21 it
    # still overlapped by 3px after being spread, which is the same lie this function exists to
    # stop, just smaller. A number that only holds while every box is the same size is a number
    # that will be wrong the next time one of them grows.
    def height(it) -> float:
        return float(it[5]) if len(it) > 5 else 18.0

    out: list = []
    for it in sorted(items, key=lambda i: (i[4], abs(i[0] - anchor_y))):
        py = it[0]
        for _ in range(40):
            clash = next((o for o in out
                          if abs(o[0] - py) < (height(o) + height(it)) / 2.0 + 3.0), None)
            if clash is None:
                break
            need = (height(clash) + height(it)) / 2.0 + 3.0
            py = clash[0] + need if py >= anchor_y else clash[0] - need
        half = height(it) / 2.0
        py = min(max(py, plot.top() + half), plot.bottom() - half)
        out.append((py, it[1], it[2], it[3], it[4]) + tuple(it[5:]))
    return out


class CandleChart(QWidget):
    hovered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.df: pd.DataFrame | None = None
        self.symbol = ""
        self.timeframe = ""
        self.position: dict[str, Any] | None = None
        self.trades: list[dict[str, Any]] = []
        # WHERE THE TRADE IS AIMED, drawn past the last candle. The owner asked for this in as
        # many words: "I clicked the coin with the open trade - you know how they draw a line
        # continuing the chart, meaning it's going to go like this - show me that."
        #
        # It is drawn as a CONE, not a line, and that is the honest shape: the two edges are the
        # target and the stop, which is the whole of what the bot actually decided. A single
        # line towards the target would be a forecast this program does not have and cannot
        # make. The width of the cone IS the risk.
        self.projection: dict[str, Any] | None = None
        self.visible = 120
        self.offset = 0          # bars hidden on the right (0 = latest bar visible)
        self.live_price: float | None = None   # last streamed price, overrides the closing tick
        self._mouse: QPointF | None = None
        self._drag_x: float | None = None
        self.setMouseTracking(True)
        # Click to arm the wheel. Until then the wheel belongs to the page - see wheelEvent.
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumHeight(320)
        self._font = QFont("Segoe UI", 8)
        self._drawn_pills: list = []     # (top, bottom, text) of every right-axis pill painted

    # ------------------------------------------------------------ data
    def set_live_price(self, price: float) -> None:
        """A freshly streamed price; only shown when the newest bar is in view."""
        self.live_price = float(price)
        self.update()

    def _projection_plan(self, win) -> dict[str, Any] | None:
        """How far past the last candle to draw, and to what levels.

        The horizon is ARITHMETIC, not a guess: a target that is three ATRs away needs at least
        three average days to be reached, so the cone is that long. It is labelled as the
        distance it is, and nothing here claims the price will arrive - only that this is what
        the trade is aimed at and what it is risking.
        """
        pr = self.projection
        if not pr or self.offset != 0:
            return None            # only when the newest bar is on screen; it is about the future
        try:
            entry = float(pr.get("entry") or 0.0)
            stop = float(pr.get("stop") or 0.0)
            target = float(pr.get("target") or 0.0)
        except (TypeError, ValueError):
            return None
        if not (entry and stop and target):
            return None
        atr = 0.0
        try:
            atr = float(pr.get("atr") or 0.0)
        except (TypeError, ValueError):
            atr = 0.0
        if atr <= 0 and "atr14" in win:
            series = win["atr14"].dropna()
            atr = float(series.iloc[-1]) if len(series) else 0.0
        reach = abs(target - entry)
        bars = int(round(reach / atr)) if atr > 0 else 8
        bars = max(4, min(28, bars))
        # HOW FAR IT IS DRAWN is not the same number as how far it is ESTIMATED. At 120 visible
        # candles a six-bar cone is five percent of the width - technically correct and, on the
        # rendered picture, a smudge in the corner. The owner asked to SEE where the trade is
        # aimed. So it is drawn across about a seventh of the view and the ATR estimate is
        # marked ON it with a tick, rather than the estimate being shrunk to invisibility or
        # the drawing quietly overstating the horizon.
        drawn = max(bars, max(8, int(self.visible / 7)))
        drawn = min(drawn, 40)
        return {"entry": entry, "stop": stop, "target": target, "bars": bars, "drawn": drawn,
                "side": str(pr.get("side") or "long"), "atr": atr}

    def set_projection(self, projection: dict[str, Any] | None) -> None:
        """Entry, stop, target and ATR for the trade being shown - or None to clear it."""
        self.projection = projection or None
        self.update()

    def set_data(self, df: pd.DataFrame, symbol: str, timeframe: str,
                 position: dict[str, Any] | None = None, trades: list[dict[str, Any]] | None = None) -> None:
        # A refresh must not throw the view away. The chart reloads on a timer, so resetting the
        # pan here meant that looking at anything but the newest bars was impossible: every
        # minute the chart jumped back under the cursor.
        same = (self.df is not None and symbol == self.symbol and timeframe == self.timeframe)
        prev_len = len(self.df) if self.df is not None else 0
        prev_offset = self.offset
        self.df, self.symbol, self.timeframe = df, symbol, timeframe
        self.position, self.trades = position, trades or []
        n = len(df)
        if not same or not n:
            self.visible = min(120, n) if n else self.visible
            self.offset = 0
        else:
            self.visible = max(20, min(self.visible, n))
            if prev_offset <= 0:
                self.offset = 0          # pinned to the newest bar: stay pinned
            else:
                grew = max(0, n - prev_len)
                self.offset = max(0, min(n - self.visible, prev_offset + grew))
        self.update()

    # ------------------------------------------------------------ interaction
    def wheelEvent(self, ev):
        # The wheel zooms ONLY after the chart has been clicked. It used to zoom whenever the
        # cursor merely passed over, which is the same mistake WheelGuard exists to stop on the
        # settings fields - and it got worse as the window got narrower, because the chart is
        # then most of a page that needs a lot of scrolling. Reported from a real session: the
        # page appeared to be stuck when it was quietly being zoomed instead.
        if not self.hasFocus():
            ev.ignore()          # let the scroll area have it
            return
        ev.accept()
        if self.df is None:
            return
        step = max(5, self.visible // 8)
        self.visible = max(20, min(len(self.df), self.visible - step if ev.angleDelta().y() > 0 else self.visible + step))
        self.offset = min(self.offset, max(0, len(self.df) - self.visible))
        self.update()

    def mousePressEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)
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
        return QRectF(8, 24, self.width() - 86, self.height() * 0.72 - 24)

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
        self._drawn_pills: list = []
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
        # Room on the right for the cone, and the levels it reaches have to be in view or the
        # chart rescales the moment it is drawn and the candles shrink for no visible reason.
        proj = self._projection_plan(win)
        if proj:
            for v in (proj["target"], proj["stop"]):
                lo, hi = min(lo, v), max(hi, v)
        pad = (hi - lo) * 0.06 or 1.0
        lo, hi = lo - pad, hi + pad
        n = len(win)
        future = proj["drawn"] if proj else 0
        bw = plot.width() / (n + future)
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
        #
        # Six labels whatever the width is, in an 80px box each. On a narrow window the chart
        # is a few hundred pixels wide and six dates became one run of digits:
        # "5-2026802512026600292062722609-08". The count has to come from how much room a real
        # label needs, not from a constant - so it is measured, with a gap, and the first and
        # last are pulled inside the plot rather than drawn half off the edge.
        idx = win.index
        intraday = bool(self.timeframe) and self.timeframe[-1] in "mh"
        fmt = "%m-%d %H:%M" if intraday else "%Y-%m-%d"
        fm = p.fontMetrics()
        lab_w = fm.horizontalAdvance("2026-09-08 00:00" if intraday else "2026-09-08") + 8
        half = lab_w / 2
        px_per_bar = max(1e-6, plot.width() / max(1, n))
        # The SPACING is what has to be at least a label wide, so derive it from that directly
        # rather than from a tick count. Capped at six as before, so a wide chart is not a ruler.
        every = max(int(math.ceil((lab_w + 14) / px_per_bar)), int(math.ceil(n / 6)), 1)
        # Start far enough in that the first box fits WHOLE. Clamping it to the edge instead was
        # the first version, and a clamped label keeps its width: the label pushed inward then
        # overlapped the next one by 17px, which is the same pile-up in a new place.
        i = int(math.ceil(half / px_per_bar))
        while i < n and x(i) + half <= plot.right():
            label = idx[i].strftime(fmt)
            w = fm.horizontalAdvance(label) + 6
            p.setPen(TEXT)
            p.drawText(QRectF(x(i) - w / 2, self.height() - 22, w, 16), Qt.AlignCenter, label)
            p.setPen(QPen(GRID, 1)); p.drawLine(QPointF(x(i), plot.top()), QPointF(x(i), vol.bottom()))
            i += every

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

        # right-axis price pill (TradingView style)
        # COLLECTED FIRST, then spread, then drawn. `_spread` has existed since the day the
        # live-price pill was found covering half the stop - and nothing ever called it. The
        # test called it directly and checked its arithmetic, so the maths was green while the
        # picture was wrong: on a real trade `75,073.62` sat half-under the green pill, and half
        # a price still LOOKS like a whole one. Exactly the shape of the toggle_run bug - a
        # correct function nobody reaches - and found the same way, by building the app and
        # looking at it.
        pills: list = []

        def pill(price_y: float, color: QColor, text: str, sub: str = "", rank: int = 1):
            """The box is sized to the PRICE, never the price to the box.

            It was a fixed 74px with the text drawn into 66 of it, so a long price - a
            six-figure BTC, or a token quoted to eight decimals - was cut, and a cut price
            reads as a whole one. Same rule as the equity-curve labels and the chart header:
            a number missing digits off its end is not a smaller number, it is a wrong one.
            Where even the full gutter cannot hold it, precision is DROPPED rather than
            characters - a rounded price is a normal thing to show, a truncated one is a lie.
            """
            h = 30 if sub else 18
            pills.append((price_y, color, text, sub, rank, h))

        def draw_pill(price_y: float, color: QColor, text: str, sub: str, h: float):
            # Recorded AS DRAWN, not recomputed. Whether two pills overlap is a fact about the
            # painting, and a test that recomputes the geometry can be green while nothing calls
            # the spreader - which is exactly what happened for the life of `_spread`.
            f = QFont(self._font); f.setBold(True)
            fm_big, fm_small = QFontMetricsF(f), QFontMetricsF(self._font)
            gutter = max(40.0, self.width() - plot.right() - 6)
            shown = text
            while fm_big.horizontalAdvance(shown) + 11 > gutter and "." in shown:
                shown = shown[:shown.rindex(".")] if shown.endswith(".") else shown[:-1]
                shown = shown.rstrip(".")
            need = max(fm_big.horizontalAdvance(shown), fm_small.horizontalAdvance(sub) if sub else 0)
            w = min(gutter, max(74.0, need + 11))
            box = QRectF(plot.right() + 2, price_y - h / 2, w, h)
            self._drawn_pills.append((box.top(), box.bottom(), shown))
            path = QPainterPath(); path.addRoundedRect(box, 4, 4)
            p.fillPath(path, color)
            p.setPen(QColor("#ffffff")); p.setFont(f)
            inner = w - 9
            if sub:
                p.drawText(QRectF(box.left() + 5, box.top() + 2, inner, 15), Qt.AlignLeft | Qt.AlignVCenter, shown)
                p.setFont(self._font)
                p.drawText(QRectF(box.left() + 5, box.top() + 15, inner, 13), Qt.AlignLeft | Qt.AlignVCenter, sub)
            else:
                p.drawText(box.adjusted(6, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, shown)
            p.setFont(self._font)

        # take-profit / stop zones for the open position, drawn translucent over the candles
        if self.position:
            entry = float(self.position.get("entry_price") or 0)
            tp = float(self.position.get("take_profit") or 0)
            stop = float(self.position.get("stop_price") or 0)
            zx = plot.left() + plot.width() * 0.55   # start the band partway across, like TV
            # These bands stop at the LAST CANDLE, not at the edge of the plot. They are about
            # where price has been against this trade; the cone past that point is about where
            # it is aimed. Running them to the edge put two different statements on top of each
            # other in the same colours, and the rendered chart was a muddy block - visible only
            # by looking at the picture, which is the whole reason this gets rendered.
            zr = x(n - 1) if proj else plot.right()
            if entry and tp:
                g = QColor(46, 204, 113, 45)
                p.fillRect(QRectF(zx, min(y(entry), y(tp)), zr - zx, abs(y(entry) - y(tp))), g)
            if entry and stop:
                rr = QColor(231, 76, 60, 45)
                p.fillRect(QRectF(zx, min(y(entry), y(stop)), zr - zx, abs(y(entry) - y(stop))), rr)
            for v, col in ((tp, UP), (entry, QColor("#8a94a7")), (stop, DOWN)):
                if v:
                    yy = y(v); p.setPen(QPen(col, 1, Qt.DotLine))
                    p.drawLine(QPointF(zx, yy), QPointF(zr, yy))

        # last price (streamed value when the latest bar is on screen)
        last = float(self.live_price) if (self.live_price and self.offset == 0) else float(win["close"].iloc[-1])
        prev_close = float(win["close"].iloc[-2]) if n > 1 else last
        live_col = UP if last >= prev_close else DOWN
        p.setPen(QPen(live_col, 1, Qt.DotLine))
        p.drawLine(QPointF(plot.left(), y(last)), QPointF(plot.right(), y(last)))

        # ---- where this trade is aimed, drawn past the last candle
        if proj:
            x0, y0 = x(n - 1), y(last)
            x1 = x(n - 1 + proj["drawn"])
            yt, ys = y(proj["target"]), y(proj["stop"])

            # The CONE between the target and the stop. Both edges are real decisions the bot
            # made and can be argued with; the space between them is the range it has committed
            # to, and its width is the risk. A single line to the target would be a forecast
            # this program does not have.
            wedge = QPainterPath()
            wedge.moveTo(QPointF(x0, y0))
            wedge.lineTo(QPointF(x1, yt))
            wedge.lineTo(QPointF(x1, ys))
            wedge.closeSubpath()
            up = proj["side"] == "long"
            fill = QColor(UP if up else DOWN); fill.setAlpha(26)
            p.fillPath(wedge, fill)

            good = QColor(UP); good.setAlpha(210)
            bad = QColor(DOWN); bad.setAlpha(210)
            p.setPen(QPen(good, 2, Qt.DashLine))
            p.drawLine(QPointF(x0, y0), QPointF(x1, yt))
            p.setPen(QPen(bad, 2, Qt.DashLine))
            p.drawLine(QPointF(x0, y0), QPointF(x1, ys))

            # A dotted spine at the target and the stop, so the eye can carry them back to the
            # price axis without following the diagonal.
            p.setPen(QPen(good, 1, Qt.DotLine)); p.drawLine(QPointF(x0, yt), QPointF(x1, yt))
            p.setPen(QPen(bad, 1, Qt.DotLine)); p.drawLine(QPointF(x0, ys), QPointF(x1, ys))

            # Where the ATR estimate actually lands, marked on the cone. Without this the
            # drawing would be claiming a horizon it did not compute.
            xe = x(n - 1 + proj["bars"])
            p.setPen(QPen(TEXT, 1, Qt.DotLine))
            p.drawLine(QPointF(xe, min(yt, ys)), QPointF(xe, max(yt, ys)))

            # Say in words what it is, because a dashed line into the future reads as a
            # prediction and this is not one.
            move = (proj["target"] - proj["entry"]) / proj["entry"] * 100.0 if proj["entry"] else 0.0
            risk = (proj["stop"] - proj["entry"]) / proj["entry"] * 100.0 if proj["entry"] else 0.0
            cap = (f"هدف {move:+.1f}٪ · حد ضرر {risk:+.1f}٪ · "
                   f"حدود {proj['bars']} کندل تا هدف اگر با سرعت این روزها برود")
            p.setFont(QFont("Segoe UI", 8))
            fm = p.fontMetrics()
            # Shorten, never clip. At 718 logical pixels the caption was wider than the plot and
            # `min(...)` alone pushed its LEFT edge off the chart, so "این روزها برود" was cut
            # against the card behind it. A cut sentence is the same lie as a cut price: it
            # still looks like a whole one. If the full text will not fit, a shorter true
            # sentence is printed instead.
            if fm.horizontalAdvance(cap) > plot.width() - 12:
                cap = f"هدف {move:+.1f}٪ · حد ضرر {risk:+.1f}٪"
            tw = fm.horizontalAdvance(cap)
            tx = max(plot.left() + 6, min(x1, plot.right() - tw - 6))
            # BELOW the cone, not above it: above put it straight through the EMA legend along
            # the top of the plot, which the rendered picture showed and no measurement would.
            ty = min(plot.bottom() - 4, max(yt, ys) + fm.height() + 6)
            box = QRectF(tx - 5, ty - fm.height(), tw + 10, fm.height() + 4)
            bgc = QColor(BG); bgc.setAlpha(215)
            p.fillRect(box, bgc)
            p.setPen(QPen(TEXT))
            p.drawText(QPointF(tx, ty), cap)

        # right-axis pills: TP (green), entry (grey), stop (red), then the live price + countdown
        if self.position:
            if self.position.get("take_profit"):
                pill(y(float(self.position["take_profit"])), UP, self._fmt(float(self.position["take_profit"])))
            if self.position.get("stop_price"):
                pill(y(float(self.position["stop_price"])), DOWN, self._fmt(float(self.position["stop_price"])))
        pill(y(last), live_col, self._fmt(last), self._countdown(), rank=0)

        # Now spread them and draw. Rank 0 last, so if anything still touches, the live price is
        # the one on top - it is the only pill that must line up with its own line.
        for py, col, txt, sub, rank, h in sorted(_spread(pills, plot), key=lambda i: -i[4]):
            draw_pill(py, col, txt, sub, h)

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
        # Built up piece by piece and stopped when the box is full, instead of written out and
        # cut. On a 1366px screen the whole "BTC/USDT  1d  76,865.40  -5.21%" did not fit, and
        # the RTL window cut it from the FRONT: what was left on screen read ":65.40  -5.21%".
        # Nobody sees that as a truncated header - they see a price of 65.40. Same damage as
        # "شروع 7" on the equity curve, and the same rule applies: whole or not at all.
        #
        # The symbol and timeframe are the identity of the chart and always stay; the price is
        # dropped first because it is also on the right-hand price pill, two centimetres away.
        fm = p.fontMetrics()
        # The 170px reserved on the right is for the two EMA legends. On a narrow chart that is
        # most of the header, and the legends are decoration while the symbol is the identity of
        # what you are looking at - so below this width the legends go and the title gets the row.
        legend = plot.width() >= 430
        room = max(0.0, plot.width() - (170 if legend else 8))
        title = f"{self.symbol}  {self.timeframe}"
        extras = [self._fmt(last), f"{chg:+.2f}%"]
        if plot.width() > 620:
            extras.append(f"({n} bars)")
        for extra in extras:
            wider = f"{title}   {extra}"
            if fm.horizontalAdvance(wider) > room:
                break
            title = wider
        if fm.horizontalAdvance(title) > room:
            # even the symbol does not fit: elide it, so what is left is visibly a cut NAME
            # rather than a number that can be mistaken for a price.
            title = fm.elidedText(title, Qt.ElideRight, int(room))
        p.drawText(QRectF(plot.left() + 4, 2, room, 20), Qt.AlignLeft | Qt.AlignVCenter, title)
        if legend:
            p.setFont(self._font); p.setPen(GOLD)
            p.drawText(QRectF(plot.right() - 150, 2, 70, 20), Qt.AlignLeft | Qt.AlignVCenter, "— EMA20")
            p.setPen(BLUE)
            p.drawText(QRectF(plot.right() - 75, 2, 70, 20), Qt.AlignLeft | Qt.AlignVCenter, "— EMA50")

    _TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

    def _countdown(self) -> str:
        """Time left until the current candle closes, as m:ss or h:mm."""
        sec = self._TF_SEC.get(self.timeframe or "", 0)
        if not sec:
            return ""
        rem = int(sec - (time.time() % sec))
        if rem >= 3600:
            return f"{rem // 3600}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"
        return f"{rem // 60:02d}:{rem % 60:02d}"

    @staticmethod
    def _fmt(v: float) -> str:
        v = float(v)
        if v >= 1000:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:.4g}"
        return f"{v:.6g}"
