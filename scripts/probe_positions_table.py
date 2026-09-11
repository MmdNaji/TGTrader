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
from trader.gui.widgets import fill                # noqa: E402

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
    print(f"window minimum width = {win.minimumWidth()}\n")

    card = t.parentWidget()
    while card is not None and card.objectName() not in ("card", "cardAccent"):
        card = card.parentWidget()

    hdr = ("rows  win  card  table   vp  sumcols  lastW lastHint  colPos7  hbar(vis/val/max)"
           "  vbar  VERDICT")
    print(hdr)
    print("-" * len(hdr))
    for rows in (2, 4, 5, 6, 8):
        for w in (900, 1100, 1381, 1650, 2278):
            win.resize(w, 900)
            t.show()
            fill(t, [list(ROW) for _ in range(rows)])
            for _ in range(6):
                app.processEvents()
            n = t.columnCount() - 1
            cols = [t.columnWidth(c) for c in range(t.columnCount())]
            sb, vb = t.horizontalScrollBar(), t.verticalScrollBar()
            pos7 = t.columnViewportPosition(n)
            # the two ways the last column can be unreadable, told apart
            off_left = pos7 < 0
            past_right = pos7 + cols[n] > t.viewport().width()
            too_narrow = cols[n] < t.sizeHintForColumn(n)
            wider_than_card = card is not None and t.width() > card.width()
            verdict = ", ".join(x for x, bad in (
                ("CUT-LEFT", off_left), ("PAST-RIGHT", past_right),
                ("TOO-NARROW", too_narrow), ("WIDER-THAN-CARD", wider_than_card)) if bad) or "ok"
            print(f"{rows:>4} {w:>5} {card.width() if card else -1:>5} {t.width():>5} "
                  f"{t.viewport().width():>5} {sum(cols):>7}  {cols[n]:>5} "
                  f"{t.sizeHintForColumn(n):>8} {pos7:>8} "
                  f"{str(sb.isVisible())[:1]}/{sb.value():>4}/{sb.maximum():>4} "
                  f"{str(vb.isVisible())[:1]:>5}  {verdict}")
    print("\nCUT-LEFT        = the last column starts left of the viewport (text loses its front)")
    print("PAST-RIGHT      = it ends past the right edge")
    print("TOO-NARROW      = the column is narrower than its own contents")
    print("WIDER-THAN-CARD = the table is drawn wider than the card holding it")
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
