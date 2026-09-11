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
    """It could not be made narrower than 1617 physical pixels on the Windows build, which does
    not fit a 1366-pixel laptop screen at all - the user could not see the right-hand edge of
    their own window. Nothing asked for that floor; it was the sum of things that merely could
    not shrink: one-line card subtitles, the topbar's four labelled buttons and its page
    subtitle, and four pages that were not inside a scroll area and so imposed their widest
    control row on the whole window.

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
    """Below TOPBAR_COMPACT_W the four action buttons keep their icon and drop their label, and
    the full label moves into the tooltip - so nothing is lost but the room. Widening puts every
    word back, including the ones set at runtime (the run button says توقف while the engine is
    running, and the update button carries a version number)."""
    from trader.gui.app import TOPBAR_COMPACT_W
    app = QApplication.instance() or QApplication([])
    buttons = (win.btn_run, win.btn_kill, win.btn_reset, win.btn_update)

    win.resize(TOPBAR_COMPACT_W + 200, 820)
    for _ in range(4):
        app.processEvents()
    wide = {b: b.text() for b in buttons}
    assert all(len(t.split()) > 1 for t in wide.values()), f"expected worded labels: {wide}"

    win.resize(TOPBAR_COMPACT_W - 200, 820)
    for _ in range(4):
        app.processEvents()
    for b in buttons:
        assert b.text() == wide[b].split()[0], f"{wide[b]!r} did not compact to its icon"
        assert wide[b] in b.toolTip(), f"the words are gone and the tooltip does not carry them: {b.toolTip()!r}"

    win.resize(TOPBAR_COMPACT_W + 200, 820)
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
