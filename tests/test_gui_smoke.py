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


def test_every_field_an_advisory_names_actually_exists_on_the_settings_page(win):
    """An advisory told the owner to change a percentage that had NO field in the window, and
    called another field by a name it does not have. "I can't find it" was the correct answer.
    Every name an advisory can print must be a label the settings page really shows."""
    import re
    from PySide6.QtWidgets import QLabel
    from trader.config import LABELS, Settings

    win.goto("settings")
    QApplication.instance().processEvents()
    on_screen = {lbl.text().strip() for lbl in win.findChildren(QLabel)}

    # every label the advisories can quote is on the settings page
    missing = [k for k, v in LABELS.items() if v not in on_screen and k != "symbols"]
    assert not missing, f"these settings have a name but no field: {missing}"

    # and every «quoted» name in a real advisory is one of those labels
    s = Settings()
    s.risk.capital_limit = 1000; s.risk.risk_per_trade = 0.10
    s.risk.max_open_risk = 0.06; s.risk.max_open_positions = 20
    s.risk.max_position_frac = 0.5
    s.symbols = ["BTC/USDT"] * 30
    advice = s.advisories()
    assert advice, "this configuration should produce advisories"
    quoted = {q for a in advice for q in re.findall(r"«([^»]+)»", a)}
    assert quoted, "an advisory that names no field cannot be acted on"
    unknown = quoted - set(LABELS.values())
    assert not unknown, f"advisories quote names that are not settings labels: {unknown}"
    # and each one is genuinely rendered
    not_rendered = [q for q in quoted if q not in on_screen and q != LABELS["symbols"]]
    assert not not_rendered, f"quoted but not on the page: {not_rendered}"


def test_the_settings_page_writes_back_the_largest_position_field(win):
    win.goto("settings")
    QApplication.instance().processEvents()
    win.s_posfrac.setValue(20.0)
    win.s_maxpos.setValue(5)
    win._save_settings()
    QApplication.instance().processEvents()
    assert win.settings.risk.max_position_frac == 0.2, "the new field must actually be saved"
    from trader.config import Settings as _S
    assert _S.load().risk.max_position_frac == 0.2, "and survive a reload"
    assert not [a for a in win.settings.advisories() if "بزرگ‌ترین پوزیشن" in a], \
        "5 positions at 20% each is consistent - it must stop warning"


def test_the_scan_page_turns_a_selection_into_the_symbol_list(win, monkeypatch):
    """The point of the page: choose symbols from numbers on screen instead of typing tickers
    from memory. If the selection does not reach settings.symbols, it is decoration."""
    from PySide6.QtWidgets import QMessageBox
    app = QApplication.instance()
    win.goto("scan")
    app.processEvents()

    win._scan_rows = [
        {"symbol": "AAA/USDT", "price": 1.0, "volume_usd": 50e6, "range_pct": 4.0, "change_pct": 2.0},
        {"symbol": "BBB/USDT", "price": 2.0, "volume_usd": 40e6, "range_pct": 5.0, "change_pct": -1.0},
        {"symbol": "CCC/USDT", "price": 3.0, "volume_usd": 30e6, "range_pct": 6.0, "change_pct": 0.5},
    ]
    win._fill_scan()
    app.processEvents()
    assert win.tbl_scan.rowCount() == 3 and win.tbl_scan.isVisible()

    # nothing selected -> it must say so, not silently wipe the symbol list
    before = list(win.settings.symbols)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    win.tbl_scan.clearSelection()
    win._scan_to_symbols()
    assert win.settings.symbols == before, "an empty selection must not replace the symbols"

    # Through the selection model, which is what a mouse click goes through. QTableWidget's
    # selectRow() convenience silently does nothing on a table that was hidden and re-shown,
    # so a test built on it passes while proving nothing.
    from PySide6.QtCore import QItemSelectionModel
    sm = win.tbl_scan.selectionModel()
    for row in (0, 2):
        sm.select(win.tbl_scan.model().index(row, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert len(sm.selectedRows()) == 2, "two rows should be selected"
    win._scan_to_symbols()
    app.processEvents()
    assert win.settings.symbols == ["AAA/USDT", "CCC/USDT"], win.settings.symbols
    from trader.config import Settings as _S
    assert _S.load().symbols == ["AAA/USDT", "CCC/USDT"], "the choice must survive a restart"
    assert win.ch_symbol.currentText() in ("AAA/USDT", "CCC/USDT"), "the chart combo follows"


def test_the_scan_page_admits_it_predicts_nothing(win):
    """This page shows numbers next to coins, which is exactly the shape of a recommendation.
    Volatility, momentum, ADX and past backtest were each tested as a way to pick coins and
    none survived, so the page has to say so where it is read - not only in a commit message."""
    from PySide6.QtWidgets import QLabel
    app = QApplication.instance()
    win.goto("scan")
    app.processEvents()
    text = " ".join(l.text() for l in win.stack.currentWidget().findChildren(QLabel))
    assert "توصیه نمی‌کند" in text, "the page must not read as a recommendation"
    assert "۰.۰۰۵" in text or "0.005" in text, "the measured correlation belongs on the page"


def test_a_number_wrapped_for_rtl_still_gets_its_colour():
    """fill() decides green or red from the first character. The bidi isolate that stops this
    right-to-left window reordering "+1.23" into "1.23+" is itself the first character, so the
    floating-P&L column quietly lost its colour the moment those cells were isolated."""
    from PySide6.QtGui import QColor
    from trader.gui.widgets import table, fill
    from trader.gui import theme
    from trader.gui.app import ltr, money_pct

    QApplication.instance() or QApplication([])
    t = table(["a"])
    fill(t, [[ltr("+21.0%")], [ltr("-1.9%")], [money_pct(3.5, 1.2)], [money_pct(-3.5, -1.2)],
             ["—"]], tones={0: "pnl"})
    green, red = QColor(theme.SUCCESS), QColor(theme.DANGER)
    assert t.item(0, 0).foreground().color() == green, "an isolated positive must still be green"
    assert t.item(1, 0).foreground().color() == red, "an isolated negative must still be red"
    assert t.item(2, 0).foreground().color() == green
    assert t.item(3, 0).foreground().color() == red
    # and a cell with no sign is left alone rather than coloured at random
    assert t.item(4, 0).foreground().color() not in (green, red)


def test_numbers_are_isolated_so_rtl_cannot_reverse_them():
    from trader.gui.app import ltr
    assert ltr("+21.0%") == "⁦+21.0%⁩"
    assert ltr("") == "", "an empty cell needs no wrapping"


def test_the_equity_curve_labels_cannot_run_into_each_other():
    """On a narrow card the two 200px corner boxes overlapped and "999.90" next to "1,000.12"
    was read off the screen as one number: 9990070.12. They also never said which numbers they
    were - first and last, or lowest and highest?"""
    from PySide6.QtGui import QImage, QPainter
    from trader.gui.widgets import EquityCurve

    QApplication.instance() or QApplication([])
    c = EquityCurve()
    c.set_points([(0.0, 999.90), (1.0, 1000.12)])
    for width in (180, 320, 900):
        c.resize(width, 140)
        img = QImage(width, 140, QImage.Format_ARGB32)
        img.fill(0)
        c.render(img)          # must not raise at any width
    # the labels say what they are, and the boxes are sized to their text rather than fixed
    src = __import__("inspect").getsource(EquityCurve.paintEvent)
    assert "شروع" in src and "اکنون" in src, "an unlabelled number is a number nobody can use"
    assert "horizontalAdvance" in src, "fixed-width boxes are what made them collide"


def test_paragraph_text_does_not_get_its_numbers_reversed():
    """In a right-to-left line, bidi moves a leading sign to the other end: "-6.4%" is read as
    "6.4%-". These are the sentences that quote measured results, so it changes what they say.

    Fixed where text reaches a widget rather than by hand at each string - wrapping them one at
    a time rots, because the next sentence someone writes brings the bug back."""
    from trader.gui.widgets import bidi_safe, hint

    QApplication.instance() or QApplication([])
    out = bidi_safe("۳ ارز حدود +۰.۵٪ و ۸ ارز -۶.۴٪ درآمد")
    assert out.count("⁦") == 2, out          # both signed figures, nothing else
    assert "⁦+۰.۵٪⁩" in out and "⁦-۶.۴٪⁩" in out

    # a bare number needs no help: digits are their own run and come out in order
    assert bidi_safe("۸ نماد و ۴۰۰ کندل") == "۸ نماد و ۴۰۰ کندل"
    # a unit alone is enough to need it
    assert "⁦۵۰٪⁩" in bidi_safe("۵۰٪ یعنی فقط ۲ تا")
    # and something already isolated is never nested
    assert bidi_safe("⁦+1.23 $⁩") == "⁦+1.23 $⁩"

    # the real path: a hint label gets it without the caller doing anything
    assert hint("بازده -۲۹.۹٪ بود").text().count("⁦") == 1


def test_no_table_column_is_narrower_than_what_is_in_it():
    """Every column used to be QHeaderView.Stretch - EQUAL width regardless of content - so in
    an eight-column table each cell got an eighth of the card. "ETH/USDT" arrived as ".../ETH",
    the reason column as "...scalp: mo", and once the P&L cell grew a percentage it became
    "-0.14 $...". Three truncation bugs that looked separate, one cause."""
    from trader.gui.widgets import table, fill
    app = QApplication.instance() or QApplication([])

    t = table(["نماد", "جهت", "ورود", "قیمت", "ارزش", "حد ضرر", "هدف", "سود شناور"])
    fill(t, [["ETH/USDT", "خرید", "2,452.06", "2,450.00", "50.02 $", "2,372.96", "2,627.56",
              "⁦-0.14 $ (-0.28%)⁩"],
             ["AVAX/USDT", "خرید", "7.4837", "7.4900", "50.00 $", "7.1392", "8.1728",
              "⁦+0.03 $ (+0.06%)⁩"]], tones={7: "pnl"})
    t.show()
    # Swept rather than sampled: the failure came back at the exact widths where the contents
    # almost fit, which three round numbers walk straight past.
    bad = {}
    for width in range(300, 1460, 20):
        t.resize(width, 200)
        app.processEvents()
        fill(t, [["ETH/USDT", "خرید", "2,452.06", "2,450.00", "50.02 $", "2,372.96", "2,627.56",
                  "⁦-0.14 $ (-0.28%)⁩"]], tones={7: "pnl"})
        app.processEvents()
        cut = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())
               if t.columnWidth(c) < t.sizeHintForColumn(c)]
        if cut:
            bad[width] = cut
    assert not bad, f"columns cut their contents at these widths: {bad}"


def test_closing_everything_says_what_it_costs(win, monkeypatch):
    """A confirmation that looks like every other confirmation gets answered from muscle
    memory. The dialog has to name what is about to be lost."""
    from PySide6.QtWidgets import QMessageBox
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker

    asked = {}
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda parent, title, text, *a, **k: (
                            asked.__setitem__("text", text), QMessageBox.No)[1]))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda parent, title, text, *a, **k: (
                            asked.__setitem__("info", text), QMessageBox.Ok)[1]))
    win.settings.mode = "paper"
    pb = PaperBroker(1000.0); pb.reset(1000.0)
    win.engine = Engine(win.settings, win.db, broker=pb)

    win.db.reset_mode("paper")
    win.close_all()
    assert "هیچ پوزیشن بازی نیست" in asked.get("info", ""), "an empty account must say so"

    f = pb.market_order("ETH/USDT", "buy", 0.02, 2450.0)
    win.db.open_trade("paper", "ETH/USDT", "long", f.qty, f.price, 2372.0, 2627.0, "t", "r",
                      entry_fee=f.fee)
    win.close_all()
    text = asked.get("text", "")
    assert "ETH/USDT" in text, "it must name the positions"
    assert "ارزش ورودی" in text and "شناور" in text, "and what they are worth"
    assert "برگشت‌پذیر نیست" in text
    assert win.db.open_trades("paper"), "answering No must close nothing"
    win.engine = None


def test_the_window_fits_a_small_laptop_screen(win):
    """It could not be made narrower than 1617 PHYSICAL pixels on the Windows build - about 1078
    logical, that display running at 150%. Nothing asked for that floor; it was the sum of things
    that merely could not shrink: one-line card subtitles, the topbar's four labelled buttons and
    its page subtitle, and four pages that were not inside a scroll area and so imposed their
    widest control row on the whole window.

    I first wrote that 1617 "does not fit a 1366px laptop". That was wrong - those were physical
    pixels, and a 1366 laptop at 100% has 1366 logical ones. The defects were real; the
    consequence I attached to them was not. What is true is that a window needing ~1078 logical
    pixels cannot share a screen and has nothing left over on a scaled display.

    Fonts differ between this box and Windows, so the number here is not the number there. What
    this pins is that no page and no card is allowed to be the floor - only the topbar is, and
    the topbar gives its words up on the way down.
    """
    app = QApplication.instance() or QApplication([])
    win.resize(900, 820)
    for _ in range(4):
        app.processEvents()
    assert win.minimumWidth() <= 820, f"window minimum is {win.minimumWidth()}px"
    # No PAGE may be the binding constraint. A page that cannot shrink is the bug that put the
    # floor at 1617 in the first place, and it comes back the moment someone adds a wide row.
    wide = {k: p.minimumSizeHint().width() for k, p in win.pages.items()
            if p.minimumSizeHint().width() > 250}
    assert not wide, f"these pages impose a width of their own: {wide}"


def test_the_topbar_gives_up_its_words_before_the_window_gives_up(win):
    """Below COMPACT_W the four action buttons keep their icon and drop their label, and
    the full label moves into the tooltip - so nothing is lost but the room. Widening puts every
    word back, including the ones set at runtime (the run button says توقف while the engine is
    running, and the update button carries a version number)."""
    from trader.gui.app import COMPACT_W
    app = QApplication.instance() or QApplication([])
    buttons = (win.btn_run, win.btn_kill, win.btn_reset, win.btn_update)

    win.resize(COMPACT_W + 200, 820)
    for _ in range(4):
        app.processEvents()
    wide = {b: b.text() for b in buttons}
    assert all(len(t.split()) > 1 for t in wide.values()), f"expected worded labels: {wide}"

    win.resize(COMPACT_W - 200, 820)
    for _ in range(4):
        app.processEvents()
    for b in buttons:
        assert b.text() == wide[b].split()[0], f"{wide[b]!r} did not compact to its icon"
        assert wide[b] in b.toolTip(), f"the words are gone and the tooltip does not carry them: {b.toolTip()!r}"

    win.resize(COMPACT_W + 200, 820)
    for _ in range(4):
        app.processEvents()
    assert {b: b.text() for b in buttons} == wide, "the words did not come back"


def test_a_table_never_sticks_out_of_the_card_it_is_in(win):
    """The narrow window turned one bug into another: columns stopped being squeezed below their
    contents, and instead the table drew wider than the card and was CLIPPED by it - the last two
    columns were not reachable at all, and there was no horizontal scrollbar, because as far as
    the table was concerned everything fitted.

    A table may scroll. It may not be wider than what contains it.
    """
    from trader.gui.widgets import fill
    app = QApplication.instance() or QApplication([])
    rows = [["ETH/USDT", "خرید", "2,457.83", "2,452.92", "50.02 $", "2,372.96", "2,627.56",
             "⁦-0.10 $ (-0.20%)⁩"],
            ["AVAX/USDT", "خرید", "7.4837", "7.4780", "50.02 $", "7.1392", "8.1728",
             "⁦-0.04 $ (-0.08%)⁩"]]
    t = win.tbl_positions
    # Qt does not lay out a widget on a page that is not showing, so on any other page this
    # table keeps the size it was born with and every width reads the same frozen number - a
    # test that measures that is measuring nothing. The `win` fixture is module-scoped and the
    # page it is left on depends on which tests ran first.
    win.show()
    win.goto("dashboard")
    over, seen = {}, set()
    for width in range(820, 1700, 20):
        win.resize(width, 820)
        t.show()
        fill(t, rows)
        for _ in range(3):
            app.processEvents()
        card = t.parentWidget()
        while card is not None and card.objectName() not in ("card", "cardAccent"):
            card = card.parentWidget()
        assert card is not None
        seen.add(card.width())
        if t.width() > card.width():
            over[width] = (t.width(), card.width())
    # The card has to have actually moved, or the loop proved nothing at all.
    assert len(seen) > 5, f"the layout never ran - the card was {seen} at every window width"
    assert not over, f"the table stuck out of its card at these window widths: {over}"


def test_the_positions_table_gets_the_whole_width_of_the_dashboard(win):
    """Positions and decisions used to share the row, half the window each. The positions table
    has eight columns; half of a MAXIMISED 2278px window was 916px and the table needed about
    950, so it did not fit even on a full screen - the symbol and the floating P&L could not be
    read at the same time, at any window size there is.

    Fonts differ between this box and Windows, so what is pinned here is the RATIO: the card
    takes essentially the whole content area rather than a share of it. A future side-by-side
    row brings the bug straight back and this is what catches it.
    """
    app = QApplication.instance() or QApplication([])
    win.show()
    win.goto("dashboard")
    win.resize(1366, 900)
    for t in (win.tbl_positions, win.tbl_decisions):
        t.show()
    for _ in range(5):
        app.processEvents()
    content = win.width() - 210          # the sidebar is the only fixed-width thing beside it
    for name, t in (("positions", win.tbl_positions), ("decisions", win.tbl_decisions)):
        card = t.parentWidget()
        while card is not None and card.objectName() not in ("card", "cardAccent"):
            card = card.parentWidget()
        assert card is not None
        assert card.width() > content * 0.85, (
            f"the {name} card is {card.width()}px of {content}px - it is sharing the row again")


def _capture_text(fn):
    """Run fn() and return every (rect, text, advance) that reached QPainter.drawText.

    Reading what was actually painted is the only way to check this: the labels are drawn, not
    laid out, so there is no widget to measure afterwards and a screenshot cannot tell a label
    that was dropped from one that was clipped to nothing.
    """
    from PySide6.QtGui import QPainter
    seen = []
    orig = QPainter.drawText

    def spy(self, *args):
        if len(args) == 3 and hasattr(args[0], "width") and isinstance(args[2], str):
            seen.append((args[0], args[2], self.fontMetrics().horizontalAdvance(args[2])))
        return orig(self, *args)

    QPainter.drawText = spy
    try:
        fn()
    finally:
        QPainter.drawText = orig
    return seen


def test_the_equity_curve_never_draws_half_a_number():
    """Clamping each corner label to half the card stopped them overlapping and introduced a
    worse bug: a clamped drawText CLIPS. At 1050px "شروع 999.77" was painted as "شروع 7", which
    does not read as a cut-off label - it reads as a balance of seven dollars.

    A missing number is honest. Half a number is a lie. So a label is drawn whole or not at all,
    and the percentage - what the card is for - is the last thing to go.
    """
    from PySide6.QtGui import QImage
    from trader.gui.widgets import EquityCurve

    app = QApplication.instance() or QApplication([])
    c = EquityCurve()
    c.set_points([(0.0, 999.77), (1.0, 1000.12)])
    cut, seen_pct = {}, 0
    for width in range(100, 920, 20):
        c.resize(width, 140)
        img = QImage(width, 140, QImage.Format_ARGB32)
        img.fill(0)
        drawn = _capture_text(lambda: c.render(img))
        app.processEvents()
        for rect, text, adv in drawn:
            if adv > rect.width() + 1:
                cut.setdefault(width, []).append((text, round(rect.width()), adv))
        if any("%" in t for _r, t, _a in drawn):
            seen_pct += 1
    assert not cut, f"text was painted into a box too small for it: {cut}"
    # and the percentage is what survives: it is shown at very nearly every width there is
    assert seen_pct >= 39, f"the percentage was dropped too eagerly - only {seen_pct} of 41 widths"


def test_the_chart_time_axis_thins_out_instead_of_piling_up():
    """Six labels whatever the width is, each in an 80px box. On a narrow window six dates ran
    together into "5-2026802512026600292062722609-08". The count has to come from how much room
    a label actually needs."""
    from PySide6.QtGui import QImage
    from trader.gui.chart import CandleChart
    import pandas as pd

    app = QApplication.instance() or QApplication([])
    n = 120
    idx = pd.date_range("2026-05-01", periods=n, freq="D")
    df = pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n,
                       "low": [99.0] * n, "close": [100.5] * n, "volume": [10.0] * n}, index=idx)
    ch = CandleChart()
    ch.set_data(df, "ETH/USDT", "1d")
    bad = {}
    for width in range(260, 1400, 40):
        ch.resize(width, 420)
        img = QImage(width, 420, QImage.Format_ARGB32)
        img.fill(0)
        drawn = _capture_text(lambda: ch.render(img))
        app.processEvents()
        axis = sorted((r for r, _t, _a in drawn if abs(r.y() - (420 - 22)) < 2),
                      key=lambda r: r.x())
        for a, b in zip(axis, axis[1:]):
            if a.x() + a.width() > b.x() + 1:
                bad.setdefault(width, []).append((round(a.x()), round(a.width()), round(b.x())))
        assert axis, f"no time axis was drawn at {width}px"
    assert not bad, f"time labels overlapped at these widths: {bad}"


def test_the_wheel_zooms_the_chart_only_after_it_is_clicked(win):
    """The chart used to zoom whenever the wheel merely passed over it - the same mistake
    WheelGuard exists to stop on the money fields, and worse on a narrow window where the chart
    is most of a page that needs scrolling. Reported from a real session as "the page is stuck"
    when it was quietly zooming instead."""
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    app = QApplication.instance() or QApplication([])
    ch = win.dash_chart
    ch.clearFocus()
    before = ch.visible

    def wheel():
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, 120),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        ch.wheelEvent(ev)
        return ev

    ev = wheel()
    assert ch.visible == before, "the wheel zoomed a chart nobody had clicked"
    assert not ev.isAccepted(), "the chart swallowed a wheel event the page needed for scrolling"

    ch.setFocus(Qt.MouseFocusReason)
    app.processEvents()
    if ch.hasFocus():                 # offscreen cannot always give focus; only assert if it did
        wheel()
        assert ch.visible != before, "a clicked chart still would not zoom"


def test_the_kpi_tiles_go_two_by_two_on_a_narrow_window(win):
    """Four tiles across a narrow window are about 245px each, and the note under the number is
    wider than that: "0 معامله بسته‌شده" was painted as "0 معامله بس" - clipped, with no ellipsis
    to say anything was missing. Two by two doubles the room and costs one row of height."""
    from trader.gui.app import COMPACT_W
    app = QApplication.instance() or QApplication([])
    win.show()
    win.goto("dashboard")

    def cells():
        # columnCount() only ever GROWS - it reports the highest index the layout has ever
        # used, so it still says 4 after the tiles have moved into 2 columns. Ask where each
        # widget actually is instead.
        g = win.kpi_grid
        out = {}
        for i in range(g.count()):
            row, col, _rs, _cs = g.getItemPosition(i)
            out[g.itemAt(i).widget()] = (row, col)
        return out

    win.resize(COMPACT_W + 200, 900)
    for _ in range(4):
        app.processEvents()
    wide = cells()
    assert set(wide) == set(win.kpis), "a tile went missing from the grid"
    assert {c for _r, c in wide.values()} == {0, 1, 2, 3}, f"expected one row of four: {wide}"

    win.resize(COMPACT_W - 200, 900)
    for _ in range(4):
        app.processEvents()
    narrow = cells()
    assert set(narrow) == set(win.kpis), "a tile was lost moving the grid around"
    assert {c for _r, c in narrow.values()} == {0, 1}, f"expected two columns: {narrow}"
    assert {r for r, _c in narrow.values()} == {0, 1}, f"expected two rows: {narrow}"


def test_the_spot_only_option_is_in_the_shape_ccxt_actually_reads():
    """`{"fetchMarkets": ["spot"]}` had been in this code for weeks with a comment explaining
    that it stops an exchange walking four market types. It never did anything: every one of
    these exchanges reads that option with safe_dict(), which returns None for a LIST and falls
    back to spot+swap+future+option. The proof it mattered is a Windows test run that exited
    0xC0000005 with a thread stuck in gate.fetch_future_markets.

    This drives ccxt's own fetch_markets with the four fetchers stubbed, so it checks the shape
    against the library rather than against my reading of it. No network.
    """
    import ccxt
    from trader.market.data import MarketData
    from trader.config import Settings

    s = Settings()
    s.exchange.exchange_id = "gate"
    md = MarketData(s)
    os.environ.pop("TGTRADER_OFFLINE", None)
    try:
        ex = md._ex("gate")
    finally:
        os.environ["TGTRADER_OFFLINE"] = "1"

    called = []
    for name in ("fetch_spot_markets", "fetch_swap_markets", "fetch_future_markets",
                 "fetch_option_markets"):
        setattr(ex, name, (lambda n: (lambda *a, **k: (called.append(n), [])[1]))(name))
    ex.fetch_markets()
    assert called == ["fetch_spot_markets"], f"load_markets would fetch {called}"

    # and the bare list really is the broken shape, so this test cannot pass by accident
    loose = ccxt.gate({"options": {"defaultType": "spot", "fetchMarkets": ["spot"]}})
    seen = []
    for name in ("fetch_spot_markets", "fetch_swap_markets", "fetch_future_markets",
                 "fetch_option_markets"):
        setattr(loose, name, (lambda n: (lambda *a, **k: (seen.append(n), [])[1]))(name))
    loose.fetch_markets()
    assert len(seen) == 4, "the list form was supposed to be the bug; it no longer is"

    # bybit is NOT affected either way, and that is worth pinning: the Windows session measured
    # no change in the self-test timings after this fix, and this is why - bybit is the
    # configured source there. Nobody should come back expecting a speed-up from this.
    both = []
    for opt in (["spot"], {"types": ["spot"]}):
        ex2 = ccxt.bybit({"options": {"defaultType": "spot", "fetchMarkets": opt}})
        got = []
        for name in ("fetch_spot_markets", "fetch_swap_markets", "fetch_future_markets",
                     "fetch_option_markets"):
            if hasattr(ex2, name):
                setattr(ex2, name, (lambda n: (lambda *a, **k: (got.append(n), [])[1]))(name))
        ex2.fetch_markets()
        both.append(got)
    assert both[0] == both[1] == ["fetch_spot_markets"], f"bybit behaviour: {both}"


def test_no_test_can_reach_the_network():
    """A GUI test builds a real window and its chart page starts a candle fetch. On Windows that
    thread was still in an SSL read when the suite ended, the teardown fell through to os._exit,
    and tearing an OpenSSL thread down where it stands is itself an access violation: 88 passed,
    exit 0xC0000005, three runs out of three.

    A test that reaches the internet is not testing this program anyway."""
    from trader.market.data import MarketData, offline
    from trader.config import Settings

    assert offline(), "the suite must run with TGTRADER_OFFLINE set"
    md = MarketData(Settings())
    for call in (lambda: md._ex("gate"), lambda: md.kcex):
        try:
            call()
        except RuntimeError as exc:
            assert "OFFLINE" in str(exc)
        else:
            raise AssertionError("a network client was built inside the test suite")


def test_a_scrollbar_that_cannot_scroll_is_not_shown(win):
    """At 1366px every column of the positions table was on screen and there was still a bar
    under it, which dragged nowhere - ResizeToContents pads each section slightly past its hint,
    so the total lands a few pixels over the viewport with nothing actually hidden. A bar that
    says "there is more" when there is not is worse than no bar."""
    from trader.gui.widgets import fill, DEAD_SCROLL
    app = QApplication.instance() or QApplication([])
    rows = [["ETH/USDT", "خرید", "2,457.83", "2,452.92", "50.02 $", "2,372.96", "2,627.56",
             "⁦-0.10 $ (-0.20%)⁩"]]
    win.show()
    win.goto("dashboard")
    t = win.tbl_positions
    useless, useful = {}, 0
    for width in range(820, 1700, 20):
        win.resize(width, 900)
        t.show()
        fill(t, rows)
        for _ in range(3):
            app.processEvents()
        sb = t.horizontalScrollBar()
        if sb.isVisible() and sb.maximum() <= DEAD_SCROLL:
            useless[width] = sb.maximum()
        if sb.isVisible():
            useful += 1
    assert not useless, f"an inert scrollbar was shown at these widths: {useless}"


def test_a_card_subtitle_uses_the_card_it_is_in(win):
    """Wrapping the subtitles is what let the window get narrow, and it came with a regression:
    a word-wrapping QLabel reports a deliberately NARROW size hint - it aims for a readable
    block rather than a long line - so with a stretch after it swallowing the leftover, the
    subtitle wrapped into a ~250px column inside a 1600px card. Three lines where there was
    room for one, and Persian words broken in half: "دلیلش ر" / "بخوان".

    A broken word is the same class of damage as a clipped number: it changes what the text
    says, not just how it looks."""
    from trader.gui.widgets import Card
    app = QApplication.instance() or QApplication([])
    win.show()
    win.goto("dashboard")
    win.resize(1366, 900)
    for _ in range(6):
        app.processEvents()
    wrapped = {}
    for c in win.pages["dashboard"].widget().findChildren(Card):
        lbl = c.sub_lbl
        if not (lbl.isVisible() and lbl.text()):
            continue
        need = lbl.fontMetrics().horizontalAdvance(lbl.text())
        # only a card that HAS the room is at fault; a genuinely narrow card may wrap
        if c.width() > need + 120 and lbl.width() < need:
            wrapped[lbl.text()[:24]] = (c.width(), lbl.width(), need)
    assert not wrapped, f"subtitles wrapped inside cards with room to spare: {wrapped}"


def test_the_chart_header_is_never_cut_into_a_number():
    """At 1366px "BTC/USDT  1d  76,865.40  -5.21%" did not fit its box, and the right-to-left
    window cut it from the FRONT: ":65.40  -5.21%" was left on screen. Nobody reads that as a
    truncated header - they read a price of 65.40.

    Same damage as "شروع 7" on the equity curve, so the same rule: the header is built up until
    the box is full, never written out and cut. The symbol and timeframe always stay."""
    from PySide6.QtGui import QImage
    from trader.gui.chart import CandleChart
    import pandas as pd

    app = QApplication.instance() or QApplication([])
    n = 140
    idx = pd.date_range("2026-05-01", periods=n, freq="D")
    df = pd.DataFrame({"open": [80000.0] * n, "high": [81000.0] * n, "low": [76000.0] * n,
                       "close": [76865.4] * n, "volume": [10.0] * n}, index=idx)
    ch = CandleChart()
    ch.set_data(df, "BTC/USDT", "1d")
    cut, kept_symbol = {}, 0
    widths = range(260, 1500, 40)
    for width in widths:
        ch.resize(width, 420)
        img = QImage(width, 420, QImage.Format_ARGB32)
        img.fill(0)
        drawn = _capture_text(lambda: ch.render(img))
        app.processEvents()
        head = [(r, t, a) for r, t, a in drawn if t.startswith("BTC/USDT") or t.startswith("BTC")]
        assert head, f"no header drawn at {width}px"
        for rect, text, adv in head:
            if adv > rect.width() + 1:
                cut[width] = (text, round(rect.width()), adv)
            if "BTC" in text:
                kept_symbol += 1
    assert not cut, f"the header was painted into a box too small for it: {cut}"
    assert kept_symbol == len(list(widths)), "the symbol was dropped at some width"


def test_the_trade_analysis_view_draws_the_chart_as_it_was(win):
    """Clicking a closed trade has to open the chart AS IT WAS at the time, with the reasoning
    under it. Drawn from the bars stored with the trade, never from a fresh fetch: a chart pulled
    today shows what happened afterwards, which makes every entry look either obvious or stupid
    with information the bot did not have.

    This builds the real dialog rather than checking that a method exists - the view was the
    thing the owner asked for, and a view nobody has looked at is not a view."""
    from trader.config import Settings
    from trader.db import Database
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker
    import pandas as pd, numpy as np

    app = QApplication.instance() or QApplication([])
    # a market with a real trend, so the built-in rules actually take something
    n = 900
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0012, 0.02, n)))
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": np.full(n, 100.0)}, index=idx)

    class FakeMarket:
        def __init__(self, d): self.df = d; self.i = 300
        def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
            self.i = min(self.i + 1, len(self.df)); return self.df.iloc[:self.i].tail(limit)
        def price(self, symbol): return float(self.df["close"].iloc[self.i - 1])

    s = Settings(); s.mode = "paper"; s.symbols = ["X/Y"]; s.use_llm_for_decisions = False
    s.risk.capital_limit = 1000; s.paper_start_balance = 1000
    import pathlib
    db = Database(pathlib.Path(tempfile.mkdtemp(prefix="tg-analysis-")) / "a.db")
    try:
        pb = PaperBroker(1000); pb.reset(1000)
        eng = Engine(s, db, broker=pb)
        eng.market = FakeMarket(df)
        for _ in range(550):
            eng.loop_once()
        closed = db.closed_trades("paper")
        assert closed, "the harness closed no trade, so the view has nothing to show"

        # The `win` fixture is MODULE-scoped: whatever this test points it at, the next test
        # inherits. An earlier version left win.db pointing at the database closed in the
        # finally below, and the settings test after it died on "Cannot operate on a closed
        # database" - a failure with nothing to do with the code it was testing.
        keep_db, keep_settings = win.db, win.settings
        win.db, win.settings = db, s
        win._closed_rows = [dict(r) for r in closed]
        dlg = win._show_trade_analysis(0, show=False)
        assert dlg is not None, "the dialog was not built"
        app.processEvents()

        assert dlg._chart.df is not None and len(dlg._chart.df) > 20, "the chart got no candles"
        assert dlg._chart.position, "entry / stop / target were not handed to the chart"
        html = dlg._body.toPlainText()
        for must in ("تحلیل ورود", "تحلیل خروج", "چرا این حد ضرر", "چرا این هدف", "نتیجه"):
            assert must in html, f"the analysis text is missing «{must}»"
        assert "None" not in html, "an unfilled value reached the reader"
        # the chart must stop AT the trade, never after it
        last_bar = float(dlg._chart.df.index[-1].timestamp())
        assert last_bar <= float(closed[0]["closed_at"]) + 1
        dlg.deleteLater()
    finally:
        win.db, win.settings = keep_db, keep_settings
        win._closed_rows = []
        db.close()


def test_the_market_watch_switch_actually_saves(win):
    """A settings control that does not reach the file is the shape this project keeps finding:
    the ATR stop multiple sat on this page for weeks deciding nothing, and the ccxt spot-only
    option was in the wrong shape for as long. So the switch is checked through the real save
    path and through a reload from disk, not by reading the widget back."""
    from trader.config import Settings as _S
    win.goto("settings")
    QApplication.instance().processEvents()
    win.s_auto_sym.setChecked(True)
    win.s_auto_n.setValue(6)
    win.s_auto_every.setValue(30)
    win._save_settings()
    QApplication.instance().processEvents()
    assert win.settings.auto_symbols is True
    assert win.settings.auto_symbols_count == 6
    assert win.settings.auto_symbols_every_min == 30
    back = _S.load()
    assert back.auto_symbols is True and back.auto_symbols_count == 6 \
        and back.auto_symbols_every_min == 30, "the watch settings did not survive a reload"

    win.s_auto_sym.setChecked(False)
    win._save_settings()
    QApplication.instance().processEvents()
    assert _S.load().auto_symbols is False, "and it must switch back off again"


def test_the_engine_asks_the_watch_only_when_it_is_switched_on(win):
    """With the watch off, the typed list is what trades - a sweep must not quietly replace it.
    With it on, the sweep's picks are what the loop reads."""
    from trader.config import Settings
    from trader.engine import Engine
    from trader.execution.paper import PaperBroker
    from trader.market.watchlist import Watch
    from trader.db import Database
    import pathlib

    s = Settings(); s.mode = "paper"; s.symbols = ["AAA/USDT", "BBB/USDT"]
    s.use_llm_for_decisions = False
    db = Database(pathlib.Path(tempfile.mkdtemp(prefix="tg-watch-")) / "w.db")
    try:
        eng = Engine(s, db, broker=PaperBroker(1000))
        eng._watch = Watch(symbols=["ZZZ/USDT"], rows=[], at=1.0, note="")
        s.auto_symbols = False
        assert eng.watch_symbols() == ["AAA/USDT", "BBB/USDT"], "the watch overrode a typed list"
        s.auto_symbols = True
        assert eng.watch_symbols() == ["ZZZ/USDT"]
        # and with the watch on but no sweep finished yet, the typed list still runs the engine
        eng._watch = None
        assert eng.watch_symbols() == ["AAA/USDT", "BBB/USDT"], \
            "an unfinished sweep must not leave the engine with nothing to trade"
    finally:
        db.close()
