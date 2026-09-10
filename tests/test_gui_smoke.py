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
        # Ask main() to report the stuck-thread escape hatch as 70 instead of 0, so this test
        # fails when the close path silently leaves a thread behind instead of joining it.
        os.environ["TGTRADER_STRICT_EXIT"] = "1"
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
    if r.returncode == 70:
        raise AssertionError(
            "main() had to leave through os._exit because a thread would not stop - the close "
            f"path did not actually join everything.\n{r.stderr[-2000:]}")
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


def test_reset_test_wipes_everything_and_sets_the_new_balance(win, monkeypatch):
    """One button: no open trades, no history, no equity curve, no leftover cooldowns, and the
    account back at a balance the user chooses."""
    from PySide6.QtWidgets import QInputDialog
    from trader.execution.paper import PaperBroker
    from trader.risk.manager import RiskManager

    app = QApplication.instance()
    db = win.db
    win.settings.mode = "paper"

    # a used account: an open trade, a closed one, equity points, decisions, a hot cooldown
    pb = PaperBroker(500.0)
    pb.reset(500.0)
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 95.0, 110.0, "t", "r",
                  entry_fee=fill.fee)
    tid = db.open_trade("paper", "A/B", "long", 1.0, 10.0, 9.0, 12.0, "t", "r")
    db.close_trade(tid, 11.0, 1.0, 1.0)
    db.record_equity("paper", 501.0)
    db.add_decision("X/Y", "buy", 0.9, "rules", "because")
    assert db.open_trades("paper") and db.closed_trades("paper") and db.equity_curve("paper")
    RiskManager(win.settings.risk, db, "paper").set_kill_switch(True)

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (1000.0, True)))
    win.reset_test()
    app.processEvents()

    assert db.open_trades("paper") == [], "open positions must be gone"
    assert db.closed_trades("paper") == [], "trade history must be gone"
    assert db.trade_stats("paper")["trades"] == 0, "the P&L statistics must be back to zero"
    curve = db.equity_curve("paper")
    assert len(curve) == 1 and curve[0][1] == 1000.0, "the equity curve restarts at the new balance"
    assert win.settings.paper_start_balance == 1000.0
    # and it must be on DISK, not only in memory: a balance that is right until the next
    # restart is the exact complaint this button exists to answer.
    from trader.config import Settings as _S
    assert _S.load().paper_start_balance == 1000.0, "the new balance did not survive a reload"
    assert PaperBroker(1000.0).cash() == 1000.0, "the broker's own state file must be reset too"
    assert not RiskManager(win.settings.risk, db, "paper").kill_switch_on(), \
        "an emergency stop left on from the last run would silently refuse every new trade"


def test_reset_test_refuses_to_touch_a_live_account(win, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    asked = {"n": 0}
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (asked.__setitem__("n", asked["n"] + 1), (1.0, True))[1]))
    win.settings.mode = "live"
    try:
        win.reset_test()
        assert asked["n"] == 0, "it must refuse before asking for a balance, not wipe a live journal"
    finally:
        win.settings.mode = "paper"


def test_a_closed_window_starts_no_more_background_work():
    """Joining the threads that exist at one instant is not a barrier. The periodic refresh is
    a child of the window and keeps firing after close(), and refresh() can start a chart load,
    so a fresh network thread could appear right after the close path finished waiting."""
    from trader.gui.app import MainWindow, live_threads, _join_threads
    app = QApplication.instance() or QApplication([])
    baseline = set(live_threads())
    w = MainWindow()
    w.show()
    app.processEvents()
    w.close()
    assert w._closing is True
    assert not w.timer.isActive(), "the refresh timer must not keep running on a closed window"
    # drive the event loop hard, and force the timer's own slot, the way a real session would
    for _ in range(5):
        app.processEvents()
    w.timer.timeout.emit()
    app.processEvents()
    assert w._run_bg(lambda: None, lambda _: None) is None, \
        "no new background work may be started once the window is closing"
    new = [t for t in live_threads() if t not in baseline]
    still = _join_threads(new, ms=5000)
    assert not still, f"threads survived or were started after close: {[type(t).__name__ for t in still]}"


def test_reset_while_the_engine_is_running_stops_it_wipes_and_restarts(win, monkeypatch):
    """The realistic case. Wiping the journal underneath a running loop is a race: the pass
    already in flight holds its own list of open positions and its own view of the cash."""
    from PySide6.QtWidgets import QInputDialog
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker

    app = QApplication.instance()
    win.settings.mode = "paper"
    win.settings.use_llm_for_decisions = False

    pb = PaperBroker(750.0)
    pb.reset(750.0)
    win.engine = Engine(win.settings, win.db, broker=pb)

    import numpy as np
    import pandas as pd
    from trader.market.indicators import enrich

    def synth(n=600):
        rng = np.random.default_rng(2)
        close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
        opn = np.roll(close, 1); opn[0] = close[0]
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        return enrich(pd.DataFrame({"open": opn, "high": close * 1.004, "low": close * 0.996,
                                    "close": close, "volume": rng.uniform(1, 9, n)}, index=idx))

    frame = synth()

    class Fake:
        is_kcex = False
        def candles(self, symbol, timeframe=None, limit=400, max_age=20.0): return frame
        def price(self, symbol): return float(frame["close"].iloc[-1])

    win.engine.market = Fake()
    win.engine.start()
    app.processEvents()
    assert win.engine.running()

    win.db.open_trade("paper", "X/Y", "long", 1.0, 100.0, 95.0, 110.0, "t", "r", entry_fee=0.1)
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (1000.0, True)))
    win.reset_test()
    app.processEvents()

    assert win.db.open_trades("paper") == [], "the wipe must have happened"
    assert win.engine.running(), "an engine that was running must be running again afterwards"
    assert isinstance(win.engine.broker, PaperBroker)
    assert win.engine.broker.cash() == 1000.0, "the restarted engine trades the new balance"
    assert win.engine._cooldown == {} and win.engine._llm_bar == {}, \
        "a reset means start over: no cooldowns or per-bar marks may carry across"
    win.engine.stop(wait=5.0)
    win.engine = None


def test_the_price_feed_does_not_ask_one_exchange_for_everything_every_second(win):
    """Eight symbols polled every second is eight requests a second at one exchange, which is
    what produced "Too Many Requests" and left the bot with no market data for the symbols it
    was holding. The chart on screen stays live; the rest take turns."""
    win.settings.symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
                            "XRP/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT"]
    win.ch_symbol.setCurrentText("DOGE/USDT")
    win._sync_watch_symbols()
    focus, rest = win._watch_split()
    assert "DOGE/USDT" in focus, "the symbol on screen must be polled every cycle"
    assert set(focus) & set(rest) == set(), "a symbol must not be in both halves"
    assert len(focus) + len(rest) == len(set(win._watch_symbols()))
    # one cycle asks for the focus symbols plus ONE of the others, not all eight
    per_cycle = len(focus) + (1 if rest else 0)
    assert per_cycle <= 4, f"{per_cycle} requests per cycle is still too many"
    # and every symbol is still reached within a few cycles
    assert per_cycle * (len(rest) or 1) >= len(win._watch_symbols())
