"""TGTrader desktop app - sidebar navigation, Persian RTL, dark/gold design system.

The engine runs in its own thread; the window only reads state and issues commands.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QEvent
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QSplitter, QProgressDialog, QTextBrowser, QStackedWidget, QScrollArea, QButtonGroup, QFrame,
    QAbstractSpinBox, QSlider, QInputDialog,
)

from .. import __version__, updater
from ..brain import make_brain, claude_client
from ..config import Settings, LABELS, NO_API_EXCHANGES
from ..db import Database
from ..engine import Engine
from ..knowledge.skills import load_seed_skills, add_extracted, active_skills
from . import theme
from .chart import CandleChart
from .help_fa import HELP_HTML
from .widgets import Card, Kpi, pill, set_pill, hint, section, FormRow, Empty, table, fill, EquityCurve, button

NAV = [
    ("dashboard", "🏠", "داشبورد", "وضعیت حساب، پوزیشن‌ها و تصمیم‌های ربات"),
    ("desk", "🖥", "میز معامله", "چارت بالا، پوزیشن‌های باز پایین - همه‌جا محلی"),
    ("chart", "📈", "چارت", "همان کندل‌هایی که ربات با آن‌ها تصمیم می‌گیرد"),
    ("trades", "🧾", "معاملات", "تاریخچه و آمار معاملات بسته‌شده"),
    ("skills", "🧠", "مهارت‌ها", "قوانینی که ربات با آن‌ها معامله می‌کند"),
    ("learn", "📚", "یادگیری", "کتاب، مقاله یا چت: به ربات قانون یاد بده"),
    ("backtest", "⏮", "بک‌تست", "قوانین پایه روی داده‌ی گذشته"),
    ("selftest", "🧪", "تست سیستم", "همه‌ی بخش‌ها را روی همین سیستم امتحان کن و گزارش را کپی کن"),
    ("settings", "⚙", "تنظیمات", "هوش مصنوعی، صرافی، ریسک، کنترل صفحه"),
    ("help", "📖", "راهنما", "همه چیز از نصب تا معامله‌ی واقعی"),
]


# ---------------------------------------------------------------- threading helpers
# Every QThread this app starts, so the exit path can find them ALL.
#
# QApplication.findChildren(QThread) was used for this and returns an empty list: both thread
# classes here call super().__init__() with no parent, so they are not children of anything Qt
# can walk. The shutdown guard built on it looked thorough and was doing nothing whatsoever.
_LIVE_THREADS: set = set()


def live_threads() -> list:
    """Threads that have been started and have not finished. Used by the exit path and by the
    test suite's session teardown."""
    return [t for t in list(_LIVE_THREADS) if _is_running(t)]


def _is_running(t) -> bool:
    try:
        return bool(t.isRunning())
    except RuntimeError:
        return False          # already deleted on the C++ side


class _Tracked(QThread):
    """A QThread that puts itself in _LIVE_THREADS for as long as it is running."""

    def start(self, *a, **k):
        _LIVE_THREADS.add(self)
        super().start(*a, **k)

    def _retire(self):
        _LIVE_THREADS.discard(self)


class Worker(_Tracked):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.finished.connect(self._retire)

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as exc:
            self.failed.emit(f"{exc}\n{traceback.format_exc(limit=2)}")


# U+2066 LEFT-TO-RIGHT ISOLATE ... U+2069 POP DIRECTIONAL ISOLATE. The whole app runs RTL, and
# bidi reordering turns "+1.23$ (+0.45%)" into something that reads as a different number - the
# sign ends up on the wrong end and the two figures swap. Isolating the run fixes it for good.
def profit_factor(st: dict) -> str:
    """Profit factor for display, or "—" when the number would only mislead.

    With wins and no losses yet the arithmetic gives infinity, and the dashboard printed "∞"
    next to a 100% win rate after ONE trade. That reads as a flawless system; it means the
    sample is too small to divide by. The bot's own skills put the minimum at thirty trades."""
    n = int(st.get("trades") or 0)
    pf = st.get("profit_factor")
    if not n or pf is None or pf == float("inf"):
        return "—"
    return f"{pf:.2f}"


def money_pct(amount: float | None, pct: float | None) -> str:
    if amount is None:
        return "—"
    body = f"{amount:+,.2f} $" + (f" ({pct:+.2f}%)" if pct is not None else "")
    return "\u2066" + body + "\u2069"


class WheelGuard(QObject):
    """Stops the mouse wheel changing a spin box, a combo or a slider it merely passes over.

    Every settings field on this page is money - capital limit, risk per trade, percent per
    trade. Scrolling the page with the cursor over one of them silently changed it, and the
    next thing the engine did was size a position from the new number. A field must be focused
    (clicked into) before the wheel touches it."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox, QSlider)):
            if not obj.hasFocus():
                ev.ignore()
                return True          # let the scroll area have it instead
        return False


class Bridge(QObject):
    event = Signal(str)
    confirm_request = Signal(str)


class PriceFeed(_Tracked):
    """Streams the latest price for the watched symbols about once a second, so the chart,
    the stop/target zones and the floating P&L move live. One second is the fastest that is
    safe against an exchange's request limits - true millisecond ticks are not possible over
    a REST price API and the price does not actually change that often."""
    tick = Signal(dict)

    def __init__(self, settings, split_fn):
        super().__init__()
        self._settings = settings
        self._split_fn = split_fn      # () -> (symbols on screen, everything else)
        self._stop = threading.Event()
        self.finished.connect(self._retire)

    def run(self):
        from ..market.data import MarketData
        if self._stop.is_set():
            return
        try:
            md = MarketData(self._settings)
        except Exception:
            return
        # Let the data layer abandon its fallback chain the moment we are asked to stop, instead
        # of walking six sources at 20 seconds each while the window waits to close.
        md.abort = self._stop.is_set
        if self._stop.is_set():
            return          # asked to stop while the exchange was still loading
        turn = 0
        backoff = 0.0
        while not self._stop.is_set():
            focus, rest = self._split_fn()
            # The chart the user is actually looking at stays live every cycle; the rest take
            # turns, one per cycle. Asking for all eight symbols every second was eight requests
            # a second at one exchange, which is what produced "Too Many Requests" and left the
            # bot with no market data at all for the symbols it holds.
            batch = list(focus)
            if rest:
                batch.append(rest[turn % len(rest)])
                turn += 1
            out, failed = {}, False
            for sym in batch:
                if self._stop.is_set():
                    break
                try:
                    out[sym] = md.price(sym)
                except Exception:
                    failed = True
            if out:
                self.tick.emit(out)
            # Back off when the source is unhappy, so a rate limit is not answered by asking
            # harder. Recovers as soon as a cycle succeeds.
            backoff = min(backoff * 2 + 1.0, 20.0) if failed and not out else 0.0
            self._stop.wait(1.0 + backoff)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TGTrader {__version__}")
        self.resize(1320, 820)
        self.settings = Settings.load()
        self.db = Database()
        load_seed_skills(self.db)
        self.engine: Engine | None = None
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.bridge.confirm_request.connect(self._on_confirm_request)
        self._confirm_result: dict[str, Any] = {}
        self._workers: list[Worker] = []
        self.teach_history: list[dict[str, str]] = []
        self._pending_update: updater.Release | None = None
        self._quit_for_update = False
        self._closing = False
        self._retiring_feeds: list[PriceFeed] = []   # feeds asked to stop that have not yet
        self._chart_last = 0.0
        self._dash_chart_last = 0.0
        self._pos_data: list[dict] = []
        self._detail_symbol: str | None = None
        self._live: dict[str, float] = {}
        self._market = None          # shared MarketData so exchange metadata loads once

        root = QWidget(); self.setCentralWidget(root)
        h = QHBoxLayout(root); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        h.addWidget(self._sidebar())
        col = QVBoxLayout(); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(0)
        col.addWidget(self._topbar())
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        builders = {"dashboard": self._page_dashboard, "desk": self._page_desk, "chart": self._page_chart, "trades": self._page_trades,
                    "skills": self._page_skills, "learn": self._page_learn, "backtest": self._page_backtest,
                    "settings": self._page_settings, "help": self._page_help, "selftest": self._page_selftest}
        for key, *_ in NAV:
            w = builders[key](); self.pages[key] = w; self.stack.addWidget(w)
        col.addWidget(self.stack, 1)
        h.addLayout(col, 1)

        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1500)
        self.refresh()
        self.goto("dashboard")
        self._feed: PriceFeed | None = None
        self._start_feed()
        if not os.environ.get("TGTRADER_NO_AUTOUPDATE"):
            # `self` is the CONTEXT object, not just a closure target. Without it the timer
            # belongs to nobody: it was measured firing five seconds AFTER the window had been
            # closed and its threads joined, starting a fresh network thread on a dead window
            # that nothing could ever join. That is the thread that crashed the Windows build.
            QTimer.singleShot(4000, self, lambda: self._check_update(manual=False))

    # ------------------------------------------------------------ frame: sidebar + topbar
    def _sidebar(self) -> QWidget:
        side = QFrame(); side.setObjectName("sidebar"); side.setFixedWidth(210)
        v = QVBoxLayout(side); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        brand = QLabel("TGTrader"); brand.setObjectName("brand"); v.addWidget(brand)
        sub = QLabel("دستیار ترید هوشمند"); sub.setObjectName("brandSub"); v.addWidget(sub)
        self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, icon, label, _ in NAV:
            b = QPushButton(f"{icon}   {label}"); b.setObjectName("navBtn"); b.setCheckable(True); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, k=key: self.goto(k))
            self.nav_group.addButton(b); v.addWidget(b); self.nav_buttons[key] = b
            if key == "backtest" or key == "selftest":
                v.addSpacing(10)
        v.addStretch()
        self.side_status = pill("متوقف", "muted"); v.addWidget(self.side_status, 0, Qt.AlignHCenter)
        # One version, always. The "(exe N)" suffix existed for the code-overlay layer, where
        # the running code and the installed exe could genuinely differ; that layer is gone, so
        # the suffix could never render and only invited the question of which number was real.
        foot = QLabel(f"نسخه {__version__}")
        foot.setObjectName("sideFoot"); foot.setAlignment(Qt.AlignCenter); v.addWidget(foot)
        return side

    def _topbar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("topbar"); bar.setFixedHeight(76)
        h = QHBoxLayout(bar); h.setContentsMargins(22, 0, 22, 0); h.setSpacing(14)
        tcol = QVBoxLayout(); tcol.setSpacing(0)
        self.page_title = QLabel("داشبورد"); self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel(""); self.page_sub.setObjectName("pageSub")
        tcol.addWidget(self.page_title); tcol.addWidget(self.page_sub)
        h.addLayout(tcol); h.addStretch()
        self.pill_mode = pill("کاغذی", "gold"); h.addWidget(self.pill_mode)
        self.pill_state = pill("متوقف", "muted"); h.addWidget(self.pill_state)
        self.btn_run = button("▶  شروع", "primary", self.toggle_engine); h.addWidget(self.btn_run)
        self.btn_kill = button("⛔ اضطراری", "ghost", self.toggle_kill); self.btn_kill.setToolTip("هیچ معامله‌ی جدیدی باز نمی‌شود تا خاموشش کنی"); h.addWidget(self.btn_kill)
        self.btn_reset = button("🧪 ریست تست", "ghost", self.reset_test)
        self.btn_reset.setToolTip("پاک‌کردن معامله‌ها، سود/زیان و نمودار سرمایه، و شروع دوباره با موجودی دلخواه")
        h.addWidget(self.btn_reset)
        self.btn_update = button("🔄", "ghost", lambda: self._check_update(manual=True)); self.btn_update.setToolTip("بررسی و نصب خودکار نسخه‌ی جدید"); h.addWidget(self.btn_update)
        return bar

    def goto(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        self.nav_buttons[key].setChecked(True)
        for k, _, label, sub in NAV:
            if k == key:
                self.page_title.setText(label); self.page_sub.setText(sub)
        if key == "chart" and time.time() - self._chart_last > 30:
            self.refresh_chart()
        if key == "desk" and time.time() - getattr(self, "_desk_chart_last", 0) > 30:
            self.refresh_desk_chart()
        if key == "skills":
            self.refresh_skills()
        if key == "learn":
            self.refresh_docs()

    # ------------------------------------------------------------ helpers
    def _run_bg(self, fn: Callable[[], Any], on_done: Callable[[Any], None], on_fail: Callable[[str], None] | None = None) -> Worker | None:
        if self._closing:
            # Nothing new once the window is going. Joining the threads that exist at one
            # instant is not a barrier: the periodic refresh kept running on the closed window
            # and re-armed a chart load, so a fresh network thread started right after the
            # close path had finished waiting for the old ones.
            return None
        w = Worker(fn)
        w.done.connect(on_done)
        w.failed.connect(on_fail or (lambda m: QMessageBox.critical(self, "خطا", m.splitlines()[0])))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start()
        return w

    @staticmethod
    def _ts(ts: float | None) -> str:
        return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""

    def _scroll(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(inner); sa.setFrameShape(QFrame.NoFrame)
        return sa

    # ============================================================ DASHBOARD
    def _page_dashboard(self) -> QWidget:
        inner = QWidget(); v = QVBoxLayout(inner); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(14)

        # Settings that are legal but fight each other. The symptom without this is a bot that
        # opens one trade and then refuses every other one, with the reason buried in a
        # truncated column nobody reads.
        self.lbl_advice = QLabel(""); self.lbl_advice.setObjectName("advice")
        self.lbl_advice.setWordWrap(True); self.lbl_advice.setVisible(False)
        v.addWidget(self.lbl_advice)

        self.card_setup = Card("راه‌اندازی", "سه قدم تا اولین معامله‌ی کاغذی", accent=True)
        self.setup_rows: dict[str, QLabel] = {}
        for key, text in (("ai", "کلید هوش مصنوعی را در تنظیمات وارد کن و «تست اتصال» را بزن"),
                          ("exchange", "صرافی و نمادها را انتخاب کن"),
                          ("risk", "سقف سرمایه و ریسک هر معامله را تعیین کن")):
            row = QHBoxLayout(); lbl = QLabel(text); st = pill("انجام نشده", "warn")
            row.addWidget(st); row.addWidget(lbl); row.addStretch()
            self.setup_rows[key] = st; self.card_setup.add_layout(row)
        self.card_setup.add_action(button("رفتن به تنظیمات", "primary", lambda: self.goto("settings")))
        v.addWidget(self.card_setup)

        k = QHBoxLayout(); k.setSpacing(12)
        self.kpi_equity = Kpi("سرمایه", "—", "کل ارزش حساب", "gold")
        self.kpi_daily = Kpi("سود/زیان امروز", "—", "سقف زیان روزانه اعمال می‌شود")
        self.kpi_open = Kpi("پوزیشن باز", "0", "از حداکثر مجاز")
        self.kpi_win = Kpi("نرخ برد", "—", "روی معاملات بسته‌شده")
        for x in (self.kpi_equity, self.kpi_daily, self.kpi_open, self.kpi_win):
            k.addWidget(x)
        v.addLayout(k)

        mid = QHBoxLayout(); mid.setSpacing(12)
        c1 = Card("بازار", "نماد اول تنظیمات، تایم‌فریم معامله")
        self.dash_chart = CandleChart(); self.dash_chart.setMinimumHeight(300); c1.add(self.dash_chart, 1)
        c1.add_action(button("چارت کامل", "ghost", lambda: self.goto("chart")))
        c2 = Card("منحنی سرمایه", "ارزش حساب در طول زمان")
        self.eq_curve = EquityCurve(); c2.add(self.eq_curve, 1)
        self.lbl_stats_mini = hint(""); c2.add(self.lbl_stats_mini)
        mid.addWidget(c1, 3); mid.addWidget(c2, 2)
        v.addLayout(mid)

        low = QHBoxLayout(); low.setSpacing(12)
        c3 = Card("پوزیشن‌های باز")
        self.tbl_positions = table(["نماد", "جهت", "ورود", "قیمت", "ارزش", "حد ضرر", "هدف", "سود شناور"])
        self.tbl_positions.setTextElideMode(Qt.ElideNone)
        self.tbl_positions.setMinimumHeight(160)
        self.empty_pos = Empty("پوزیشن بازی نیست. وقتی شرایط ورود جور شود، این‌جا ظاهر می‌شود.")
        self.tbl_positions.setToolTip("روی یک ردیف بزن تا جزئیات کامل باز شود")
        self.tbl_positions.cellClicked.connect(lambda r, _c: self._show_pos_detail(r))
        self.tbl_positions.cellDoubleClicked.connect(lambda r, _c: self._open_pos_chart(r))
        c3.add(self.tbl_positions); c3.add(self.empty_pos)
        self.pos_detail = QFrame(); self.pos_detail.setObjectName("card")
        pd = QVBoxLayout(self.pos_detail); pd.setContentsMargins(12, 10, 12, 10); pd.setSpacing(8)
        self.pos_detail_lbl = QLabel(""); self.pos_detail_lbl.setWordWrap(True); self.pos_detail_lbl.setTextFormat(Qt.RichText)
        pd.addWidget(self.pos_detail_lbl)
        pdbtn = QHBoxLayout()
        self.btn_pos_chart = button("📈 نمایش چارت این ارز", "primary", self._detail_to_chart)
        self.btn_pos_close = button("✕ بستن این پوزیشن", "danger", self._detail_close_pos)
        pdbtn.addWidget(self.btn_pos_chart); pdbtn.addWidget(self.btn_pos_close); pdbtn.addStretch()
        b_hide = button("▲ بستن جزئیات", "ghost", lambda: self._hide_pos_detail()); pdbtn.addWidget(b_hide)
        pd.addLayout(pdbtn)
        self.pos_detail.hide()
        c3.add(self.pos_detail)
        c3.add_action(button("بستن همه", "danger", self.close_all))
        c3.add_action(button("🧪 ریست تست", "ghost", self.reset_test))
        c4 = Card("آخرین تصمیم‌ها", "نگه‌داشتن هم یک تصمیم است؛ دلیلش را بخوان")
        self.tbl_decisions = table(["زمان", "نماد", "اقدام", "اطمینان", "منبع", "دلیل"]); self.tbl_decisions.setMinimumHeight(160)
        # "دلیل" is the column this table exists for, and it was being elided to "not enoug…".
        # Let it take the slack and show the whole sentence on hover.
        from PySide6.QtWidgets import QHeaderView as _HV
        _h = self.tbl_decisions.horizontalHeader()
        _h.setSectionResizeMode(5, _HV.Stretch)
        self.tbl_decisions.setWordWrap(False)
        self.tbl_decisions.setTextElideMode(Qt.ElideRight)
        self.empty_dec = Empty("هنوز تصمیمی ثبت نشده. «شروع» را بزن تا ربات بازار را بررسی کند.")
        c4.add(self.tbl_decisions); c4.add(self.empty_dec)
        low.addWidget(c3, 1); low.addWidget(c4, 1)
        v.addLayout(low)

        c5 = Card("گزارش زنده")
        self.txt_log = QPlainTextEdit(); self.txt_log.setReadOnly(True); self.txt_log.setMaximumBlockCount(500); self.txt_log.setFixedHeight(140)
        c5.add(self.txt_log)
        v.addWidget(c5)
        return self._scroll(inner)

    # ------------------------------------------------------------ engine control
    def toggle_engine(self):
        if self.engine and self.engine.running():
            self.engine.stop()
        else:
            self.start_engine()

    def start_engine(self):
        problems = self.settings.validate()
        if problems:
            QMessageBox.warning(self, "تنظیمات ناقص", "\n".join(problems)); self.goto("settings"); return
        if self.settings.mode == "live":
            ok = QMessageBox.question(self, "معامله واقعی",
                                      f"ربات با پول واقعی معامله می‌کند.\nسقف سرمایه: {self.settings.risk.capital_limit}\n"
                                      f"ریسک هر معامله: {self.settings.risk.risk_per_trade*100:.1f}%\nادامه؟")
            if ok != QMessageBox.Yes:
                return
        try:
            brain = make_brain(self.settings) if (self.settings.use_llm_for_decisions and self.settings.has_llm()) else None
            broker = None
            if self.settings.computer.enabled and self.settings.mode == "live":
                from ..execution.computer import ComputerBroker
                if not self.settings.has_claude():
                    raise RuntimeError("کنترل صفحه به کلید Claude نیاز دارد (حتی اگر تصمیم‌ها با OpenAI باشد)")
                broker = ComputerBroker(self.settings, claude_client(self.settings), confirm=self._confirm_blocking,
                                        on_step=lambda s: self.bridge.event.emit("[screen] " + s))
            self.engine = Engine(self.settings, self.db, broker=broker, brain=brain, on_event=self.bridge.event.emit)
            self.engine.start()
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "خطا در شروع", str(exc))

    def toggle_kill(self):
        from ..risk.manager import RiskManager
        rm = RiskManager(self.settings.risk, self.db, self.live_mode())
        rm.set_kill_switch(not rm.kill_switch_on()); self.refresh()

    def close_all(self):
        if not self.engine:
            QMessageBox.information(self, "", "موتور فعال نیست"); return
        if QMessageBox.question(self, "", "همه پوزیشن‌ها با قیمت بازار بسته شوند؟") == QMessageBox.Yes:
            self._run_bg(lambda: self.engine.close_all("manual"), lambda _: self.refresh())

    def reset_test(self):
        """Start a fresh test: wipe the paper journal and choose the starting balance."""
        if self.live_mode() == "live":
            QMessageBox.warning(self, "ریست تست",
                                "الان روی حالت «واقعی» هستی. این دکمه فقط حساب تمرینی (کاغذی) را "
                                "پاک می‌کند و به تاریخچه‌ی معامله‌های واقعی دست نمی‌زند.\n\n"
                                "برای تست، از تنظیمات حالت را روی paper بگذار.")
            return
        n_open = len(self.db.open_trades("paper"))
        n_closed = len(self.db.closed_trades("paper", 100000))
        bal, ok = QInputDialog.getDouble(
            self, "ریست تست",
            f"یک تست تازه شروع می‌شود.\n\n"
            f"پاک می‌شود: {n_closed} معامله‌ی بسته، {n_open} پوزیشن باز، نمودار سرمایه و تصمیم‌ها.\n"
            f"نگه داشته می‌شود: تنظیمات، مهارت‌ها، کتابخانه و گزارش سیستم.\n\n"
            f"موجودی شروع (دلار):",
            1000.0, 1.0, 10_000_000.0, 2)
        if not ok:
            return
        was_running = bool(self.engine and self.engine.running())
        self._reset_paper_now(balance=float(bal))
        QMessageBox.information(
            self, "انجام شد",
            f"حساب تمرینی پاک شد و موجودی روی {bal:g} دلار تنظیم شد.\n"
            + ("موتور دوباره راه افتاد؛ از همین‌جا تست را ادامه بده."
               if was_running else "حالا «شروع» را بزن تا تست شروع شود."))

    def _reset_paper_now(self, balance: float | None = None):
        """Wipe the paper journal and set the account back to a starting balance.

        The engine is STOPPED first if it is running. It is not enough to hold the trade lock:
        a loop pass that is already under way is holding its own list of open positions and its
        own view of the broker's cash, and would carry on managing rows that no longer exist.
        """
        from ..execution.paper import PaperBroker
        if balance is not None:
            self.settings.paper_start_balance = float(balance)
            self.settings.save()
            if hasattr(self, "s_paper_bal"):
                self.s_paper_bal.setValue(float(balance))
        bal = float(self.settings.paper_start_balance)

        was_running = bool(self.engine and self.engine.running())
        if was_running:
            self.engine.stop(wait=5.0)

        self.db.reset_mode("paper")
        PaperBroker(bal).reset(bal)
        if self.engine and isinstance(self.engine.broker, PaperBroker):
            self.engine.broker.reset(bal)
            self.engine.last_prices.clear()
            # A reset means "start over". Leaving the cooldowns and the one-entry-per-bar marks
            # behind means the engine sits out its first pass on exactly the symbols the user
            # just wiped, which reads as "I reset it and it still does nothing".
            self.engine._cooldown.clear()
            self.engine._entered_bar.clear()
            self.engine._last_bar.clear()
            self.engine._llm_bar.clear()
            self.engine._order_err.clear()
            self.engine._reconciled = True     # nothing is open; do not re-scan
        # An emergency stop left on from the previous run would silently refuse every trade of
        # the new one, and the only symptom is a bot that does nothing.
        from ..risk.manager import RiskManager
        RiskManager(self.settings.risk, self.db, "paper").set_kill_switch(False)
        self.db.record_equity("paper", bal)
        self._live = {}
        self._pos_data = []
        self._hide_pos_detail()
        if was_running:
            self.start_engine()      # a fresh Engine, so nothing carries over
        self.refresh()

    def _confirm_blocking(self, summary: str) -> bool:
        ev = threading.Event(); self._confirm_result = {"event": ev, "ok": False}
        self.bridge.confirm_request.emit(summary); ev.wait(timeout=120)
        return bool(self._confirm_result.get("ok"))

    def _on_confirm_request(self, summary: str):
        ok = QMessageBox.question(self, "تأیید سفارش روی صفحه", f"ربات می‌خواهد این سفارش را ثبت کند:\n\n{summary}\n\nتأیید می‌کنی؟")
        self._confirm_result["ok"] = ok == QMessageBox.Yes; self._confirm_result["event"].set()

    def _on_event(self, line: str):
        self.txt_log.appendPlainText(f"{time.strftime('%H:%M:%S')} {line}")

    # ------------------------------------------------------------ updates
    def _check_update(self, manual: bool):
        if self._pending_update and manual:
            self._offer_update(self._pending_update); return
        if not updater.configured():
            if manual:
                QMessageBox.information(self, "به‌روزرسانی", "این نسخه از سورس اجرا شده و مخزن به‌روزرسانی تنظیم نشده است.")
            return
        # A short timeout on the AUTOMATIC check: it fires seconds after the window opens, and
        # a slow update server must not leave a thread in flight for the rest of the session.
        self._run_bg(lambda: updater.check(timeout=20.0 if manual else 8.0),
                     lambda rel: self._update_checked(rel, manual),
                     (lambda m: QMessageBox.warning(self, "به‌روزرسانی", f"بررسی انجام نشد:\n{m.splitlines()[0]}")) if manual
                     else (lambda m: self._on_event("[update] check failed: " + m.splitlines()[0])))

    def _update_checked(self, rel, manual: bool):
        if rel is None:
            if manual:
                QMessageBox.information(self, "به‌روزرسانی", f"نسخه‌ی فعلی ({__version__}) آخرین نسخه است.")
            return
        self._pending_update = rel
        self.btn_update.setText(f"🔄 نصب {rel.version}"); self.btn_update.setObjectName("primary")
        self.btn_update.style().unpolish(self.btn_update); self.btn_update.style().polish(self.btn_update)
        self._on_event(f"[update] version {rel.version} is available")
        if not manual and updater.attempted_recently(rel.version):
            self._on_event(f"[update] {rel.version} was offered recently and the app is still {__version__}")
            QMessageBox.warning(self, "به‌روزرسانی",
                f"نسخه‌ی {rel.version} چند دقیقه پیش دانلود/نصب شد ولی برنامه هنوز {__version__} است.\n\n"
                f"معمولاً یعنی نصب‌کننده تا آخر اجرا نشده، یا در پوشه‌ی دیگری نصب شده.\n"
                f"پوشه‌ی برنامه‌ی فعلی:\n{updater.install_dir()}\n\n"
                f"برای تلاش دوباره 🔄 را بزن.")
            return
        if manual:
            self._offer_update(rel); return
        if self.settings.auto_update and updater.is_frozen() and rel.asset_url:
            if self.engine and self.engine.running():
                self._on_event("[update] engine is running - will install when it is stopped"); return
            # Automatic means the DOWNLOAD happens on its own - that is the slow part and the
            # part that fails from Iran. Closing the app still asks, because the alternative is
            # the window vanishing mid-session with no explanation.
            self._on_event(f"[update] downloading {rel.version} automatically")
            self._download_and_install(rel)

    def _offer_update(self, rel):
        if not updater.is_frozen():
            QMessageBox.information(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} منتشر شده.\n\nاین نسخه از سورس اجرا شده؛ با git pull به‌روز کن یا نصب‌کننده را بگیر:\n{rel.page_url}"); return
        if not rel.asset_url:
            QMessageBox.warning(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} فایل نصب ندارد:\n{rel.page_url}"); return
        if self.engine and self.engine.running():
            QMessageBox.warning(self, "به‌روزرسانی", "اول موتور معامله را متوقف کن، بعد به‌روزرسانی کن."); return
        mb = QMessageBox(self)
        mb.setWindowTitle("به‌روزرسانی")
        mb.setText(f"نسخه‌ی {rel.version} آماده است (نسخه‌ی فعلی {__version__}).\n\n"
                   f"حجم: {(rel.asset_size or 0) / 1048576:.0f} مگابایت\n\n"
                   "دانلود داخل برنامه از همان مسیری می‌رود که قیمت‌ها از آن می‌آید (پروکسی/VPN)،\n"
                   "و فایل قبل از اجرا با SHA-256 بررسی می‌شود.")
        b_app = mb.addButton("دانلود و نصب", QMessageBox.AcceptRole)
        b_web = mb.addButton("دانلود با مرورگر", QMessageBox.ActionRole)
        mb.addButton("بعداً", QMessageBox.RejectRole)
        mb.exec()
        if mb.clickedButton() is b_web:
            self._update_via_browser(rel)
            return
        if mb.clickedButton() is not b_app:
            return
        self._download_and_install(rel)

    def _update_via_browser(self, rel):
        """Fallback only. The browser has no idea about the app's proxy setting, so on a
        connection where the exchange itself is blocked this link usually will not open."""
        import webbrowser
        updater.mark_attempt(rel.version)
        try:
            opened = webbrowser.open(rel.asset_url)
        except Exception:
            opened = False
        self._on_event(f"[update] opened installer download for {rel.version}: {rel.asset_url}")
        QMessageBox.information(self, "به‌روزرسانی",
            f"نسخه‌ی {rel.version}: دانلود در مرورگر باز شد.\n\nبعد از دانلود:\n"
            f"۱) برنامه را ببند.\n۲) فایل TGTrader-Setup را اجرا کن و Install بزن.\n\n"
            + ("" if opened else f"اگر مرورگر باز نشد، این آدرس را دستی باز کن:\n{rel.asset_url}"))

    def _download_and_install(self, rel):
        dlg = QProgressDialog(f"در حال دانلود نسخه‌ی {rel.version}…", "لغو", 0, 100, self)
        dlg.setWindowTitle("به‌روزرسانی"); dlg.setMinimumDuration(0); dlg.setAutoClose(False)
        w = Worker(lambda: updater.download(rel, progress=lambda d, t: w.progress.emit(d, t)))

        def on_progress(d, t):
            if dlg.wasCanceled():
                return          # here it is genuine: the dialog is still open
            dlg.setMaximum(max(t, 1)); dlg.setValue(min(d, t) if t else 0)
            dlg.setLabelText(f"در حال دانلود نسخه‌ی {rel.version}…  {d / 1048576:.0f} از {t / 1048576:.0f} مگابایت")

        def on_done(path):
            # Read the cancel flag BEFORE closing. QProgressDialog::close() emits canceled(),
            # which is connected to cancel() by default, so wasCanceled() is True immediately
            # after close() whatever the user did - and this branch then returned on every
            # single successful download. The in-app update never installed anything.
            was_canceled = dlg.wasCanceled()
            dlg.close()
            if was_canceled:
                return
            verified = ("بررسی‌شده با SHA-256" if getattr(rel, "verified", False)
                        else "⚠ بدون checksum منتشر شده — صحتش بررسی نشد")
            self._on_event(f"[update] downloaded {rel.version} to {path} ({verified})")
            if QMessageBox.question(self, "به‌روزرسانی",
                    f"دانلود تمام شد ({verified}).\n\nحالا نصب‌کننده باز می‌شود و این برنامه بسته می‌شود.\n"
                    "بعد از پایان نصب، برنامه دوباره باز می‌شود.\n\nادامه؟") != QMessageBox.Yes:
                return
            updater.mark_attempt(rel.version)
            try:
                updater.install(path)
            except Exception as exc:
                QMessageBox.critical(self, "به‌روزرسانی", f"اجرای نصب‌کننده انجام نشد:\n{exc}\n\nفایل اینجاست:\n{path}")
                return
            # Quitting is not optional: Windows will not let the installer replace an exe that
            # is still running, and staying open is exactly what produced the download loop.
            self._quit_for_update = True
            QApplication.instance().quit()

        def on_fail(msg):
            was_canceled = dlg.wasCanceled()
            dlg.close()
            if was_canceled:
                return
            first = msg.splitlines()[0]
            self._on_event(f"[update] download failed: {first}")
            if QMessageBox.question(self, "به‌روزرسانی",
                    f"دانلود داخل برنامه انجام نشد:\n{first}\n\nبا مرورگر امتحان شود؟") == QMessageBox.Yes:
                self._update_via_browser(rel)

        w.progress.connect(on_progress)
        w.done.connect(on_done)
        w.failed.connect(on_fail)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start()
        dlg.exec()

    def _install_mt5(self):
        if updater.mt5_installed():
            QMessageBox.information(self, "MetaTrader 5", "MetaTrader 5 نصب است."); return
        if QMessageBox.question(self, "MetaTrader 5", "نصب‌کننده‌ی رسمی MetaTrader 5 دانلود و اجرا شود؟ (فقط برای فارکس لازم است)") != QMessageBox.Yes:
            return
        dlg = QProgressDialog("در حال دانلود MetaTrader 5…", None, 0, 100, self); dlg.setMinimumDuration(0)
        w = Worker(lambda: updater.install_mt5(progress=lambda d, t: w.progress.emit(d, t)))
        w.progress.connect(lambda d, t: (dlg.setMaximum(max(t, 1)), dlg.setValue(min(d, t) if t else 0)))
        w.done.connect(lambda _: (dlg.close(), QMessageBox.information(self, "MetaTrader 5", "نصب‌کننده باز شد. بعد از نصب و ورود به حساب بروکر، برنامه را دوباره باز کن.")))
        w.failed.connect(lambda m: (dlg.close(), QMessageBox.critical(self, "MetaTrader 5", m.splitlines()[0])))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start(); dlg.exec()

    # ============================================================ DESK (chart + open positions together)
    def _page_desk(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(12)
        split = QSplitter(Qt.Vertical)
        ctop = Card("چارت", "روی چارت اسکرول کنی فقط چارت زوم می‌شود، نه صفحه")
        h = QHBoxLayout(); h.setSpacing(10)
        self.desk_symbol = QComboBox(); self.desk_symbol.setEditable(True); self.desk_symbol.addItems(self.settings.symbols); self.desk_symbol.setMinimumWidth(160)
        self.desk_tf = QComboBox(); self.desk_tf.addItems(["1m", "5m", "15m", "1h", "4h", "1d"]); self.desk_tf.setCurrentText(self.settings.timeframe)
        h.addWidget(QLabel("نماد")); h.addWidget(self.desk_symbol); h.addWidget(QLabel("تایم‌فریم")); h.addWidget(self.desk_tf)
        h.addWidget(button("⟳", "", self.refresh_desk_chart)); h.addStretch()
        ctop.add_layout(h)
        self.desk_chart = CandleChart(); ctop.add(self.desk_chart, 1)
        self.desk_symbol.currentTextChanged.connect(lambda _: (self._sync_watch_symbols(), self.refresh_desk_chart()))
        self.desk_tf.currentTextChanged.connect(lambda _: self.refresh_desk_chart())
        cbot = Card("پوزیشن‌های باز", "روی هر ردیف بزن تا چارت بالا برود روی همان ارز")
        self.tbl_desk = table(["نماد", "جهت", "ورود", "قیمت", "ارزش", "حد ضرر", "هدف", "سود شناور"])
        self.tbl_desk.setTextElideMode(Qt.ElideNone)
        self.tbl_desk.cellClicked.connect(self._desk_row_to_chart)
        self.empty_desk = Empty("پوزیشن بازی نیست.")
        cbot.add(self.tbl_desk, 1); cbot.add(self.empty_desk)
        cbot.add_action(button("بستن همه", "danger", self.close_all))
        split.addWidget(ctop); split.addWidget(cbot); split.setSizes([520, 260])
        v.addWidget(split, 1)
        self._desk_chart_last = 0.0
        QTimer.singleShot(700, self, self.refresh_desk_chart)
        return w

    def refresh_desk_chart(self):
        sym = self.desk_symbol.currentText().strip().upper(); tf = self.desk_tf.currentText()
        if sym:
            self._desk_chart_last = time.time()
            self._load_chart(self.desk_chart, sym, tf, lambda m: self._on_event("[desk] " + m.splitlines()[0]))

    def _desk_row_to_chart(self, row: int, _col: int = 0):
        if 0 <= row < len(self._pos_data):
            self.desk_symbol.setCurrentText(self._pos_data[row]["symbol"])
            self.refresh_desk_chart()

    # ============================================================ CHART
    def _page_chart(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(12)
        c = Card()
        h = QHBoxLayout(); h.setSpacing(10)
        self.ch_symbol = QComboBox(); self.ch_symbol.setEditable(True); self.ch_symbol.addItems(self.settings.symbols); self.ch_symbol.setMinimumWidth(160)
        self.ch_tf = QComboBox(); self.ch_tf.addItems(["5m", "15m", "30m", "1h", "4h", "1d"]); self.ch_tf.setCurrentText(self.settings.timeframe)
        self.ch_auto = QCheckBox("تازه‌سازی خودکار"); self.ch_auto.setChecked(True)
        h.addWidget(QLabel("نماد")); h.addWidget(self.ch_symbol); h.addWidget(QLabel("تایم‌فریم")); h.addWidget(self.ch_tf)
        h.addWidget(button("⟳ تازه‌سازی", "", self.refresh_chart)); h.addWidget(self.ch_auto); h.addStretch()
        c.add_layout(h)
        self.chart = CandleChart(); c.add(self.chart, 1)
        self.lbl_hover = hint("چرخ ماوس: زوم · کشیدن: جابه‌جایی · ▲▼ ورود/خروج معاملات · خط‌چین: قیمت آخر، ورود، حد ضرر، هدف")
        self.chart.hovered.connect(self.lbl_hover.setText); c.add(self.lbl_hover)
        self.ch_symbol.currentTextChanged.connect(lambda _: (self._sync_watch_symbols(), self.refresh_chart()))
        self.ch_tf.currentTextChanged.connect(lambda _: self.refresh_chart())
        v.addWidget(c, 1)
        return w

    def market_data(self):
        from ..market.data import MarketData
        if self.engine:
            return self.engine.market
        if self._market is None:
            self._market = MarketData(self.settings, on_notice=lambda m: self.bridge.event.emit("[data] " + m))
        return self._market

    def _load_chart(self, widget: CandleChart, sym: str, tf: str, on_fail=None):
        from ..market.indicators import enrich
        md = self.market_data()

        def done(df):
            mode = self.live_mode()
            pos = next((dict(r) for r in self.db.open_trades(mode) if r["symbol"] == sym), None)
            trades = [dict(r) for r in self.db.closed_trades(mode, 300) if r["symbol"] == sym]
            widget.set_data(df, sym, tf, pos, trades)
        self._run_bg(lambda: enrich(md.candles(sym, tf, limit=500)), done, on_fail or (lambda m: self._on_event("[chart] " + m.splitlines()[0])))

    def refresh_chart(self):
        sym, tf = self.ch_symbol.currentText().strip().upper(), self.ch_tf.currentText()
        if sym:
            self._chart_last = time.time()
            self._load_chart(self.chart, sym, tf, lambda m: self.lbl_hover.setText("خطا در دریافت داده: " + m.splitlines()[0]))

    def refresh_dash_chart(self):
        if self.settings.symbols:
            self._dash_chart_last = time.time()
            self._load_chart(self.dash_chart, self.settings.symbols[0], self.settings.timeframe)

    # ============================================================ TRADES
    def _page_trades(self) -> QWidget:
        inner = QWidget(); v = QVBoxLayout(inner); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(14)
        k = QHBoxLayout(); k.setSpacing(12)
        self.kpi_t_n = Kpi("معاملات بسته", "0"); self.kpi_t_win = Kpi("نرخ برد", "—"); self.kpi_t_pnl = Kpi("سود خالص", "—")
        self.kpi_t_pf = Kpi("ضریب سود", "—", "بالای ۱ یعنی سودده"); self.kpi_t_r = Kpi("میانگین R", "—", "سود نسبت به ریسک اولیه")
        for x in (self.kpi_t_n, self.kpi_t_win, self.kpi_t_pnl, self.kpi_t_pf, self.kpi_t_r):
            k.addWidget(x)
        v.addLayout(k)
        c = Card("تاریخچه", "برای قضاوت درباره‌ی یک روش حداقل ۳۰ معامله لازم است")
        self.tbl_trades = table(["باز", "بسته", "نماد", "جهت", "مقدار", "ورود", "خروج", "سود/زیان", "R", "استراتژی", "دلیل"])
        self.tbl_trades.setMinimumHeight(420)
        self.empty_trades = Empty("هنوز معامله‌ای بسته نشده.")
        c.add(self.tbl_trades, 1); c.add(self.empty_trades)
        v.addWidget(c, 1)
        return self._scroll(inner)

    # ============================================================ SKILLS
    def _page_skills(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(12)
        top = QHBoxLayout(); top.setSpacing(10)
        self.sk_filter = QComboBox(); self.sk_filter.addItems(["همه", "فعال", "پیش‌نویس", "غیرفعال"]); self.sk_filter.currentIndexChanged.connect(lambda _: self.refresh_skills())
        self.sk_search = QLineEdit(); self.sk_search.setPlaceholderText("جستجو در نام و متن قانون…"); self.sk_search.textChanged.connect(lambda _: self.refresh_skills())
        top.addWidget(self.sk_filter); top.addWidget(self.sk_search, 1)
        self.pill_sk_active = pill("", "ok"); self.pill_sk_draft = pill("", "warn"); top.addWidget(self.pill_sk_active); top.addWidget(self.pill_sk_draft)
        v.addLayout(top)
        split = QSplitter(Qt.Horizontal)
        c1 = Card("قوانین", "پیش‌نویس‌ها تا تأیید نشوند در تصمیم استفاده نمی‌شوند")
        self.tbl_skills = table(["#", "وضعیت", "دسته", "نام", "منبع"]); self.tbl_skills.itemSelectionChanged.connect(self._skill_selected)
        from PySide6.QtWidgets import QHeaderView
        for col in (1, 2):
            self.tbl_skills.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        c1.add(self.tbl_skills, 1)
        c2 = Card("جزئیات")
        self.txt_skill = QTextEdit(); self.txt_skill.setReadOnly(True); c2.add(self.txt_skill, 1)
        acts = QHBoxLayout()
        acts.addWidget(button("✓ تأیید", "primary", lambda: self._skill_status("approved")))
        acts.addWidget(button("غیرفعال", "", lambda: self._skill_status("disabled")))
        acts.addWidget(button("پیش‌نویس", "ghost", lambda: self._skill_status("draft")))
        acts.addWidget(button("حذف", "danger", self._skill_delete))
        c2.add_layout(acts)
        c2.add(hint("چند ردیف را با Ctrl انتخاب کن تا یک‌جا تغییر وضعیت بدهی."))
        split.addWidget(c1); split.addWidget(c2); split.setSizes([700, 420])
        v.addWidget(split, 1)
        return w

    def _selected_skill_ids(self) -> list[int]:
        return [int(self.tbl_skills.item(i.row(), 0).text()) for i in self.tbl_skills.selectionModel().selectedRows()]

    def _skill_status(self, st: str):
        for sid in self._selected_skill_ids():
            self.db.set_skill_status(sid, st)
        self.refresh_skills()

    def _skill_delete(self):
        ids = self._selected_skill_ids()
        if ids and QMessageBox.question(self, "", f"{len(ids)} مهارت حذف شود؟") == QMessageBox.Yes:
            for sid in ids:
                self.db.delete_skill(sid)
            self.refresh_skills()

    def _skill_selected(self):
        ids = self._selected_skill_ids()
        if ids:
            r = self.db.one("SELECT * FROM skills WHERE id=?", (ids[0],))
            if r:
                st = {"approved": "فعال", "draft": "پیش‌نویس", "disabled": "غیرفعال"}.get(r["status"], r["status"])
                self.txt_skill.setHtml(f"<h3 style='color:{theme.ACCENT};margin:0'>{r['name']}</h3>"
                                       f"<p style='color:{theme.MUTED}'>{st} · دسته: {r['category']} · منبع: {r['source']}</p>"
                                       f"<p style='line-height:1.8'>{r['rule']}</p>")

    def refresh_skills(self):
        rows = self.db.skills()
        f = self.sk_filter.currentIndex(); st_map = {1: "approved", 2: "draft", 3: "disabled"}
        q = self.sk_search.text().strip().lower()
        shown = [r for r in rows if (f == 0 or r["status"] == st_map[f]) and (not q or q in r["name"].lower() or q in r["rule"].lower())]
        fa = {"approved": "فعال", "draft": "پیش‌نویس", "disabled": "غیرفعال"}
        fill(self.tbl_skills, [[r["id"], fa.get(r["status"], r["status"]), r["category"], r["name"], r["source"]] for r in shown])
        set_pill(self.pill_sk_active, f"{sum(1 for r in rows if r['status']=='approved')} فعال", "ok")
        set_pill(self.pill_sk_draft, f"{sum(1 for r in rows if r['status']=='draft')} پیش‌نویس", "warn")

    # ============================================================ LEARN
    def _page_learn(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(12)
        split = QSplitter(Qt.Horizontal)

        c1 = Card("کتابخانه", "کتاب یا مقاله را اضافه کن، بعد «یادگیری» را بزن تا قوانینش مهارت شوند")
        add = QHBoxLayout(); add.setSpacing(8)
        add.addWidget(button("📄 PDF / متن", "", self._add_pdf))
        self.in_url = QLineEdit(); self.in_url.setPlaceholderText("آدرس مقاله https://…"); self.in_url.returnPressed.connect(self._add_url)
        add.addWidget(self.in_url, 1); add.addWidget(button("🌐 افزودن لینک", "", self._add_url)); add.addWidget(button("✍ متن", "ghost", self._add_text))
        c1.add_layout(add)
        self.tbl_docs = table(["#", "نوع", "حروف", "عنوان", "افزوده شده"]); c1.add(self.tbl_docs, 1)
        self.empty_docs = Empty("کتابخانه خالی است. یک PDF یا لینک اضافه کن."); c1.add(self.empty_docs)
        acts = QHBoxLayout()
        acts.addWidget(button("🧠 یادگیری از سند انتخاب‌شده", "primary", self._learn_doc)); acts.addWidget(button("حذف سند", "ghost", self._delete_doc)); acts.addStretch()
        c1.add_layout(acts)

        c2 = Card("آموزش مستقیم", "قانونت را به زبان خودت بگو؛ بلافاصله مهارت فعال می‌شود")
        self.txt_chat = QPlainTextEdit(); self.txt_chat.setReadOnly(True); c2.add(self.txt_chat, 1)
        hc = QHBoxLayout()
        self.in_chat = QLineEdit(); self.in_chat.setPlaceholderText("مثلاً: وقتی RSI زیر ۳۰ بود ولی روند نزولی قوی بود، خرید نکن"); self.in_chat.returnPressed.connect(self._send_chat)
        hc.addWidget(self.in_chat, 1); hc.addWidget(button("ارسال", "primary", self._send_chat))
        c2.add_layout(hc)
        split.addWidget(c1); split.addWidget(c2); split.setSizes([640, 520])
        v.addWidget(split, 1)
        return w

    def _selected_doc_id(self) -> int | None:
        rows = self.tbl_docs.selectionModel().selectedRows()
        return int(self.tbl_docs.item(rows[0].row(), 0).text()) if rows else None

    def _add_pdf(self):
        from ..knowledge.ingest import ingest_pdf, ingest_text
        path, _ = QFileDialog.getOpenFileName(self, "انتخاب فایل", "", "PDF / Text (*.pdf *.txt *.md)")
        if not path:
            return
        if path.lower().endswith(".pdf"):
            self._run_bg(lambda: ingest_pdf(self.db, path), self._ingested)
        else:
            text = open(path, encoding="utf-8", errors="ignore").read()
            self._ingested(ingest_text(self.db, path.replace("\\", "/").split("/")[-1], text, "text", path))

    def _add_url(self):
        from ..knowledge.ingest import ingest_url
        url = self.in_url.text().strip()
        if url:
            self._run_bg(lambda: ingest_url(self.db, url), self._ingested)

    def _add_text(self):
        from ..knowledge.ingest import ingest_text
        dlg = QTextEdit(); dlg.setMinimumSize(600, 400)
        box = QMessageBox(self); box.setWindowTitle("متن دستی"); box.setText("متن را وارد کن:")
        box.layout().addWidget(dlg, 1, 0, 1, box.layout().columnCount()); box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if box.exec() == QMessageBox.Ok and dlg.toPlainText().strip():
            self._ingested(ingest_text(self.db, dlg.toPlainText()[:60].replace("\n", " "), dlg.toPlainText(), "text", "manual"))

    def _ingested(self, r):
        doc_id, text = r
        self.refresh_docs()
        self.txt_chat.appendPlainText(f"📚 سند #{doc_id} با {len(text):,} حرف ذخیره شد. آن را انتخاب کن و «یادگیری» را بزن.")

    def _learn_doc(self):
        doc_id = self._selected_doc_id()
        if doc_id is None:
            QMessageBox.information(self, "", "اول یک سند از کتابخانه انتخاب کن"); return
        if not self.settings.has_llm():
            QMessageBox.warning(self, "", "کلید هوش مصنوعی در تنظیمات وارد نشده"); self.goto("settings"); return
        doc = self.db.one("SELECT * FROM knowledge_docs WHERE id=?", (doc_id,))
        text = "\n\n".join(self.db.doc_chunks(doc_id))
        self.txt_chat.appendPlainText(f"… در حال خواندن «{doc['title']}» ({len(text):,} حرف). چند دقیقه طول می‌کشد.")

        def job():
            skills = make_brain(self.settings).extract_skills(doc["title"], text)
            return len(skills), add_extracted(self.db, skills, source=f"{doc['kind']}:{doc['title']}")
        self._run_bg(job, self._learned)

    def _learned(self, r):
        found, new = r
        self.refresh_skills()
        self.txt_chat.appendPlainText(f"✓ {found} قانون پیدا شد، {new} مهارت جدید به عنوان پیش‌نویس ذخیره شد. در «مهارت‌ها» بررسی و تأیید کن.")
        self.goto("skills"); self.sk_filter.setCurrentIndex(2)

    def _delete_doc(self):
        doc_id = self._selected_doc_id()
        if doc_id is not None and QMessageBox.question(self, "", "سند حذف شود؟ (مهارت‌های استخراج‌شده می‌مانند)") == QMessageBox.Yes:
            self.db.delete_doc(doc_id); self.refresh_docs()

    def _send_chat(self):
        msg = self.in_chat.text().strip()
        if not msg:
            return
        if not self.settings.has_llm():
            QMessageBox.warning(self, "", "کلید هوش مصنوعی در تنظیمات وارد نشده"); self.goto("settings"); return
        self.in_chat.clear(); self.txt_chat.appendPlainText(f"شما: {msg}")
        self.teach_history.append({"role": "user", "content": msg}); hist = list(self.teach_history)

        def done(r):
            reply, skills = r
            self.teach_history.append({"role": "assistant", "content": reply}); self.txt_chat.appendPlainText(f"ربات: {reply}")
            if skills:
                n = add_extracted(self.db, skills, source="user", status="approved")
                self.txt_chat.appendPlainText(f"✓ {n} مهارت ذخیره و فعال شد: " + "، ".join(s["name"] for s in skills)); self.refresh_skills()
        self._run_bg(lambda: make_brain(self.settings).teach_chat(hist, active_skills(self.db)), done)

    def refresh_docs(self):
        docs = self.db.docs()
        fill(self.tbl_docs, [[d["id"], d["kind"], f"{d['chars']:,}", d["title"], self._ts(d["added_at"])] for d in docs])
        self.empty_docs.setVisible(not docs); self.tbl_docs.setVisible(bool(docs))

    # ============================================================ BACKTEST
    def _page_backtest(self) -> QWidget:
        inner = QWidget(); v = QVBoxLayout(inner); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(14)
        c = Card("اجرا", "قوانین پایه با همان مدیریت ریسک، روی داده‌ی گذشته، با کارمزد و لغزش. هوش مصنوعی و مهارت‌های آموخته شامل نمی‌شود.")
        h = QHBoxLayout(); h.setSpacing(10)
        self.bt_symbol = QComboBox(); self.bt_symbol.setEditable(True); self.bt_symbol.addItems(self.settings.symbols); self.bt_symbol.setMinimumWidth(160)
        self.bt_tf = QComboBox(); self.bt_tf.addItems(["15m", "30m", "1h", "4h", "1d"]); self.bt_tf.setCurrentText(self.settings.timeframe)
        self.bt_bars = QSpinBox(); self.bt_bars.setRange(200, 5000); self.bt_bars.setValue(1000)
        self.bt_short = QCheckBox("فروش استقراضی هم"); self.bt_short.setChecked(True)
        for lab, wid in (("نماد", self.bt_symbol), ("تایم‌فریم", self.bt_tf), ("تعداد کندل", self.bt_bars)):
            h.addWidget(QLabel(lab)); h.addWidget(wid)
        h.addWidget(self.bt_short); h.addStretch(); self.btn_bt = button("▶ اجرای بک‌تست", "primary", self._run_bt); h.addWidget(self.btn_bt)
        c.add_layout(h); v.addWidget(c)
        k = QHBoxLayout(); k.setSpacing(12)
        self.kpi_bt = {key: Kpi(lab, "—", sub) for key, lab, sub in (
            ("return_pct", "بازده", "درصد"), ("max_drawdown_pct", "بیشترین افت", "از قله"), ("profit_factor", "ضریب سود", "بالای ۱ سودده"),
            ("avg_r", "میانگین R", "مثبت یعنی می‌ارزد"), ("trades", "معاملات", ""), ("win_rate", "نرخ برد", "به تنهایی معنی ندارد"))}
        for x in self.kpi_bt.values():
            k.addWidget(x)
        v.addLayout(k)
        self.lbl_bt_verdict = hint(""); v.addWidget(self.lbl_bt_verdict)
        c2 = Card("معاملات بک‌تست")
        self.tbl_bt = table(["استراتژی", "جهت", "ورود", "خروج", "سود/زیان", "R", "دلیل"]); self.tbl_bt.setMinimumHeight(360)
        self.empty_bt = Empty("نتیجه بعد از اجرا این‌جا می‌آید."); c2.add(self.tbl_bt, 1); c2.add(self.empty_bt)
        v.addWidget(c2, 1)
        return self._scroll(inner)

    def _run_bt(self):
        from ..backtest.engine import run_backtest
        sym, tf, bars, short = self.bt_symbol.currentText().strip().upper(), self.bt_tf.currentText(), self.bt_bars.value(), self.bt_short.isChecked()
        self.btn_bt.setEnabled(False); self.lbl_bt_verdict.setText("در حال دریافت داده و اجرا…")

        def job():
            df = self.market_data().candles(sym, tf, limit=bars)
            from ..backtest.engine import engine_params
            return run_backtest(sym, df, self.settings.risk,
                                 start_equity=self.settings.risk.capital_limit, allow_short=short,
                                 **engine_params(self.settings))

        def done(res):
            self.btn_bt.setEnabled(True)
            st = res.stats()
            self.kpi_bt["return_pct"].set(f"{st['return_pct']:+.2f}%", tone="green" if st["return_pct"] > 0 else "red")
            self.kpi_bt["max_drawdown_pct"].set(f"{st['max_drawdown_pct']:.2f}%")
            self.kpi_bt["profit_factor"].set(profit_factor(st),
                                             tone="green" if st["profit_factor"] > 1 else "red")
            self.kpi_bt["avg_r"].set(f"{st['avg_r']:+.2f}", tone="green" if st["avg_r"] > 0 else "red")
            self.kpi_bt["trades"].set(str(st["trades"])); self.kpi_bt["win_rate"].set(f"{st['win_rate']*100:.0f}%")
            if st["trades"] < 10:
                verdict = "معامله‌ی کافی برای قضاوت نیست؛ کندل بیشتری بده یا تایم‌فریم کوتاه‌تر."
            elif st["profit_factor"] > 1.2 and st["avg_r"] > 0:
                verdict = "قوانین پایه روی این بازار و تایم‌فریم مثبت‌اند. قدم بعدی: چند هفته کاغذی."
            elif st["profit_factor"] > 1:
                verdict = "کمی مثبت، ولی ضعیف. تایم‌فریم بلندتر را هم امتحان کن."
            else:
                verdict = "قوانین پایه این‌جا ضرر می‌دهند. تایم‌فریم یا نماد را عوض کن؛ با این تنظیمات معامله‌ی واقعی نکن."
            self.lbl_bt_verdict.setText(verdict)
            fill(self.tbl_bt, [[t.strategy, t.side, f"{t.entry:.6g}", f"{t.exit:.6g}", f"{t.pnl:+.4f}", f"{t.r:+.2f}", t.reason] for t in res.trades], tones={4: "pnl", 5: "pnl"})
            self.empty_bt.setVisible(not res.trades); self.tbl_bt.setVisible(bool(res.trades))

        def fail(m):
            self.btn_bt.setEnabled(True); self.lbl_bt_verdict.setText("خطا: " + m.splitlines()[0])
        self._run_bg(job, done, fail)

    # ============================================================ SELF-TEST
    def _page_selftest(self) -> QWidget:
        inner = QWidget(); v = QVBoxLayout(inner); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(14)
        c = Card("تست کامل سیستم", "تنظیمات، پایگاه داده، داده‌ی صرافی، KCEX، قوانین، ریسک، بک‌تست، Claude، OpenAI، یک تصمیم واقعی، یادگیری، چت، کنترل صفحه، به‌روزرسانی")
        h = QHBoxLayout()
        self.st_ai = QCheckBox("تست‌های هوش مصنوعی هم (چند سنت هزینه دارد)"); self.st_ai.setChecked(True)
        self.btn_st = button("🧪 اجرای تست", "primary", self._run_selftest)
        self.btn_st_copy = button("📋 کپی گزارش", "", self._copy_selftest); self.btn_st_copy.setEnabled(False)
        self.btn_st_save = button("💾 ذخیره گزارش", "ghost", self._save_selftest); self.btn_st_save.setEnabled(False)
        h.addWidget(self.btn_st); h.addWidget(self.st_ai); h.addStretch(); h.addWidget(self.btn_st_copy); h.addWidget(self.btn_st_save)
        c.add_layout(h)
        self.lbl_st_progress = hint("هنوز اجرا نشده. بعد از اجرا، «کپی گزارش» را بزن و متن را برای سازنده بفرست."); c.add(self.lbl_st_progress)
        v.addWidget(c)
        c2 = Card("نتیجه")
        self.tbl_st = table(["وضعیت", "بخش", "زمان (ms)", "جزئیات"])
        from PySide6.QtWidgets import QHeaderView
        for col in (0, 1, 2):
            self.tbl_st.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_st.setMinimumHeight(360); c2.add(self.tbl_st, 1)
        self.txt_st = QPlainTextEdit(); self.txt_st.setReadOnly(True); self.txt_st.setFixedHeight(160); c2.add(self.txt_st)
        v.addWidget(c2, 1)
        self._st_report = None
        return self._scroll(inner)

    def _run_selftest(self):
        from ..diagnostics import run_all
        if self.engine and self.engine.running():
            QMessageBox.warning(self, "تست", "اول موتور معامله را متوقف کن."); return
        self.btn_st.setEnabled(False); self.tbl_st.setRowCount(0); self.txt_st.clear()
        include_ai = self.st_ai.isChecked()
        w = Worker(lambda: run_all(self.settings, self.db, progress=lambda m: w.progress.emit(0, 0) or self.bridge.event.emit("[selftest] " + m), include_ai=include_ai))

        def done(rep):
            self._st_report = rep; self.btn_st.setEnabled(True); self.btn_st_copy.setEnabled(True); self.btn_st_save.setEnabled(True)
            fill(self.tbl_st, [["✓ موفق" if c.ok else "✗ خطا", c.name, str(c.ms), c.detail] for c in rep.checks])
            for i, c in enumerate(rep.checks):
                self.tbl_st.item(i, 0).setForeground(__import__("PySide6.QtGui", fromlist=["QColor"]).QColor(theme.SUCCESS if c.ok else theme.DANGER))
            ok = sum(1 for c in rep.checks if c.ok)
            self.lbl_st_progress.setText(f"{ok} از {len(rep.checks)} بخش موفق. «کپی گزارش» را بزن و متن را بفرست.")
            self.txt_st.setPlainText(rep.summary())

        def fail(m):
            self.btn_st.setEnabled(True); self.lbl_st_progress.setText("خطا: " + m.splitlines()[0])
        w.done.connect(done); w.failed.connect(fail)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start()
        self.lbl_st_progress.setText("در حال اجرا… (تست‌های هوش مصنوعی یک تا دو دقیقه طول می‌کشد)")

    def _copy_selftest(self):
        if self._st_report:
            QApplication.clipboard().setText(self._st_report.summary())
            self.lbl_st_progress.setText("گزارش کپی شد. در چت Paste کن (Ctrl+V).")

    def _save_selftest(self):
        if not self._st_report:
            return
        import json
        path, _ = QFileDialog.getSaveFileName(self, "ذخیره گزارش", "tgtrader-selftest.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._st_report.to_dict(), f, ensure_ascii=False, indent=2)
            self.lbl_st_progress.setText(f"ذخیره شد: {path}")

    # ============================================================ HELP
    def _page_help(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(22, 18, 22, 22)
        tb = QTextBrowser(); tb.setOpenExternalLinks(True); tb.setHtml(HELP_HTML); v.addWidget(tb)
        return w

    # ============================================================ SETTINGS
    def _page_settings(self) -> QWidget:
        s = self.settings
        inner = QWidget(); v = QVBoxLayout(inner); v.setContentsMargins(22, 18, 22, 22); v.setSpacing(14)

        cp = Card("حالت‌های آماده", "یک کلیک، همه‌ی تنظیمات با هم", accent=True)
        prow = QHBoxLayout(); prow.setSpacing(10)
        prow.addWidget(button("⚡ هوشمند چندارزی", "primary", lambda: self._preset("smart")))
        prow.addWidget(button("🐢 جدی و صبور (پول واقعی)", "", lambda: self._preset("serious")))
        prow.addWidget(button("🎯 اسکالپ (فقط تماشا)", "ghost", lambda: self._preset("scalp")))
        prow.addStretch()
        cp.add_layout(prow)
        cp.add(hint(
            "اندازه‌گیری واقعی روی ۸ ارز و ۸۰۰ کندل روزانه، با کارمزد ۰.۱٪ در هر طرف و لغزش قیمت:\n"
            "• هر ارز با حساب مستقل خودش: میانگین +۴.۲٪ ، ۷ ارز از ۸ سودده.\n"
            "• ولی با یک حساب ۱۰۰۰ دلاری مشترک بین ارزها (کاری که برنامه واقعاً می‌کند): "
            "۳ ارز حدود +۰.۵٪ و ۸ ارز ‎-۶.۴٪‎ — یعنی تقریباً سربه‌سر، نه سودِ چشمگیر.\n"
            "• تایم‌فریم ۴ ساعته و ۱ ساعته: همیشه منفی. اسکالپ روی ۵ و ۱۵ دقیقه: ۰ از ۶ سودده، "
            "بین ‎-۹٪‎ تا ‎-۱۸٪‎. سرعت زیاد یعنی کارمزد زیاد، نه سود زیاد.\n"
            "این عددها گذشته است و تضمین آینده نیست. قوانین پایه‌ی برنامه لبه‌ی بزرگی ندارند؛ "
            "کاری که واقعاً کمک می‌کند اضافه‌کردن مهارت و تحلیل هوش مصنوعی روی همین پایه است."))
        v.addWidget(cp)

        grid = QGridLayout(); grid.setSpacing(14)

        c1 = Card("هوش مصنوعی", "چه کسی تصمیم می‌گیرد و از کتاب‌ها یاد می‌گیرد")
        self.s_provider = QComboBox(); self.s_provider.addItems(["claude", "openai"]); self.s_provider.setCurrentText(s.ai_provider)
        self.s_key = QLineEdit(s.anthropic_api_key); self.s_key.setEchoMode(QLineEdit.Password); self.s_key.setPlaceholderText("sk-ant-…")
        self.s_model = QComboBox(); self.s_model.addItems(["claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"]); self.s_model.setCurrentText(s.model)
        self.s_oai_key = QLineEdit(s.openai_api_key); self.s_oai_key.setEchoMode(QLineEdit.Password); self.s_oai_key.setPlaceholderText("sk-…")
        self.s_oai_model = QLineEdit(s.openai_model)
        self.s_effort = QComboBox(); self.s_effort.addItems(["low", "medium", "high", "xhigh", "max"]); self.s_effort.setCurrentText(s.effort)
        self.s_llm = QCheckBox("تصمیم نهایی با هوش مصنوعی و مهارت‌ها (خاموش = فقط قوانین پایه)"); self.s_llm.setChecked(s.use_llm_for_decisions)
        self.s_agg = QComboBox()
        self.s_agg.addItems(["normal", "high", "scalp"]); self.s_agg.setCurrentText(getattr(s, "aggressiveness", "normal"))
        self.s_autoupd = QCheckBox("به‌روزرسانی خودکار موقع باز شدن برنامه"); self.s_autoupd.setChecked(s.auto_update)
        self.s_align = QCheckBox("هم‌جهت با بیت‌کوین (لانگ آلت وقتی BTC ریزشی است، باز نشود) — "
                                 "در بک‌تست سود را کمی کم کرد؛ فقط برای محافظه‌کاری در ریزش")
        self.s_align.setChecked(getattr(s, "align_with_leader", True))
        c1.add(FormRow("تصمیم‌گیرنده", self.s_provider, "کلید همان را وارد کن. کنترل صفحه همیشه با Claude است."))
        c1.add(FormRow("Claude API key", self.s_key, "از console.anthropic.com"))
        c1.add(FormRow("مدل Claude", self.s_model, "opus-5 پیشنهادی؛ sonnet-5 ارزان‌تر"))
        c1.add(FormRow("OpenAI API key", self.s_oai_key, "از platform.openai.com"))
        c1.add(FormRow("مدل OpenAI", self.s_oai_model, "پیش‌فرض gpt-5"))
        c1.add(FormRow("دقت (effort)", self.s_effort, "high برای تصمیم معامله کافی است"))
        c1.add(self.s_llm)
        c1.add(FormRow("میزان تهاجم", self.s_agg,
                       "normal = صبور، منتظر ستاپ واقعی · high = آستانه پایین‌تر، معامله‌ی بیشتر · "
                       "scalp = فقط قوانین، روی هر مومنتوم وارد می‌شود (برای دیدن فعالیت روی تایم‌فریم کوتاه، نه برای سود)"))
        c1.add(self.s_align)
        c1.add(self.s_autoupd)
        c1.add_action(button("تست اتصال", "", self._test_llm))
        grid.addWidget(c1, 0, 0)

        c2 = Card("بازار و صرافی", "از کجا قیمت می‌آید و سفارش کجا زده می‌شود")
        self.s_mode = QComboBox(); self.s_mode.addItems(["paper", "live"]); self.s_mode.setCurrentText(s.mode)
        self.s_market = QComboBox(); self.s_market.addItems(["crypto", "forex"]); self.s_market.setCurrentText(s.market)
        self.s_exchange = QComboBox(); self.s_exchange.setEditable(True)
        self.s_exchange.addItems(["bybit", "binance", "kucoin", "okx", "mexc", "gateio", "bitget", "htx", "kcex"]); self.s_exchange.setCurrentText(s.exchange.exchange_id)
        self.lbl_exchange_note = hint(""); self.lbl_exchange_note.setStyleSheet(f"color:{theme.ACCENT}")
        self.s_data_source = QComboBox(); self.s_data_source.addItems(["auto", "mexc", "kcex", "gateio", "htx", "bitget", "bybit", "binance", "kucoin", "okx"]); self.s_data_source.setCurrentText(getattr(s, "data_source", "auto"))
        self.s_ex_key = QLineEdit(s.exchange.api_key); self.s_ex_secret = QLineEdit(s.exchange.secret); self.s_ex_secret.setEchoMode(QLineEdit.Password)
        self.s_ex_pass = QLineEdit(s.exchange.password); self.s_ex_pass.setEchoMode(QLineEdit.Password)
        self.s_symbols = QLineEdit(", ".join(s.symbols))
        # "1m" was missing from this list while the scalp preset tried to select it. setCurrentText
        # on a non-editable combo does NOTHING when the value is absent - no error, no warning - so
        # the preset silently left whatever timeframe was there before.
        self.s_tf = QComboBox(); self.s_tf.addItems(["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
        self.s_tf.setCurrentText(s.timeframe)
        self.s_paper_bal = QDoubleSpinBox(); self.s_paper_bal.setRange(1, 1e9); self.s_paper_bal.setValue(s.paper_start_balance)
        c2.add(FormRow("حالت", self.s_mode, "paper = کاغذی، بدون پول واقعی. اول همیشه کاغذی."))
        c2.add(FormRow("بازار", self.s_market, "فارکس به MetaTrader 5 نیاز دارد (پایین)"))
        c2.add(FormRow("صرافی", self.s_exchange, "قیمت‌ها از این‌جا می‌آید، حتی در حالت کاغذی"))
        c2.add(self.lbl_exchange_note)
        c2.add(FormRow("منبع داده", self.s_data_source, "auto = همان صرافی، و اگر از ایران بسته بود خودکار MEXC → KCEX → Gate → HTX. برنامه در گزارش می‌گوید کدام را گرفته."))
        c2.add(FormRow("API key صرافی", self.s_ex_key, "فقط برای حالت واقعی. مجوز فقط Trade، بدون Withdrawal."))
        c2.add(FormRow("Secret", self.s_ex_secret)); c2.add(FormRow("Passphrase", self.s_ex_pass, "فقط okx و kucoin"))
        c2.add(FormRow("نمادها", self.s_symbols, "با کاما: BTC/USDT, ETH/USDT"))
        c2.add(FormRow("تایم‌فریم", self.s_tf, "1d پیش‌فرض؛ کوتاه‌تر = معامله و نویز بیشتر"))
        c2.add(FormRow("موجودی کاغذی", self.s_paper_bal, "پول مجازی حساب تمرینی. عوضش کنی و ذخیره بزنی، حساب همان لحظه با همین مبلغ ریست می‌شود."))
        grid.addWidget(c2, 0, 1)

        c3 = Card("ریسک", "سقف‌های سخت؛ هوش مصنوعی نمی‌تواند از این‌ها رد شود")
        self.s_cap = QDoubleSpinBox(); self.s_cap.setRange(1, 1e9); self.s_cap.setValue(s.risk.capital_limit)
        self.s_rpt = QDoubleSpinBox(); self.s_rpt.setRange(0.1, 10); self.s_rpt.setSuffix(" %"); self.s_rpt.setValue(s.risk.risk_per_trade * 100)
        self.s_pospct = QDoubleSpinBox(); self.s_pospct.setRange(0, 100); self.s_pospct.setSuffix(" %"); self.s_pospct.setValue(getattr(s, "position_pct", 0.0))
        self.s_openrisk = QDoubleSpinBox(); self.s_openrisk.setRange(0, 50); self.s_openrisk.setSuffix(" %")
        self.s_openrisk.setValue(getattr(s.risk, "max_open_risk", 0.06) * 100)
        self.s_dl = QDoubleSpinBox(); self.s_dl.setRange(0.5, 50); self.s_dl.setSuffix(" %"); self.s_dl.setValue(s.risk.max_daily_loss * 100)
        self.s_maxpos = QSpinBox(); self.s_maxpos.setRange(1, 20); self.s_maxpos.setValue(s.risk.max_open_positions)
        # This one had NO field at all, while an advisory told the owner to change it - so the
        # answer to "where do I change it?" was "nowhere".
        self.s_posfrac = QDoubleSpinBox(); self.s_posfrac.setRange(1, 100); self.s_posfrac.setSuffix(" %")
        self.s_posfrac.setValue(getattr(s.risk, "max_position_frac", 0.25) * 100)
        self.s_atr = QDoubleSpinBox(); self.s_atr.setRange(0.5, 6); self.s_atr.setValue(s.risk.atr_stop_mult)
        self.s_rr = QDoubleSpinBox(); self.s_rr.setRange(0.5, 10); self.s_rr.setValue(s.risk.reward_risk)
        self.s_trail = QDoubleSpinBox(); self.s_trail.setRange(0, 5); self.s_trail.setValue(s.risk.trail_after_r)
        c3.add(FormRow(LABELS["capital_limit"], self.s_cap, "ربات هرگز بیش از این مبلغ را درگیر نمی‌کند"))
        c3.add(FormRow(LABELS["risk_per_trade"], self.s_rpt, "حداکثر ضرر یک معامله، درصدی از سقف. ۱٪ = با سقف ۱۰۰ دلار، ۱ دلار"))
        c3.add(FormRow(LABELS["position_pct"], self.s_pospct,
                       "چند درصد پول در هر معامله گذاشته شود. ۰ = خودکار (اندازه از روی فاصله‌ی حد ضرر).\n"
                       "⚠ این حالت فاصله‌ی حد ضرر را نادیده می‌گیرد، پس معامله‌ای با حد ضرر دور "
                       "چند برابر بقیه ریسک می‌کند — سود و ضرر هر دو بزرگ‌تر می‌شوند.\n"
                       "در شبیه‌سازی خودِ موتور روی یک حساب مشترک با ۸ ارز، عدد ۲۰ به‌جای ۰ نتیجه را "
                       "از ‎-۶.۴٪‎ به ‎-۲۹.۹٪‎ برد و بیشترین افت را از ۱۸.۶٪ به ۳۶.۹٪ رساند. "
                       "با ۳ ارز هم +۰.۵٪ را به ‎-۰.۱٪‎ برد. ۰ توصیه می‌شود."))
        c3.add(FormRow(LABELS["max_open_risk"], self.s_openrisk,
                       "اگر همه‌ی پوزیشن‌های باز با هم حد ضرر بخورند، حداکثر چند درصد سرمایه از دست می‌رود. "
                       "۰ = بدون سقف. با ۶٪ و ریسک ۱٪ در هر معامله، حدود ۶ پوزیشن همزمان جا می‌شود."))
        c3.add(FormRow(LABELS["max_daily_loss"], self.s_dl, "با رسیدن به آن، تا فردا معامله‌ی جدیدی باز نمی‌شود"))
        c3.add(FormRow(LABELS["max_open_positions"], self.s_maxpos,
                       "چند معامله می‌تواند هم‌زمان باز باشد. برای اینکه واقعاً به این عدد برسد، "
                       "«بزرگ‌ترین پوزیشن» باید حدود ۱۰۰ تقسیم بر همین عدد باشد."))
        c3.add(FormRow(LABELS["max_position_frac"], self.s_posfrac,
                       "یک معامله حداکثر چند درصد از سقف سرمایه را می‌گیرد. این عدد تعیین می‌کند "
                       "نقدینگی به چند پوزیشن می‌رسد: ۵۰٪ یعنی فقط ۲ تا، ۲۵٪ یعنی ۴ تا، ۲۰٪ یعنی ۵ تا."))
        c3.add(FormRow("حد ضرر (ATR ×)", self.s_atr, "۲ = دو برابر نوسان معمول یک کندل"))
        c3.add(FormRow("نسبت سود به ضرر", self.s_rr, "هدف = این عدد × فاصله‌ی حد ضرر"))
        c3.add(FormRow("تریل بعد از (R)", self.s_trail, "۱ = بعد از یک برابر ریسک سود، حد ضرر به نقطه‌ی ورود می‌آید. ۰ = خاموش"))
        grid.addWidget(c3, 1, 0)

        c4 = Card("کنترل صفحه", "برای صرافی‌هایی که API ندارند (مثل KCEX)")
        self.s_cu_on = QCheckBox("سفارش‌ها را با کنترل ماوس/کیبورد روی سایت صرافی ثبت کن"); self.s_cu_on.setChecked(s.computer.enabled)
        self.s_cu_confirm = QCheckBox("قبل از کلیک نهایی از من بپرس (پیشنهاد: روشن)"); self.s_cu_confirm.setChecked(s.computer.confirm_before_submit)
        self.s_cu_notes = QTextEdit(s.computer.exchange_notes); self.s_cu_notes.setMinimumHeight(110)
        self.s_cu_notes.setPlaceholderText("سایت صرافی کجا باز است و فرم سفارش چه شکلی است…")
        c4.add(self.s_cu_on); c4.add(self.s_cu_confirm); c4.add(FormRow("توضیح صرافی", self.s_cu_notes, "با انتخاب kcex خودش پر می‌شود"))
        c4.add(section("پروکسی / VPN"))
        self.s_proxy_mode = QComboBox(); self.s_proxy_mode.addItems(["system", "manual", "none"]); self.s_proxy_mode.setCurrentText(getattr(s, "proxy_mode", "system"))
        self.s_proxy = QLineEdit(s.exchange.proxy); self.s_proxy.setPlaceholderText("http://127.0.0.1:10809")
        c4.add(FormRow("حالت پروکسی", self.s_proxy_mode, "system = پروکسی سیستم ویندوز که VPN تنظیم می‌کند (یا مستقیم اگر نبود) · manual = آدرس پایین · none = مستقیم"))
        c4.add(FormRow("آدرس پروکسی", self.s_proxy, "v2rayN: http://127.0.0.1:10809 · Clash: http://127.0.0.1:7890 · Nekoray: http://127.0.0.1:2080"))
        self.lbl_proxy_probe = hint(""); c4.add(self.lbl_proxy_probe)
        c4.add(button("🌐 تست اتصال از این پروکسی (نمایش IP و کشور)", "", self._probe_proxy))
        c4.add(section("پیشرفته"))
        self.s_loop = QSpinBox(); self.s_loop.setRange(2, 3600); self.s_loop.setValue(s.loop_seconds)
        self.s_mt5_login = QLineEdit(str(s.mt5_login or "")); self.s_mt5_pass = QLineEdit(s.mt5_password); self.s_mt5_pass.setEchoMode(QLineEdit.Password)
        self.s_mt5_server = QLineEdit(s.mt5_server)
        c4.add(FormRow("فاصله بررسی (ثانیه)", self.s_loop, "هر چند ثانیه بازار بررسی شود. ۶۰ برای روزانه؛ برای scalp روی تایم‌فریم کوتاه ۲ تا ۵"))
        c4.add(FormRow("MT5 login", self.s_mt5_login, "فقط فارکس")); c4.add(FormRow("MT5 password", self.s_mt5_pass)); c4.add(FormRow("MT5 server", self.s_mt5_server))
        c4.add(button("⬇ دانلود و نصب MetaTrader 5", "ghost", self._install_mt5))
        grid.addWidget(c4, 1, 1)
        v.addLayout(grid)

        save = button("💾 ذخیره تنظیمات", "primary", self._save_settings); save.setMinimumHeight(44); v.addWidget(save)
        v.addWidget(hint("اگر موتور در حال اجراست، برای اعمال تغییرات آن را متوقف و دوباره شروع کن."))
        self.s_exchange.currentTextChanged.connect(self._exchange_changed); self.s_mode.currentTextChanged.connect(lambda _: self._exchange_changed(self.s_exchange.currentText()))
        self._exchange_changed(self.s_exchange.currentText())
        return self._scroll(inner)

    def _preset(self, kind: str):
        """One click that fills every field for a coherent mode, saves, and asks for a restart."""
        if kind == "smart":
            # 8 symbols is what was asked for; the DAILY timeframe is what the measurement
            # supports. The same eight symbols on 5m lost money on every single one of them.
            self.s_agg.setCurrentText("high"); self.s_effort.setCurrentText("max"); self.s_llm.setChecked(True)
            self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT, DOGE/USDT, ADA/USDT, AVAX/USDT")
            self.s_maxpos.setValue(8); self.s_tf.setCurrentText("1d")
            self.s_cap.setValue(1000); self.s_paper_bal.setValue(1000); self.s_loop.setValue(30)
            # position_pct 0 = size from the stop. Measured on the same 8 coins and the same
            # 600 daily bars: auto sizing -5.6%, "20% of capital per trade" -18.5%, and the
            # worst drawdown went 20% -> 30%. Sizing by notional ignores the stop distance, so
            # a wide-stop trade risks several times what a tight-stop one does.
            self.s_pospct.setValue(0)
            self.s_rr.setValue(2.0); self.s_trail.setValue(1.0)
            msg = ("حالت هوشمند چندارزی اعمال شد: ۸ ارز، تایم‌فریم روزانه، دقت max، تا ۸ پوزیشن.\n\n"
                   "صادقانه بگویم: در شبیه‌سازی خودِ موتور روی یک حساب ۱۰۰۰ دلاری و ۶۰۰ کندل روزانه، "
                   "پخش‌کردن همان پول روی ۸ ارز بدتر از ۳ ارز درآمد (‎-۶.۴٪‎ در برابر ‎+۰.۵٪‎)، "
                   "چون نقدینگی بین همه تقسیم می‌شود. اگر هدف سود است، «جدی و صبور» را بزن.")
        elif kind == "serious":
            self.s_agg.setCurrentText("normal"); self.s_effort.setCurrentText("max"); self.s_llm.setChecked(True)
            self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT")
            self.s_maxpos.setValue(3); self.s_tf.setCurrentText("1d"); self.s_loop.setValue(60); self.s_pospct.setValue(0)
            self.s_rr.setValue(2.0); self.s_trail.setValue(1.0)
            msg = ("حالت جدی و صبور اعمال شد: روزانه، normal، ۳ ارز، اندازه‌ی خودکار.\n\n"
                   "بهترین ترکیبی بود که اندازه‌گیری شد، ولی بزرگش نمی‌کنم: ‎+۰.۵٪‎ روی حساب مشترک "
                   "۱۰۰۰ دلاری با بیشترین افت ۹.۳٪ — یعنی عملاً سربه‌سر. همان قوانین با ۸ ارز "
                   "‎-۶.۴٪‎ و با «۲۰٪ در هر معامله» ‎-۳۰٪‎ دادند، پس ارزشِ این تنظیم در نبردنِ پول است.\n"
                   "عددها از ۶۰۰ کندل روزانه با کارمزد و لغزش واقعی است، نه تضمین آینده.")
        else:  # scalp
            self.s_agg.setCurrentText("scalp"); self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT")
            # 5m, not 1m: of the fast timeframes it was the least bad when measured
            # (-8.6% against -18.3% at 15m, and 1m is worse still). All of them lose.
            self.s_maxpos.setValue(6); self.s_tf.setCurrentText("5m"); self.s_loop.setValue(3)
            self.s_cap.setValue(1000); self.s_paper_bal.setValue(1000); self.s_pospct.setValue(15)
            msg = ("حالت اسکالپ اعمال شد: پرتعداد و سریع.\n\n"
                   "این حالت در بک‌تست روی ۶ ارز و تایم‌فریم ۵ و ۱۵ دقیقه، هیچ‌کدام سودده نبود "
                   "(بین -۹٪ تا -۱۸٪). فقط برای دیدن کارکرد برنامه است، نه برای سود.")
        self._save_settings()
        if self.settings.mode == "paper":
            # Resetting the broker's cash file but leaving the open trades in the journal left
            # rows describing positions nothing held: the next close had to invent them.
            self._reset_paper_now()
        QMessageBox.information(self, "اعمال شد", msg + "\n\nحالا برو داشبورد و «توقف» بعد «شروع» را بزن تا فعال شود.")
        self.goto("dashboard")

    def _exchange_changed(self, ex: str):
        ex = ex.strip().lower()
        if ex in NO_API_EXCHANGES:
            self.lbl_exchange_note.setText(f"{ex}: API معاملاتی ندارد. قیمت و کندل مستقیم از سایتش خوانده می‌شود و سفارش با «کنترل صفحه» ثبت می‌شود. کلید API لازم نیست؛ سایت را در مرورگر باز و لاگین بگذار.")
            if ex == "kcex" and not self.s_cu_notes.toPlainText().strip():
                from ..market.kcex import KCEX_SCREEN_NOTES
                self.s_cu_notes.setPlainText(KCEX_SCREEN_NOTES)
            if self.s_mode.currentText() == "live":
                self.s_cu_on.setChecked(True)
            self.lbl_exchange_note.show()
        else:
            self.lbl_exchange_note.hide()

    def _save_settings(self):
        s = self.settings
        old_paper_bal = s.paper_start_balance
        s.ai_provider = self.s_provider.currentText(); s.openai_api_key = self.s_oai_key.text().strip(); s.openai_model = self.s_oai_model.text().strip() or "gpt-5"
        s.anthropic_api_key = self.s_key.text().strip(); s.model = self.s_model.currentText(); s.effort = self.s_effort.currentText()
        s.use_llm_for_decisions = self.s_llm.isChecked(); s.auto_update = self.s_autoupd.isChecked()
        s.aggressiveness = self.s_agg.currentText(); s.align_with_leader = self.s_align.isChecked()
        s.mode = self.s_mode.currentText(); s.market = self.s_market.currentText()
        s.exchange.exchange_id = self.s_exchange.currentText().strip().lower(); s.data_source = self.s_data_source.currentText()
        s.exchange.api_key = self.s_ex_key.text().strip(); s.exchange.secret = self.s_ex_secret.text().strip()
        s.exchange.password = self.s_ex_pass.text().strip(); s.exchange.proxy = self.s_proxy.text().strip(); s.proxy_mode = self.s_proxy_mode.currentText()
        s.symbols = [x.strip().upper() for x in self.s_symbols.text().split(",") if x.strip()]
        s.timeframe = self.s_tf.currentText(); s.loop_seconds = self.s_loop.value(); s.paper_start_balance = self.s_paper_bal.value()
        s.mt5_login = int(self.s_mt5_login.text()) if self.s_mt5_login.text().strip().isdigit() else 0
        s.mt5_password = self.s_mt5_pass.text(); s.mt5_server = self.s_mt5_server.text().strip()
        s.risk.capital_limit = self.s_cap.value(); s.risk.risk_per_trade = self.s_rpt.value() / 100; s.risk.max_daily_loss = self.s_dl.value() / 100
        s.position_pct = self.s_pospct.value()
        s.risk.max_open_positions = self.s_maxpos.value(); s.risk.atr_stop_mult = self.s_atr.value(); s.risk.reward_risk = self.s_rr.value()
        s.risk.max_open_risk = self.s_openrisk.value() / 100
        s.risk.max_position_frac = self.s_posfrac.value() / 100
        s.risk.trail_after_r = self.s_trail.value()
        s.computer.enabled = self.s_cu_on.isChecked(); s.computer.confirm_before_submit = self.s_cu_confirm.isChecked()
        s.computer.exchange_notes = self.s_cu_notes.toPlainText()
        problems = s.validate() + s.advisories(); s.save()
        for combo in (self.ch_symbol, self.bt_symbol):
            cur = combo.currentText(); combo.blockSignals(True); combo.clear(); combo.addItems(s.symbols)
            combo.setCurrentText(cur if cur in s.symbols else (s.symbols[0] if s.symbols else "")); combo.blockSignals(False)
        self._market = None; self._dash_chart_last = 0; self._start_feed()
        extra = ""
        if s.mode == "paper" and abs(float(s.paper_start_balance) - float(old_paper_bal)) > 1e-9:
            self._reset_paper_now()
            extra = f"\n\nموجودی کاغذی روی {s.paper_start_balance:g} تنظیم و حساب کاغذی ریست شد."
        else:
            self.refresh()
        QMessageBox.information(self, "ذخیره شد", "تنظیمات ذخیره شد." + extra + ("\n\nهشدار:\n" + "\n".join(problems) if problems else ""))

    def _probe_proxy(self):
        from ..net import probe
        self.settings.proxy_mode = self.s_proxy_mode.currentText(); self.settings.exchange.proxy = self.s_proxy.text().strip()
        self.lbl_proxy_probe.setText("در حال بررسی…")
        self._run_bg(lambda: probe(self.settings),
                     lambda i: self.lbl_proxy_probe.setText(f"از طریق {i['proxy']}: IP {i['ip']} · کشور {i['country']} · {i.get('city','')} · {i.get('org','')}"
                                                            + ("   ⚠ هنوز از ایران دیده می‌شوی؛ Bybit/Binance باز نمی‌شوند، منبع داده خودکار عوض می‌شود." if i.get('country') == 'IR' else "")),
                     lambda m: self.lbl_proxy_probe.setText("اتصال برقرار نشد: " + m.splitlines()[0]))

    def _test_llm(self):
        s = self.settings
        s.ai_provider = self.s_provider.currentText(); s.anthropic_api_key = self.s_key.text().strip(); s.model = self.s_model.currentText()
        s.openai_api_key = self.s_oai_key.text().strip(); s.openai_model = self.s_oai_model.text().strip() or "gpt-5"
        self._run_bg(lambda: make_brain(s).ping(), lambda r: QMessageBox.information(self, s.ai_provider, f"پاسخ: {r}\nاتصال برقرار است."))

    # ============================================================ periodic refresh
    def live_mode(self) -> str:
        """The mode the RUNNING engine is in, not the one in the settings form.

        Changing the mode in Settings takes effect only when the engine is restarted, so
        reading settings.mode while it runs made the dashboard show the paper journal while
        the engine traded live, or the reverse - the single most dangerous thing this window
        can get wrong."""
        if self.engine and self.engine.running():
            return self.engine.mode
        return self.settings.mode

    def refresh(self):
        s = self.settings; mode = self.live_mode()
        advice = s.advisories()
        if hasattr(self, "lbl_advice"):
            self.lbl_advice.setText("⚠ " + "  ·  ".join(advice) if advice else "")
            self.lbl_advice.setVisible(bool(advice))
        running = bool(self.engine and self.engine.running())
        from ..risk.manager import RiskManager
        rm = RiskManager(s.risk, self.db, mode)
        kill = rm.kill_switch_on()
        set_pill(self.pill_mode, "کاغذی" if mode == "paper" else "واقعی", "gold" if mode == "paper" else "danger")
        if self.engine and self.engine.status.get("error"):
            set_pill(self.pill_state, "خطا", "danger")
        else:
            set_pill(self.pill_state, "در حال اجرا" if running else "متوقف", "ok" if running else "muted")
        set_pill(self.side_status, ("⛔ اضطراری" if kill else ("● در حال اجرا" if running else "○ متوقف")), "danger" if kill else ("ok" if running else "muted"))
        self.btn_run.setText("■  توقف" if running else "▶  شروع"); self.btn_run.setObjectName("danger" if running else "primary")
        self.btn_run.style().unpolish(self.btn_run); self.btn_run.style().polish(self.btn_run)
        self.btn_kill.setText("⛔ اضطراری: روشن" if kill else "⛔ اضطراری"); self.btn_kill.setObjectName("danger" if kill else "ghost")
        self.btn_kill.style().unpolish(self.btn_kill); self.btn_kill.style().polish(self.btn_kill)

        ok_ai = s.has_llm(); ok_ex = bool(s.exchange.exchange_id and s.symbols); ok_risk = s.risk.capital_limit > 0 and s.risk.risk_per_trade > 0
        for key, ok in (("ai", ok_ai), ("exchange", ok_ex), ("risk", ok_risk)):
            set_pill(self.setup_rows[key], "انجام شد" if ok else "انجام نشده", "ok" if ok else "warn")
        self.card_setup.setVisible(not (ok_ai and ok_ex and ok_risk))

        curve = self.db.equity_curve(mode, limit=500)
        self.kpi_equity.set(f"{curve[-1][1]:,.2f}" if curve else "—")
        daily = rm.daily_pnl()
        self.kpi_daily.set(f"{daily:+,.2f}", f"سقف زیان روزانه {s.risk.max_daily_loss*s.risk.capital_limit:,.2f}", "green" if daily > 0 else ("red" if daily < 0 else ""))
        opens = self.db.open_trades(mode)
        self.kpi_open.set(str(len(opens)), f"از حداکثر {s.risk.max_open_positions}")
        st = self.db.trade_stats(mode)
        self.kpi_win.set(f"{st['win_rate']*100:.0f}%" if st["trades"] else "—", f"{st['trades']} معامله بسته‌شده")
        self.eq_curve.set_points(curve)
        pf = profit_factor(st)
        small = st["trades"] < 30
        self.lbl_stats_mini.setText(
            f"سود خالص {st['pnl']:+.4f} · ضریب سود {pf} · میانگین R {st['avg_r']:+.2f}"
            + (f" · فقط {st['trades']} معامله — برای قضاوت کم است" if small else "")
            if st["trades"] else "")

        prices = {**(self.engine.last_prices if self.engine else {}), **self._live}
        rows = []
        for r in opens:
            px = prices.get(r["symbol"])
            fl = ((px - r["entry_price"]) if r["side"] == "long" else (r["entry_price"] - px)) * r["qty"] if px else None
            val = float(r["qty"]) * float(r["entry_price"])
            pct = (fl / val * 100) if (fl is not None and val) else None
            rows.append([r["symbol"], "خرید" if r["side"] == "long" else "فروش", f"{r['entry_price']:g}",
                         f"{px:g}" if px else "—", f"${val:.2f}", f"{r['stop_price']:g}",
                         f"{r['take_profit']:g}" if r["take_profit"] else "—",
                         money_pct(fl, pct)])
        self._pos_data = [dict(x) for x in opens]
        fill(self.tbl_positions, rows, tones={7: "pnl"}); self.tbl_positions.setVisible(bool(rows)); self.empty_pos.setVisible(not rows)
        if hasattr(self, "tbl_desk"):
            fill(self.tbl_desk, rows, tones={7: "pnl"}); self.tbl_desk.setVisible(bool(rows)); self.empty_desk.setVisible(not rows)
        self._render_pos_detail()
        decs = self.db.recent_decisions(30)
        fa = {"buy": "خرید", "sell": "فروش", "hold": "نگه‌دار", "close": "بستن"}
        fill(self.tbl_decisions, [[self._ts(d["ts"]), d["symbol"], fa.get(d["action"], d["action"]), f"{d['confidence']:.2f}" if d["confidence"] is not None else "",
                                   d["source"], d["reason"]] for d in decs])
        self.tbl_decisions.setVisible(bool(decs)); self.empty_dec.setVisible(not decs)

        self.kpi_t_n.set(str(st["trades"])); self.kpi_t_win.set(f"{st['win_rate']*100:.0f}%" if st["trades"] else "—")
        self.kpi_t_pnl.set(f"{st['pnl']:+.4f}" if st["trades"] else "—", tone="green" if st["pnl"] > 0 else ("red" if st["pnl"] < 0 else ""))
        self.kpi_t_pf.set(pf if st["trades"] else "—"); self.kpi_t_r.set(f"{st['avg_r']:+.2f}" if st["trades"] else "—")
        closed = self.db.closed_trades(mode, 200)
        fill(self.tbl_trades, [[self._ts(r["opened_at"]), self._ts(r["closed_at"]), r["symbol"], r["side"], f"{r['qty']:g}", f"{r['entry_price']:g}",
                                f"{r['exit_price']:g}" if r["exit_price"] else "", f"{r['pnl']:+.4f}" if r["pnl"] is not None else "",
                                f"{r['r_multiple']:+.2f}" if r["r_multiple"] is not None else "", r["strategy"], r["reason"]] for r in closed], tones={7: "pnl", 8: "pnl"})
        self.tbl_trades.setVisible(bool(closed)); self.empty_trades.setVisible(not closed)

        now = time.time()
        if self.stack.currentWidget() is self.pages["dashboard"] and now - self._dash_chart_last > 60:
            self.refresh_dash_chart()
        if self.stack.currentWidget() is self.pages["chart"] and self.ch_auto.isChecked() and now - self._chart_last > 60:
            self.refresh_chart()
        if self.stack.currentWidget() is self.pages["desk"] and now - getattr(self, "_desk_chart_last", 0) > 60:
            self.refresh_desk_chart()
        if self.tbl_skills.rowCount() == 0 and not self.sk_search.text():
            self.refresh_skills()

    # ------------------------------------------------------------ position detail
    def _show_pos_detail(self, row: int):
        if 0 <= row < len(self._pos_data):
            self._detail_symbol = self._pos_data[row]["symbol"]
            self._render_pos_detail()

    def _hide_pos_detail(self):
        self._detail_symbol = None
        self.pos_detail.hide()

    def _render_pos_detail(self):
        sym = self._detail_symbol
        r = next((p for p in self._pos_data if p["symbol"] == sym), None) if sym else None
        if not r:
            self.pos_detail.hide()
            return
        side = r["side"]; entry = float(r["entry_price"]); stop = float(r["stop_price"] or 0)
        tp = float(r["take_profit"] or 0); qty = float(r["qty"])
        px = self._live.get(sym) or (self.engine.last_prices.get(sym) if self.engine else None) or entry
        fl = ((px - entry) if side == "long" else (entry - px)) * qty
        fl -= float(r.get("entry_fee") or 0.0)      # the fee is already spent, so it is P&L
        # R is measured from the ORIGINAL stop. Using the current one means a trade that has
        # trailed to break-even reports an enormous R and then an infinite one - the same bug
        # the engine and the backtest were both fixed for.
        init_stop = r.get("init_stop") or stop
        rdist = abs(entry - float(init_stop)) if init_stop else 0
        r_now = (fl / (qty * rdist)) if rdist else 0.0
        to_stop = (px - stop) / px * 100 if stop else 0
        to_tp = (tp - px) / px * 100 if tp else 0
        col = theme.SUCCESS if fl >= 0 else theme.DANGER
        fa_side = "خرید (long)" if side == "long" else "فروش (short)"
        opened = self._ts(r.get("opened_at"))
        html = (
            f"<div style='font-size:15px;color:{theme.ACCENT};font-weight:700'>{sym} &nbsp; <span style='color:{theme.MUTED};font-size:12px'>{fa_side} · {r.get('strategy','')}</span></div>"
            f"<table cellpadding='3' style='font-size:13px'>"
            f"<tr><td style='color:{theme.MUTED}'>مقدار</td><td>{qty:g}</td>"
            f"<td style='color:{theme.MUTED}'>&nbsp;&nbsp;قیمت ورود</td><td>{entry:g}</td>"
            f"<td style='color:{theme.MUTED}'>&nbsp;&nbsp;قیمت فعلی</td><td>{px:g}</td></tr>"
            f"<tr><td style='color:{theme.MUTED}'>حد ضرر</td><td style='color:{theme.DANGER}'>{stop:g} ({to_stop:+.2f}%)</td>"
            f"<td style='color:{theme.MUTED}'>&nbsp;&nbsp;هدف</td><td style='color:{theme.SUCCESS}'>{tp:g} ({to_tp:+.2f}%)</td>"
            f"<td style='color:{theme.MUTED}'>&nbsp;&nbsp;زمان</td><td>{opened}</td></tr>"
            f"<tr><td style='color:{theme.MUTED}'>سود/زیان شناور</td><td style='color:{col};font-weight:700'>{fl:+.4f}</td>"
            f"<td style='color:{theme.MUTED}'>&nbsp;&nbsp;R فعلی</td><td style='color:{col}'>{r_now:+.2f}R</td>"
            f"<td colspan='2'></td></tr>"
            f"</table>"
            f"<div style='color:{theme.MUTED};font-size:12px'>دلیل ورود: {r.get('reason','')}</div>"
        )
        self.pos_detail_lbl.setText(html)
        self.pos_detail.show()

    def _open_pos_chart(self, row: int):
        if 0 <= row < len(self._pos_data):
            self._detail_symbol = self._pos_data[row]["symbol"]
            self._detail_to_chart()

    def _detail_to_chart(self):
        if not self._detail_symbol:
            return
        sym = self._detail_symbol
        self.goto("chart")
        self.ch_symbol.setCurrentText(sym)
        self.refresh_chart()

    def _detail_close_pos(self):
        r = next((p for p in self._pos_data if p["symbol"] == self._detail_symbol), None)
        if not r:
            return
        if QMessageBox.question(self, "", f"پوزیشن {r['symbol']} با قیمت بازار بسته شود؟") != QMessageBox.Yes:
            return
        if not self.engine:
            QMessageBox.information(self, "", "موتور فعال نیست"); return
        px = self._live.get(r["symbol"]) or self.engine.last_prices.get(r["symbol"]) or float(r["entry_price"])
        self._run_bg(lambda: self.engine.close_position(dict(r), px, "manual"), lambda _: (self._hide_pos_detail(), self.refresh()))

    # ------------------------------------------------------------ live price feed
    def _sync_watch_symbols(self) -> None:
        """Recompute the watch list ON THE GUI THREAD. Reading a combo box from the feed thread
        is a data race against Qt: it happened to work and is not allowed to."""
        syms = list(self.settings.symbols)
        for combo_name in ("ch_symbol", "desk_symbol"):
            try:
                cs = getattr(self, combo_name).currentText().strip().upper()
                if cs and cs not in syms:
                    syms.append(cs)
            except Exception:
                pass
        self._watch_cache = syms      # replaced whole, never mutated in place
        focus = []
        for combo_name in ("ch_symbol", "desk_symbol"):
            try:
                cs = getattr(self, combo_name).currentText().strip().upper()
                if cs and cs not in focus:
                    focus.append(cs)
            except Exception:
                pass
        if self.settings.symbols and self.settings.symbols[0] not in focus:
            focus.append(self.settings.symbols[0])    # the dashboard chart
        self._focus_cache = focus

    def _watch_symbols(self) -> list[str]:
        # Called from the feed thread: a plain read of a list the GUI thread swapped in.
        return list(getattr(self, "_watch_cache", None) or self.settings.symbols)

    def _watch_split(self) -> tuple[list[str], list[str]]:
        """(what is on screen, everything else). Read from caches the GUI thread fills."""
        focus = [s for s in (getattr(self, "_focus_cache", None) or []) if s]
        rest = [s for s in self._watch_symbols() if s not in focus]
        return focus, rest

    def _start_feed(self):
        self._stop_feed()
        self._sync_watch_symbols()
        self._feed = PriceFeed(self.settings, self._watch_split)
        self._feed.tick.connect(self._on_tick)
        self._feed.start()

    def _stop_feed(self):
        """Ask the feed to stop and only let go of it once it really has.

        Dropping the last Python reference to a QThread that is still running makes Qt abort the
        whole process ("QThread: Destroyed while thread is still running"). The feed can easily
        be inside a several-second HTTP call over a slow proxy, so waiting 1.5s and then setting
        it to None crashed the app whenever settings were saved at the wrong moment."""
        feed = getattr(self, "_feed", None)
        self._feed = None
        if not feed:
            return
        feed.stop()
        if not feed.wait(1500):
            self._retiring_feeds.append(feed)          # keep it alive until it finishes
        self._retiring_feeds = [f for f in getattr(self, "_retiring_feeds", []) if not f.isFinished()]

    def _on_tick(self, prices: dict):
        self._live.update(prices)
        if self.engine:
            self.engine.last_prices.update(prices)
        # live price on both charts (only shows on the newest bar)
        try:
            cs = self.ch_symbol.currentText().strip().upper()
            if cs in self._live:
                self.chart.set_live_price(self._live[cs])
        except Exception:
            pass
        if self.settings.symbols and self.settings.symbols[0] in self._live:
            self.dash_chart.set_live_price(self._live[self.settings.symbols[0]])
        if hasattr(self, "desk_symbol"):
            ds = self.desk_symbol.currentText().strip().upper()
            if ds in self._live:
                self.desk_chart.set_live_price(self._live[ds])
        # live floating P&L on the open positions + equity, without a full DB refresh cycle
        try:
            opens = [dict(r) for r in self.db.open_trades(self.live_mode())]
        except Exception:
            return
        rows = []
        for r in opens:
            px = self._live.get(r["symbol"])
            fl = ((px - r["entry_price"]) if r["side"] == "long" else (r["entry_price"] - px)) * r["qty"] if px else None
            val = float(r["qty"]) * float(r["entry_price"])
            pct = (fl / val * 100) if (fl is not None and val) else None
            rows.append([r["symbol"], "خرید" if r["side"] == "long" else "فروش", f"{r['entry_price']:g}",
                         f"{px:g}" if px else "—", f"${val:.2f}", f"{r['stop_price']:g}",
                         f"{r['take_profit']:g}" if r["take_profit"] else "—",
                         money_pct(fl, pct)])
        self._pos_data = [dict(x) for x in opens]
        fill(self.tbl_positions, rows, tones={7: "pnl"})
        self.tbl_positions.setVisible(bool(rows)); self.empty_pos.setVisible(not rows)
        if hasattr(self, "tbl_desk"):
            fill(self.tbl_desk, rows, tones={7: "pnl"}); self.tbl_desk.setVisible(bool(rows)); self.empty_desk.setVisible(not rows)
        self._render_pos_detail()
        if self.engine:
            try:
                self.kpi_equity.set(f"{self.engine.broker.equity(self._live):,.2f}")
            except Exception:
                pass

    def closeEvent(self, ev):
        if self.engine and self.engine.running():
            # No question when the installer is already running: the answer would arrive after
            # Windows had failed to replace a locked exe, which is the update loop all over again.
            if not self._quit_for_update and QMessageBox.question(
                    self, "خروج",
                    "موتور در حال اجراست. با بستن برنامه معامله متوقف می‌شود (پوزیشن‌های باز روی صرافی می‌مانند). خارج شوم؟") != QMessageBox.Yes:
                ev.ignore(); return
            # Wait for it: the thread is a daemon, so leaving without it can cut the loop
            # between placing a real exchange order and writing it to the journal.
            self.engine.stop(wait=5.0)
        # From here the window is going. Set the barrier BEFORE joining anything: the periodic
        # refresh is a child of the window and keeps firing after close(), and refresh() can
        # start a chart load - so without this the close path joins the threads that happen to
        # exist at that instant and a fresh one starts on the next event-loop turn.
        self._closing = True
        if getattr(self, "timer", None) is not None:
            self.timer.stop()
        # Only now, once the quit is certain. Stopping the feed before the question meant that
        # cancelling the quit left the window open with dead prices and no way back short of a
        # restart - and an edit that was supposed to re-add this line silently did not apply,
        # so for one release the feed was never stopped at all and Qt aborted on every exit.
        self._stop_feed()
        # Background threads must be joined before the window goes: Qt aborts the process with
        # "QThread: Destroyed while thread is still running" if one is alive at teardown.
        #
        # The lists are NOT cleared afterwards. Clearing them dropped the last Python reference
        # to any thread that had not stopped - which is precisely the object that must stay
        # alive - and it also emptied the lists main() checks, so the os._exit safety net after
        # app.exec() was reading two empty lists and never fired.
        _join_threads(live_threads(), ms=3000)
        ev.accept()


def _join_threads(threads: list, ms: int = 5000) -> list:
    """Wait for background threads. Returns whichever are STILL running.

    It deliberately does NOT call QThread.terminate(). That was tried and it is far worse than
    the problem it solves: terminate() kills a thread wherever it happens to be, and CI caught
    it killing one inside OpenSSL's create_default_context - "Windows fatal exception: access
    violation", a corrupted process rather than a cleanly failing one. Qt's own documentation
    warns about exactly this.

    A thread blocked in a network call cannot be interrupted at all, so the honest options are
    to wait for it or to leave the process. The caller does the second, with os._exit, which is
    immediate and cannot corrupt anything because nothing runs after it."""
    stuck = []
    for t in threads:
        try:
            if not t.wait(ms):
                stuck.append(t)
        except Exception:
            pass
    return stuck


def main() -> int:
    app = QApplication(sys.argv)
    app._wheel_guard = WheelGuard()          # kept alive on the app, or Qt drops the filter
    app.installEventFilter(app._wheel_guard)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(theme.QSS)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow(); win.show()
    code = app.exec()
    # Belt and braces: anything still alive after the window closed is joined or terminated
    # here, so the process can never abort on the way out.
    win._stop_feed()
    _join_threads(live_threads(), ms=3000)
    # Tear the window down while the QApplication is definitely still alive. Left to the
    # interpreter, the order is undefined and Qt can be asked to destroy widgets after its
    # own application object has gone - which is a segfault, not an exception.
    win.deleteLater()
    app.processEvents()
    stuck = live_threads()
    del win
    if stuck:
        # A QThread blocked in a network call cannot be interrupted, and Qt aborts the process
        # when it is destroyed while running - which the user sees as "it crashed when I closed
        # it". Everything is already on disk (every DB write commits immediately, settings and
        # the paper state are written synchronously), so leaving by the front door is strictly
        # better than being killed on the way out - and unlike terminate(), it cannot corrupt
        # anything, because nothing runs after it.
        sys.stdout.flush(); sys.stderr.flush()
        # Distinguishable on purpose. A test that only checks "the process exited 0" cannot tell
        # a clean shutdown from this escape hatch, so it would go on passing after the thing it
        # was written to catch came back. Production still leaves with the real code.
        os._exit(70 if os.environ.get("TGTRADER_STRICT_EXIT") else code)
    return code
