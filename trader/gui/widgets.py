"""Reusable UI pieces: cards, KPI tiles, pills, form rows with hints, empty states, equity curve."""
from __future__ import annotations

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
        self.sub_lbl = QLabel(subtitle); self.sub_lbl.setObjectName("cardSub")
        tcol = QVBoxLayout(); tcol.setSpacing(0); tcol.addWidget(self.title_lbl)
        if subtitle:
            tcol.addWidget(self.sub_lbl)
        else:
            self.sub_lbl.hide()
        self.header.addLayout(tcol); self.header.addStretch()
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


def hint(text: str) -> QLabel:
    lbl = QLabel(text); lbl.setObjectName("hint"); lbl.setWordWrap(True)
    return lbl


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
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    if headers and headers[0] == "#":
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed); t.setColumnWidth(0, 48)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.NoFocus)
    return t


def fill(t: QTableWidget, rows: list[list[Any]], tones: dict[int, str] | None = None) -> None:
    """tones: {column_index: 'pnl'} colours positive/negative numbers in that column."""
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            it = QTableWidgetItem("" if v is None else str(v))
            if tones and j in tones and isinstance(v, str) and v[:1] in "+-":
                it.setForeground(QColor(theme.SUCCESS if v.startswith("+") else theme.DANGER))
            t.setItem(i, j, it)


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
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(r.left(), r.top(), 200, 16), Qt.AlignLeft, f"{ys[0]:,.2f}")
        p.drawText(QRectF(r.right() - 200, r.top(), 200, 16), Qt.AlignRight, f"{ys[-1]:,.2f}")


def button(text: str, kind: str = "", slot=None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    b.setCursor(Qt.PointingHandCursor)
    return b
