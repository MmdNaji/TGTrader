"""Measure the positions table INSIDE the real dashboard, not on its own.

Why this script exists: the same table built standalone never overflows - the last column is
handed three to eleven times the width it needs and sum(columns) always equals the viewport
exactly. Every number taken that way says "fits, with room to spare", which is why three
attempts at this column were made from Linux and two of them were wrong. The failure only
appears inside the real card, so this builds the real window.

Run it on the machine that can SEE the problem:

    .venv\\Scripts\\python scripts\\probe_positions_table.py

It changes nothing and touches no account: paper broker, no engine, no network.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TGTRADER_NO_AUTOUPDATE", "1")
os.environ.setdefault("TGTRADER_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt                      # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

from trader.gui.app import MainWindow, WheelGuard  # noqa: E402
from trader.gui import theme                       # noqa: E402
from trader.gui.widgets import fill, _text_width   # noqa: E402

ROW = ["ETH/USDT", "خرید", "2,457.83", "2,452.92", "50.02 $", "2,372.96", "2,627.56",
       "⁦-0.14 $ (-0.28%)⁩"]


def main() -> int:
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    for name in ("STYLE", "STYLESHEET", "QSS", "sheet"):
        css = getattr(theme, name, None)
        if isinstance(css, str) and css.strip():
            app.setStyleSheet(css)
            break
    app._wheel = WheelGuard()
    app.installEventFilter(app._wheel)

    win = MainWindow()
    win.show()
    win.goto("dashboard")
    t = win.tbl_positions
    dpr = win.devicePixelRatioF()
    print(f"device pixel ratio = {dpr}  (window widths below are LOGICAL pixels)")

    # FIND the minimum by shrinking, do not ask for it. minimumWidth() read at the opening size
    # answers 906 while the live window goes to 700, because the floor is progressive: below
    # COMPACT_W the topbar drops its words and the minimum drops with them. Reading it before
    # that has happened reports a floor 206px above the real one, and the sweep then starts
    # above the band where the table actually breaks - which is exactly what it did.
    steps = []
    for _ in range(6):
        win.resize(300, 900)
        for _ in range(8):
            app.processEvents()
        if steps and win.width() == steps[-1]:
            break
        steps.append(win.width())
    floor = steps[-1] if steps else win.width()
    print(f"narrowest the window will actually go = {floor}")
    print(f"  it takes {len(steps)} shrink(s) to get there: {steps}")
    print(f"  (the floor is PROGRESSIVE - below COMPACT_W the topbar drops its words and the")
    print(f"   minimum drops with it, so one resize stops at the first floor and a user")
    print(f"   dragging the edge goes further. minimumWidth() now reads {win.minimumWidth()}.)\n")

    card = t.parentWidget()
    while card is not None and card.objectName() not in ("card", "cardAccent"):
        card = card.parentWidget()

    hdr = ("rows  win  card  table   vp  sumcols  lastW lastHint  colPos7  hbar(vis/val/max)"
           "  vbar  VERDICT")
    print(hdr)
    print("-" * len(hdr))
    widths = sorted({floor, floor + 60, floor + 140, 900, 1100, 1381, 1650, 2278})
    for rows in (2, 4, 5, 6, 8):
        for w in widths:
            win.resize(w, 900)
            t.show()
            fill(t, [list(ROW) for _ in range(rows)])
            for _ in range(6):
                app.processEvents()
            n = t.columnCount() - 1
            cols = [t.columnWidth(c) for c in range(t.columnCount())]
            sb, vb = t.horizontalScrollBar(), t.verticalScrollBar()
            pos7 = t.columnViewportPosition(n)
            over = sum(cols) - t.viewport().width()
            # Content that genuinely does not fit SHOULD sit off the edge - that is what the
            # scrollbar is for, and the last column keeps its full width. The failure is being
            # off the edge with no way back, or being narrower than the text in it.
            unreachable = (pos7 < 0 or pos7 + cols[n] > t.viewport().width()) and \
                          (not sb.isVisible() or sb.maximum() < max(0, over))
            # A column the table deliberately DROPPED reads as zero-width; that is not the
            # same thing as one squeezed below its content, and only the second is a fault.
            hidden = [c for c in range(t.columnCount()) if t.isColumnHidden(c)]
            elided = [c for c in range(t.columnCount())
                      if not t.isColumnHidden(c) and cols[c] < _text_width(t, c) + 2]
            wider_than_card = card is not None and t.width() > card.width()
            # the same data filled twice must not come out narrower the second time
            fill(t, [list(ROW) for _ in range(rows)])
            for _ in range(4):
                app.processEvents()
            drift = sum(t.columnWidth(c) for c in range(t.columnCount())) - sum(cols)
            verdict = ", ".join(x for x, bad in (
                ("UNREACHABLE", unreachable), (f"ELIDED{elided}", bool(elided)),
                ("WIDER-THAN-CARD", wider_than_card), (f"DRIFT({drift})", drift < 0)) if bad)
            if not verdict:
                verdict = f"ok (dropped {hidden})" if hidden else "ok"
            print(f"{rows:>4} {w:>5} {card.width() if card else -1:>5} {t.width():>5} "
                  f"{t.viewport().width():>5} {sum(cols):>7}  {cols[n]:>5} "
                  f"{t.sizeHintForColumn(n):>8} {pos7:>8} "
                  f"{str(sb.isVisible())[:1]}/{sb.value():>4}/{sb.maximum():>4} "
                  f"{str(vb.isVisible())[:1]:>5}  {verdict}")
    print("\nUNREACHABLE     = a column is off the viewport with no scrollbar range to reach it")
    print("ELIDED[..]      = those columns are narrower than the text in them")
    print("ok (dropped..)  = it did not fit, so the table let those columns go - by design;")
    print("                  they come back as soon as there is room, and the row detail has all")
    print("WIDER-THAN-CARD = the table is drawn wider than the card holding it")
    print("DRIFT(-n)       = refilling the SAME data made the columns n pixels narrower;")
    print("                  the dashboard refills every 1.5s, so any drift compounds")
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
