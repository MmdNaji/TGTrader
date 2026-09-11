"""Reusable UI pieces: cards, KPI tiles, pills, form rows with hints, empty states, equity curve."""
from __future__ import annotations

import re

from typing import Any

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QPainterPath, QLinearGradient
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QTableWidget, QTableWidgetItem,
                               QHeaderView, QSizePolicy, QPushButton)

from . import theme


class Card(QFrame):
    """A rounded panel with an optional title row (title, subtitle, right-side actions)."""

    def __init__(self, title: str = "", subtitle: str = "", accent: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("cardAccent" if accent else "card")
        self.outer = QVBoxLayout(self); self.outer.setContentsMargins(16, 14, 16, 14); self.outer.setSpacing(10)
        self.header = QHBoxLayout(); self.header.setSpacing(8)
        self.title_lbl = QLabel(title); self.title_lbl.setObjectName("cardTitle")
        self.sub_lbl = QLabel(bidi_safe(subtitle)); self.sub_lbl.setObjectName("cardSub")
        # A one-line Persian subtitle is a 350px minimum width that the card can never go
        # below, and a card that cannot shrink pushes the whole WINDOW wider. Measured: the
        # subtitles alone were most of a 984px minimum on a page with two of them side by
        # side. Wrapping costs a second line on a narrow window and nothing on a wide one.
        self.sub_lbl.setWordWrap(True)
        tcol = QVBoxLayout(); tcol.setSpacing(0); tcol.addWidget(self.title_lbl)
        if subtitle:
            tcol.addWidget(self.sub_lbl)
        else:
            self.sub_lbl.hide()
        # The TITLE COLUMN takes the slack, not a stretch after it. A word-wrapping QLabel
        # reports a narrow size hint on purpose - it aims for a readable block - so with
        # the stretch swallowing the leftover, the subtitle wrapped into a ~250px column
        # inside a 1600px card, and a card three lines tall was breaking Persian words in
        # half: "دلیلش ر" / "بخوان". That is the same class of damage as a clipped number.
        self.header.addLayout(tcol, 1)
        if title:
            self.outer.addLayout(self.header)
        self.body = QVBoxLayout(); self.body.setSpacing(8)
        self.outer.addLayout(self.body)

    def add_action(self, w: QWidget) -> None:
        self.header.addWidget(w)

    def add(self, w: QWidget, stretch: int = 0) -> None:
        self.body.addWidget(w, stretch)

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class Kpi(QFrame):
    """Big number with a small label above and an optional note below."""

    def __init__(self, label: str, value: str = "—", sub: str = "", tone: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        v = QVBoxLayout(self); v.setContentsMargins(16, 12, 16, 12); v.setSpacing(2)
        self.lbl = QLabel(label); self.lbl.setObjectName("kpiLabel")
        self.val = QLabel(value); self.set_tone(tone)
        self.sub = QLabel(sub); self.sub.setObjectName("kpiSub")
        v.addWidget(self.lbl); v.addWidget(self.val); v.addWidget(self.sub)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set(self, value: str, sub: str | None = None, tone: str | None = None) -> None:
        self.val.setText(value)
        if sub is not None:
            self.sub.setText(sub)
        if tone is not None:
            self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.val.setObjectName({"gold": "kpiValueGold", "green": "kpiValueGreen", "red": "kpiValueRed"}.get(tone, "kpiValue"))
        self.val.style().unpolish(self.val); self.val.style().polish(self.val)


def pill(text: str, kind: str = "muted") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName({"ok": "pillOk", "warn": "pillWarn", "danger": "pillDanger", "gold": "pillGold"}.get(kind, "pillMuted"))
    lbl.setAlignment(Qt.AlignCenter); lbl.setFixedHeight(24)
    return lbl


def set_pill(lbl: QLabel, text: str, kind: str) -> None:
    lbl.setText(text)
    lbl.setObjectName({"ok": "pillOk", "warn": "pillWarn", "danger": "pillDanger", "gold": "pillGold"}.get(kind, "pillMuted"))
    lbl.style().unpolish(lbl); lbl.style().polish(lbl)


# Only what bidi actually mangles: a SIGN in front of a number, or a unit behind one. A bare
# number needs no help - digits are their own run and come out in the right order. Isolating
# everything also swallowed the space after it ("۳ ارز" -> "⁦۳ ⁩ارز"), which is untidy for no gain.
# Latin and Persian digits both, because these sentences mix them.
_D = r"[0-9\u06F0-\u06F9]"
_NUM_BODY = _D + r"[0-9\u06F0-\u06F9,.\u066B\u066C]*"
_NUMBER = re.compile(
    r"[+\-\u2212]" + _NUM_BODY + r"(?:\s?[%$\u066A])?"     # signed, unit optional
    r"|" + _NUM_BODY + r"\s?[%$\u066A]"                      # unsigned, but carrying a unit
)
_ISOLATED = "\u2066\u2067\u2068"


def bidi_safe(text: str) -> str:
    """Stop a right-to-left paragraph reordering the numbers inside it.

    In an RTL line, bidi moves a leading sign to the other end: "-6.4%" is read out as "6.4%-"
    and "+0.5%" as "0.5%+". That is not cosmetic - it reads as a different number, and these
    sentences are the ones quoting measured results.

    Done HERE, where text reaches a widget, rather than by hand at every string. Wrapping them
    one at a time was the alternative and it rots: the next person writes a new sentence and
    the bug comes back with it. Table cells go through ltr() for the same reason.
    """
    if not text or any(c in text for c in _ISOLATED):
        return text          # already isolated by the caller; do not nest
    return _NUMBER.sub(lambda m: "\u2066" + m.group(0) + "\u2069", text)


def hint(text: str) -> QLabel:
    lbl = QLabel(bidi_safe(text)); lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


class ElidedLabel(QLabel):
    """A label that gives up its words instead of forcing the window wider.

    QLabel's minimum width is the width of its text, so one long Persian sentence sitting in a
    row that cannot wrap becomes a floor under the WHOLE WINDOW. Measured here: the topbar
    subtitle alone accounted for 224 of the 984 pixels the window refused to go below.

    CORRECTION, and it matters for anyone calibrating against the numbers in these comments.
    The Windows figures quoted around this change - a 1617px floor, then 1050 - are PHYSICAL
    pixels on a 150% display. Qt sizes in LOGICAL pixels, so the floor was ~1078 logical before
    and ~700 after. I wrote that 1617 "does not fit a 1366px laptop"; that was wrong - a 1366
    laptop at 100% has 1366 logical pixels and would have fitted either way. What was actually
    wrong is that ~1078 logical is far more than this window needs, so it could not share a
    screen with anything else and had no room left on a scaled display. The causes were real
    defects; the consequence I claimed was not.

    So this one reports a minimum width of zero, keeps the full sentence for the tooltip, and
    draws as much of it as fits with an ellipsis.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:           # type: ignore[override]
        self._full = text
        self._redraw()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self):                       # type: ignore[override]
        s = super().minimumSizeHint()
        s.setWidth(0)
        return s

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._redraw()

    def _redraw(self) -> None:
        fm = self.fontMetrics()
        fits = fm.horizontalAdvance(self._full) <= self.width()
        super().setText(self._full if fits else fm.elidedText(self._full, Qt.ElideRight, max(0, self.width())))
        self.setToolTip("" if fits else self._full)


def section(text: str) -> QLabel:
    lbl = QLabel(text); lbl.setObjectName("sectionTitle")
    return lbl


class FormRow(QWidget):
    """Label on the right, control on the left, a one-line explanation under the control."""

    def __init__(self, label: str, control: QWidget, help_text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("transparent")
        h = QHBoxLayout(self); h.setContentsMargins(0, 4, 0, 4); h.setSpacing(12)
        lbl = QLabel(label); lbl.setMinimumWidth(150); lbl.setAlignment(Qt.AlignRight | Qt.AlignTop); lbl.setStyleSheet("padding-top:8px")
        col = QVBoxLayout(); col.setSpacing(2); col.addWidget(control)
        if help_text:
            col.addWidget(hint(help_text))
        h.addWidget(lbl); h.addLayout(col, 1)


class Empty(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("empty"); self.setAlignment(Qt.AlignCenter); self.setWordWrap(True)


def table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    """Columns sized to their contents, with the last one absorbing whatever is left over.

    Every column used to be QHeaderView.Stretch, which means EQUAL width regardless of what is
    in it: in an eight-column table each cell got one eighth of the card, so "ETH/USDT" came out
    as ".../ETH", the reason column as "...scalp: mo", and - once the P&L cell grew a percentage
    - "-0.14 $ (-0.28%)" as "-0.14 $...". Three separate-looking truncation bugs, one cause.

    This is also why the elide fix appeared to work on the scan table and not on the positions
    table: the scan table already set ResizeToContents by hand. Elide direction decides WHICH
    end is cut; the width decides whether anything is cut at all.
    """
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    hh = t.horizontalHeader()
    for i in range(len(headers)):
        hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
    t._stretch_last = bool(stretch_last)
    if headers and headers[0] == "#":
        hh.setSectionResizeMode(0, QHeaderView.Fixed); t.setColumnWidth(0, 48)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.NoFocus)
    return t


# The bidi isolates that keep a number from being reordered in this right-to-left window.
# They are invisible, they are the FIRST characters of the string, and they silently defeated
# the sign test below: the floating-P&L column lost its green and red the moment those cells
# started being isolated, and nothing looked wrong enough to notice.
_BIDI = "\u2066\u2067\u2068\u2069\u200e\u200f"


# Below this many pixels of travel, a horizontal scrollbar is hidden instead of shown: see
# the note in fit_columns. Well under one character, so nothing readable can hide behind it.
DEAD_SCROLL = 8


def fit_columns(t: QTableWidget) -> None:
    """Stretch the last column only while there is room to spare.

    Keeping a column on Stretch permanently lets Qt squeeze it BELOW its content when the
    window is tight, which is how "-0.14 $ (-0.28%)" became "-0.14 $..." - and the horizontal
    scrollbar could not help, because as far as the table was concerned everything fitted.

    Measured across widths with everything on ResizeToContents and nothing stretched: no column
    is ever cut and the table scrolls when it must, at the cost of blank space on a wide card.
    So the stretch is applied only when the contents genuinely leave slack, which gives both.
    """
    n = t.columnCount()
    if not n or not getattr(t, "_stretch_last", True):
        return
    need = sum(t.sizeHintForColumn(c) for c in range(n))
    room = t.viewport().width()
    hh = t.horizontalHeader()
    # HEADROOM, not "need < room". Deciding on the exact boundary meant that at a width two
    # pixels above the content the stretch was applied anyway, and stretching then redistributes
    # and squeezes the last column under its own hint - the truncation comes back at precisely
    # the widths where it is hardest to notice.
    headroom = 24
    hh.setSectionResizeMode(n - 1, QHeaderView.Stretch if need + headroom <= room
                            else QHeaderView.ResizeToContents)
    # A scrollbar that can move three pixels is worse than none: it says there is something
    # hidden when every column is already on screen, and dragging it does nothing anyone can
    # see. That is what it looked like on a 1366px window - all eight columns visible and an
    # inert bar under them. ResizeToContents pads each section a little beyond the hint above,
    # so the total can end up a few pixels over the viewport with no column actually cut.
    sb = t.horizontalScrollBar()
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if sb.maximum() <= DEAD_SCROLL
                                   else Qt.ScrollBarAsNeeded)


def fill(t: QTableWidget, rows: list[list[Any]], tones: dict[int, str] | None = None) -> None:
    """tones: {column_index: 'pnl'} colours positive/negative numbers in that column."""
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            it = QTableWidgetItem("" if v is None else str(v))
            if tones and j in tones and isinstance(v, str):
                bare = v.strip(_BIDI).strip()
                if bare[:1] in "+-":
                    it.setForeground(QColor(theme.SUCCESS if bare.startswith("+") else theme.DANGER))
            t.setItem(i, j, it)
    # after the data, because the decision depends on what is now in the cells. The live tables
    # are refilled on the refresh timer, so a window the user resizes by hand catches up on the
    # next tick rather than needing a resize event of its own.
    fit_columns(t)


class EquityCurve(QWidget):
    """A small filled line chart of equity over time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transparent")
        self.points: list[tuple[float, float]] = []
        self.setMinimumHeight(120)

    def set_points(self, pts: list[tuple[float, float]]) -> None:
        self.points = pts; self.update()

    def paintEvent(self, ev):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(6, 6, self.width() - 12, self.height() - 12)
        if len(self.points) < 2:
            p.setPen(QColor(theme.MUTED)); p.drawText(self.rect(), Qt.AlignCenter, "منحنی سرمایه بعد از اولین بررسی بازار رسم می‌شود")
            return
        xs = [t for t, _ in self.points]; ys = [e for _, e in self.points]
        x0, x1 = xs[0], xs[-1] or xs[0] + 1; y0, y1 = min(ys), max(ys)
        if y1 - y0 < 1e-9:
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.1; y0 -= pad; y1 += pad
        up = ys[-1] >= ys[0]
        col = QColor(theme.SUCCESS if up else theme.DANGER)
        path = QPainterPath(); fillp = QPainterPath()
        for i, (t, e) in enumerate(self.points):
            x = r.left() + (t - x0) / max(x1 - x0, 1e-9) * r.width()
            y = r.bottom() - (e - y0) / (y1 - y0) * r.height()
            (path.moveTo if i == 0 else path.lineTo)(QPointF(x, y))
            if i == 0:
                fillp.moveTo(QPointF(x, r.bottom())); fillp.lineTo(QPointF(x, y))
            else:
                fillp.lineTo(QPointF(x, y))
        fillp.lineTo(QPointF(r.left() + r.width(), r.bottom())); fillp.closeSubpath()
        g = QLinearGradient(0, r.top(), 0, r.bottom()); c1 = QColor(col); c1.setAlpha(90); c2 = QColor(col); c2.setAlpha(0)
        g.setColorAt(0, c1); g.setColorAt(1, c2)
        p.fillPath(fillp, g)
        p.setPen(QPen(col, 2)); p.drawPath(path)
        # Two 200px boxes at the top corners: on a narrow card they overlapped and the pair
        # "999.90" / "1,000.12" was read off the screen as one number, "9990070.12".
        #
        # Clamping each to half the card fixed the overlap and introduced a worse bug, because
        # a clamped drawText CLIPS: at 1050px "شروع 999.77" was drawn as "شروع 7", which does
        # not read as a cut-off label, it reads as a balance of seven dollars. A number that is
        # missing is honest; a number that is cut in half is a lie.
        #
        # So nothing is ever clipped here. Each label is drawn only if it fits WHOLE, and they
        # are dropped in order of how little they are worth: the start value first - the curve
        # itself shows where it began - then the current value. The percentage is what the card
        # is for, so it is the last thing to go.
        first, last = ys[0], ys[-1]
        left_txt = f"شروع {first:,.2f}"
        right_txt = f"اکنون {last:,.2f}"
        pct = (last / first - 1) * 100 if first else 0.0
        mid_txt = f"{pct:+.2f}%" if first else ""
        fm = p.fontMetrics()
        gap = 10
        lw = fm.horizontalAdvance(left_txt) + 4
        rw = fm.horizontalAdvance(right_txt) + 4
        mw = fm.horizontalAdvance(mid_txt) + 4 if mid_txt else 0
        room = r.width()
        show_left = show_right = True
        if lw + mw + rw + gap * 2 > room:
            show_left = False
            if mw + rw + gap > room:
                show_right = False
        p.setPen(QColor(theme.MUTED))
        if show_left:
            p.drawText(QRectF(r.left(), r.top(), lw, 16), Qt.AlignLeft, left_txt)
        if show_right:
            p.drawText(QRectF(r.right() - rw, r.top(), rw, 16), Qt.AlignRight, right_txt)
        if mid_txt and mw <= room:
            x_from = r.left() + (lw if show_left else 0)
            x_to = r.right() - (rw if show_right else 0)
            p.setPen(QColor(theme.SUCCESS if pct >= 0 else theme.DANGER))
            p.drawText(QRectF(x_from, r.top(), max(0.0, x_to - x_from), 16), Qt.AlignCenter, mid_txt)


def button(text: str, kind: str = "", slot=None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    b.setCursor(Qt.PointingHandCursor)
    return b
