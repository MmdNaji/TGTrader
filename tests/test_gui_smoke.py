"""Build the real window and walk every page.

This exists because 0.4.0 shipped an AttributeError on startup that no unit test could see:
every module imported, every function was correct in isolation, and the app died before it drew
anything. It has since caught a preset that silently did nothing and a teardown that aborted the
process. If it is slow, it is still cheaper than shipping a window that will not open.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("TGTRADER_HOME", tempfile.mkdtemp(prefix="tgtrader-gui-"))

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QPoint, QPointF          # noqa: E402
from PySide6.QtGui import QImage, QWheelEvent           # noqa: E402
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def win():
    app = QApplication.instance() or QApplication([])
    from trader.gui.app import MainWindow, WheelGuard
    app._wheel_guard = WheelGuard()
    app.installEventFilter(app._wheel_guard)
    app.setLayoutDirection(Qt.RightToLeft)
    # modal dialogs would block forever with no user
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    w = MainWindow()
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


def test_every_page_builds_and_renders(win):
    app = QApplication.instance()
    assert win.stack.count() >= 8
    for i in range(win.stack.count()):
        win.stack.setCurrentIndex(i)
        app.processEvents()
        img = QImage(1320, 820, QImage.Format_ARGB32)
        img.fill(0)
        win.stack.widget(i).render(img)     # raises if the page is broken
    win.refresh()                           # what the 60s timer does on every page
    app.processEvents()


def test_the_wheel_cannot_change_a_money_field_it_only_scrolls_past(win):
    app = QApplication.instance()

    def wheel(w):
        return QWheelEvent(QPointF(5, 5), w.mapToGlobal(QPoint(5, 5)), QPoint(0, 0), QPoint(0, 120),
                           Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)

    spins = win.findChildren(QAbstractSpinBox)
    assert spins, "the settings page should have spin boxes"
    for w in spins:
        w.clearFocus()
        before = w.text()
        app.sendEvent(w, wheel(w))
        app.processEvents()
        assert w.text() == before, f"the wheel changed {w.objectName() or w} from {before} to {w.text()}"
    # deliberately focusing one still lets the wheel work
    w = spins[0]
    w.setFocus()
    app.processEvents()
    before = w.text()
    app.sendEvent(w, wheel(w))
    app.processEvents()
    assert w.text() != before


def test_every_preset_actually_applies_what_it_claims(win):
    """setCurrentText on a non-editable combo is a silent no-op when the value is not in the
    list, which is how the scalp preset spent several releases not setting its timeframe."""
    app = QApplication.instance()
    for kind, tf, agg in (("smart", "1d", "high"), ("serious", "1d", "normal"), ("scalp", "5m", "scalp")):
        win._preset(kind)
        app.processEvents()
        assert (win.s_tf.currentText(), win.s_agg.currentText()) == (tf, agg), \
            f"preset {kind} did not apply"


def test_a_chart_refresh_does_not_throw_the_view_away(win):
    import numpy as np
    import pandas as pd
    from trader.market.indicators import enrich

    def synth(n):
        rng = np.random.default_rng(4)
        close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
        opn = np.roll(close, 1); opn[0] = close[0]
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        return pd.DataFrame({"open": opn, "high": close * 1.004, "low": close * 0.996,
                             "close": close, "volume": rng.uniform(1, 9, n)}, index=idx)

    ch = win.chart
    ch.set_data(enrich(synth(400)), "X/Y", "1h", None, [])
    ch.visible, ch.offset = 60, 40                       # zoomed in and panned back
    ch.set_data(enrich(synth(401)), "X/Y", "1h", None, [])
    assert ch.visible == 60 and ch.offset == 41, "the pan must survive an auto-refresh"
    ch.set_data(enrich(synth(400)), "OTHER/Z", "1h", None, [])
    assert ch.offset == 0, "a different symbol starts at the newest bar"


def test_closing_the_window_cannot_abort_the_process():
    """A QThread still running when Python lets go of it makes Qt abort the whole process.

    This test used to iterate w._workers and w._retiring_feeds AFTER closeEvent had emptied
    both lists, so its loop body never ran: it passed for a whole release while the price feed
    was not being stopped at all. It now asserts on the module's own thread registry, which
    nothing clears, and only about the threads this window started.
    """
    from trader.gui.app import MainWindow, _join_threads, live_threads
    app = QApplication.instance() or QApplication([])
    # The registry is global and the module-scoped fixture keeps its own window - and its own
    # feed - open, so measure against a baseline rather than expecting an empty registry.
    baseline = set(live_threads())
    w = MainWindow()
    w.show()
    app.processEvents()
    mine = set(live_threads()) - baseline
    assert mine, "the window should have started at least one thread of its own"
    w.close()
    app.processEvents()
    still = _join_threads([t for t in live_threads() if t in mine], ms=5000)
    assert not still, f"threads survived the close path: {[type(t).__name__ for t in still]}"


def test_the_real_main_starts_and_exits_cleanly():
    """Runs trader.gui.app.main() in a subprocess and quits it from inside its own event loop -
    which is exactly what closing the window does. Checks the PROCESS exit code, because the
    failure mode here is a SIGABRT/SIGSEGV during teardown, not a Python exception: nothing is
    raised, nothing is logged, the window just disappears and Windows reports a crash."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        import os, sys, tempfile
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TGTRADER_HOME"] = tempfile.mkdtemp(prefix="mainexit-")
        # The rest of the suite switches the startup update check off; this test must NOT.
        # That check is the thread that reached the network, built an SSL context, and was
        # still in flight at shutdown - the exact case that crashed on Windows. Quitting at
        # 5s lands right on top of it.
        os.environ.pop("TGTRADER_NO_AUTOUPDATE", None)
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtCore import QTimer
        QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
        _exec = QApplication.exec
        def _patched(*a, **k):
            QTimer.singleShot(5000, QApplication.instance().quit)
            return _exec()
        QApplication.exec = staticmethod(_patched)
        import trader.gui.app as A
        sys.exit(A.main())
    ''')
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=180)
    # A POSIX signal shows up as a negative returncode; Windows reports an unhandled SEH
    # exception or an abort() as a large unsigned value (0xC0000409, 0xC0000005 and friends),
    # which the old message called simply "error" on the one platform where this crash was seen.
    if r.returncode < 0:
        how = f"killed by signal {-r.returncode}"
    elif r.returncode > 0x8000_0000 // 2:
        how = f"crashed, Windows status 0x{r.returncode & 0xFFFFFFFF:08X}"
    else:
        how = "exited with an error"
    assert r.returncode == 0, f"main() {how} (code {r.returncode})\n{r.stderr[-2000:]}"


def test_join_threads_never_terminates_a_thread():
    """QThread.terminate() kills a thread wherever it is. CI caught it killing one inside
    OpenSSL's create_default_context: "Windows fatal exception: access violation" - a corrupted
    process instead of a clean one. Waiting, then leaving via os._exit, is the safe pair."""
    import ast
    import inspect
    from trader.gui import app as A

    # Parse it, do not grep it: the explanation of why terminate() is absent naturally
    # contains the word, and a text search on the source calls that a violation.
    tree = ast.parse(inspect.getsource(A))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "terminate"]
    assert not calls, f"terminate() is called again at line(s) {[c.lineno for c in calls]}"

    class Stuck:
        def __init__(self): self.waited = 0
        def wait(self, ms): self.waited += 1; return False      # never finishes
        def terminate(self): raise AssertionError("must never be called")

    s = Stuck()
    still = A._join_threads([s], ms=1)
    assert still == [s], "a thread that will not stop must be REPORTED, not killed"
    assert s.waited == 1
