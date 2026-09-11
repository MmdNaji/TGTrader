"""Why do the test and the probe disagree about column widths on Windows?

The test reports "col 3 narrower than its text" at EVERY width, including 2278px where the
card is 2014 and the whole table needs about 600. Nothing can be short there, so the
measurement is wrong rather than the layout - and this prints every number behind it so the
wrong one is visible instead of guessed at.

    .venv\\Scripts\\python scripts\\probe_text_width.py

Changes nothing, touches no account.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TGTRADER_NO_AUTOUPDATE", "1")
os.environ.setdefault("TGTRADER_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt                       # noqa: E402
from PySide6.QtGui import QFontMetrics              # noqa: E402
from PySide6.QtWidgets import QApplication          # noqa: E402

from trader.gui import theme                        # noqa: E402
from trader.gui.app import MainWindow, WheelGuard   # noqa: E402
from trader.gui.widgets import fill, _text_width    # noqa: E402

ROW = ["ETH/USDT", "خرید", "2,457.83", "2,452.92", "50.02 $", "2,372.96", "2,627.56",
       "⁦-0.14 $ (-0.28%)⁩"]


def main() -> int:
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(theme.QSS)
    app._wheel = WheelGuard()
    app.installEventFilter(app._wheel)
    win = MainWindow()
    win.show()
    win.goto("dashboard")
    t = win.tbl_positions
    win.resize(2278, 900)          # far wider than anything can need
    t.show()
    fill(t, [list(ROW) for _ in range(2)])
    for _ in range(6):
        app.processEvents()

    print("FONTS - if these differ, a width measured with the wrong one is the whole problem")
    for who, f in (("app", app.font()), ("table", t.font()), ("viewport", t.viewport().font()),
                   ("header", t.horizontalHeader().font())):
        print(f"  {who:>9}: {f.family()!r} {f.pointSizeF()}pt bold={f.bold()}")
    it = t.item(0, 3)
    if it is not None:
        print(f"  {'item(0,3)':>9}: {it.font().family()!r} {it.font().pointSizeF()}pt")

    fm_table = QFontMetrics(t.font())
    fm_view = QFontMetrics(t.viewport().font())
    print(f"\nwindow {win.width()}  table {t.width()}  viewport {t.viewport().width()}")
    print(f"{'col':>4} {'header':>11} {'width':>6} {'qtHint':>7} {'_text_width':>12} "
          f"{'byTableFont':>12} {'byViewFont':>11}  widest cell")
    for c in range(t.columnCount()):
        texts = [t.horizontalHeaderItem(c).text() if t.horizontalHeaderItem(c) else ""]
        texts += [t.item(r, c).text() for r in range(t.rowCount()) if t.item(r, c)]
        widest = max(texts, key=len) if texts else ""
        print(f"{c:>4} {texts[0]:>11} {t.columnWidth(c):>6} {t.sizeHintForColumn(c):>7} "
              f"{_text_width(t, c):>12} {max(fm_table.horizontalAdvance(x) for x in texts):>12} "
              f"{max(fm_view.horizontalAdvance(x) for x in texts):>11}  {widest!r}")
    print("\nA column whose width is BELOW its own qtHint has been shaved; below the text")
    print("measure it would be elided. At this window width neither should be true.")
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
