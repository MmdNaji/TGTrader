"""TGTrader desktop app - sidebar navigation, Persian RTL, dark/gold design system.

The engine runs in its own thread; the window only reads state and issues commands.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QSplitter, QProgressDialog, QTextBrowser, QStackedWidget, QScrollArea, QButtonGroup, QFrame,
)

from .. import __version__, updater
from ..brain import make_brain, claude_client
from ..config import Settings, NO_API_EXCHANGES
from ..db import Database
from ..engine import Engine
from ..knowledge.skills import load_seed_skills, add_extracted, active_skills
from . import theme
from .chart import CandleChart
from .help_fa import HELP_HTML
from .widgets import Card, Kpi, pill, set_pill, hint, section, FormRow, Empty, table, fill, EquityCurve, button

NAV = [
    ("dashboard", "🏠", "داشبورد", "وضعیت حساب، پوزیشن‌ها و تصمیم‌های ربات"),
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
class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as exc:
            self.failed.emit(f"{exc}\n{traceback.format_exc(limit=2)}")


class Bridge(QObject):
    event = Signal(str)
    confirm_request = Signal(str)


class PriceFeed(QThread):
    """Streams the latest price for the watched symbols about once a second, so the chart,
    the stop/target zones and the floating P&L move live. One second is the fastest that is
    safe against an exchange's request limits - true millisecond ticks are not possible over
    a REST price API and the price does not actually change that often."""
    tick = Signal(dict)

    def __init__(self, settings, symbols_fn):
        super().__init__()
        self._settings = settings
        self._symbols_fn = symbols_fn
        self._stop = threading.Event()

    def run(self):
        from ..market.data import MarketData
        try:
            md = MarketData(self._settings)
        except Exception:
            return
        while not self._stop.is_set():
            out = {}
            for sym in self._symbols_fn():
                if self._stop.is_set():
                    break
                try:
                    out[sym] = md.price(sym)
                except Exception:
                    pass
            if out:
                self.tick.emit(out)
            self._stop.wait(1.0)

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
        self._chart_last = 0.0
        self._dash_chart_last = 0.0
        self._market = None          # shared MarketData so exchange metadata loads once

        root = QWidget(); self.setCentralWidget(root)
        h = QHBoxLayout(root); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        h.addWidget(self._sidebar())
        col = QVBoxLayout(); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(0)
        col.addWidget(self._topbar())
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        builders = {"dashboard": self._page_dashboard, "chart": self._page_chart, "trades": self._page_trades,
                    "skills": self._page_skills, "learn": self._page_learn, "backtest": self._page_backtest,
                    "settings": self._page_settings, "help": self._page_help, "selftest": self._page_selftest}
        for key, *_ in NAV:
            w = builders[key](); self.pages[key] = w; self.stack.addWidget(w)
        col.addWidget(self.stack, 1)
        h.addLayout(col, 1)

        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1500)
        self.refresh()
        self.goto("dashboard")
        self._live: dict[str, float] = {}
        self._feed: PriceFeed | None = None
        self._start_feed()
        QTimer.singleShot(4000, lambda: self._check_update(manual=False))

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
        base = updater.base_version()
        foot = QLabel(f"نسخه {__version__}" + (f" (exe {base})" if base != __version__ else "")); foot.setObjectName("sideFoot"); foot.setAlignment(Qt.AlignCenter); v.addWidget(foot)
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
        if key == "skills":
            self.refresh_skills()
        if key == "learn":
            self.refresh_docs()

    # ------------------------------------------------------------ helpers
    def _run_bg(self, fn: Callable[[], Any], on_done: Callable[[Any], None], on_fail: Callable[[str], None] | None = None) -> Worker:
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
        self.tbl_positions = table(["نماد", "جهت", "ورود", "قیمت", "حد ضرر", "هدف", "سود شناور"])
        self.tbl_positions.setTextElideMode(Qt.ElideNone)
        self.tbl_positions.setMinimumHeight(160)
        self.empty_pos = Empty("پوزیشن بازی نیست. وقتی شرایط ورود جور شود، این‌جا ظاهر می‌شود.")
        c3.add(self.tbl_positions); c3.add(self.empty_pos)
        c3.add_action(button("بستن همه", "danger", self.close_all)); c3.add_action(button("ریست کاغذی", "ghost", self.reset_paper))
        c4 = Card("آخرین تصمیم‌ها", "نگه‌داشتن هم یک تصمیم است؛ دلیلش را بخوان")
        self.tbl_decisions = table(["زمان", "نماد", "اقدام", "اطمینان", "منبع", "دلیل"]); self.tbl_decisions.setMinimumHeight(160)
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
        rm = RiskManager(self.settings.risk, self.db, self.settings.mode)
        rm.set_kill_switch(not rm.kill_switch_on()); self.refresh()

    def close_all(self):
        if not self.engine:
            QMessageBox.information(self, "", "موتور فعال نیست"); return
        if QMessageBox.question(self, "", "همه پوزیشن‌ها با قیمت بازار بسته شوند؟") == QMessageBox.Yes:
            self._run_bg(lambda: self.engine.close_all("manual"), lambda _: self.refresh())

    def reset_paper(self):
        from ..execution.paper import PaperBroker
        if QMessageBox.question(self, "", "حساب کاغذی ریست شود؟ (معاملات باز کاغذی بسته حساب می‌شوند)") == QMessageBox.Yes:
            PaperBroker(self.settings.paper_start_balance).reset(self.settings.paper_start_balance)
            for r in self.db.open_trades("paper"):
                self.db.close_trade(r["id"], r["entry_price"], 0.0, 0.0)
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
        self._run_bg(updater.check, lambda rel: self._update_checked(rel, manual),
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
            self._on_event(f"[update] {rel.version} was already attempted recently and did not apply - see {updater.log_path()}; press the update button to retry")
            QMessageBox.warning(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} چند دقیقه پیش نصب شد ولی اعمال نشد (برنامه هنوز {__version__} است).\n"
                                f"لاگ نصب: {updater.log_path()}\nپوشه‌ی برنامه: {updater.install_dir()}\n\n"
                                f"برای تلاش دوباره دکمه‌ی 🔄 را بزن، یا نصب دستی:\n{rel.asset_url}")
            return
        if manual or (self.settings.auto_update and updater.is_frozen() and rel.asset_url):
            if self.engine and self.engine.running() and not manual:
                self._on_event("[update] engine is running - will install when it is stopped"); return
            self._offer_update(rel)

    def _offer_update(self, rel):
        if not updater.is_frozen():
            QMessageBox.information(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} منتشر شده.\n\nاین نسخه از سورس اجرا شده؛ با git pull به‌روز کن یا نصب‌کننده را بگیر:\n{rel.page_url}"); return
        if not rel.asset_url:
            QMessageBox.warning(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} فایل نصب ندارد:\n{rel.page_url}"); return
        if self.engine and self.engine.running():
            QMessageBox.warning(self, "به‌روزرسانی", "اول موتور معامله را متوقف کن، بعد به‌روزرسانی کن."); return
        self._on_event(f"[update] downloading {rel.version} from {rel.source}: {rel.asset_url}")
        dlg = QProgressDialog(f"در حال دانلود نسخه‌ی {rel.version} ({'کد، چند ثانیه' if rel.kind == 'code' else 'نصب کامل'})…", "لغو", 0, 100, self)
        dlg.setWindowTitle("به‌روزرسانی خودکار"); dlg.setAutoClose(False); dlg.setMinimumDuration(0)
        w = Worker(lambda: updater.download(rel, progress=lambda d, t: w.progress.emit(d, t)))

        def on_prog(d, t):
            dlg.setMaximum(max(t, 1)); dlg.setValue(min(d, t) if t else 0)
            dlg.setLabelText(f"در حال دانلود… {d/1e6:.1f} / {t/1e6:.1f} MB\nبعد از دانلود، برنامه بسته و با نسخه‌ی جدید باز می‌شود.")

        def done(path):
            dlg.close()
            if dlg.wasCanceled():
                return
            try:
                updater.mark_attempt(rel.version)
                if rel.kind == "code":
                    updater.apply_code(path)
                    self._on_event(f"[update] code {rel.version} applied to {updater.overlay_dir()}; restarting")
                    updater.restart_app()
                else:
                    updater.install(path)
                    self._on_event(f"[update] installer started for {rel.version} into {updater.install_dir()}; closing")
            except Exception as exc:
                QMessageBox.critical(self, "به‌روزرسانی", f"{exc}\n\nفایل دانلودشده:\n{path}"); return
            QApplication.instance().quit()

        w.progress.connect(on_prog); w.done.connect(done)
        w.failed.connect(lambda m: (dlg.close(), QMessageBox.critical(self, "به‌روزرسانی", m.splitlines()[0])))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start(); dlg.exec()

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
        self.ch_symbol.currentTextChanged.connect(lambda _: self.refresh_chart()); self.ch_tf.currentTextChanged.connect(lambda _: self.refresh_chart())
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
            pos = next((dict(r) for r in self.db.open_trades(self.settings.mode) if r["symbol"] == sym), None)
            trades = [dict(r) for r in self.db.closed_trades(self.settings.mode, 300) if r["symbol"] == sym]
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
            return run_backtest(sym, df, self.settings.risk, start_equity=self.settings.risk.capital_limit, allow_short=short)

        def done(res):
            self.btn_bt.setEnabled(True)
            st = res.stats()
            self.kpi_bt["return_pct"].set(f"{st['return_pct']:+.2f}%", tone="green" if st["return_pct"] > 0 else "red")
            self.kpi_bt["max_drawdown_pct"].set(f"{st['max_drawdown_pct']:.2f}%")
            self.kpi_bt["profit_factor"].set(f"{st['profit_factor']:.2f}" if st["profit_factor"] != float("inf") else "∞", tone="green" if st["profit_factor"] > 1 else "red")
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
        cp.add(hint("«هوشمند چندارزی»: ۸ ارز، Claude با دقت max و ۸۶ مهارت، تا ۸ پوزیشن، تایم‌فریم ۵ دقیقه، موجودی کاغذی ۱۰۰۰. "
                    "برای دیدن معامله‌ی زیاد روی ارزهای مختلف. یادت باشد سود تضمینی نیست."))
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
        self.s_tf = QComboBox(); self.s_tf.addItems(["5m", "15m", "30m", "1h", "4h", "1d"]); self.s_tf.setCurrentText(s.timeframe)
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
        c2.add(FormRow("موجودی کاغذی", self.s_paper_bal, "موجودی شروع حساب کاغذی"))
        grid.addWidget(c2, 0, 1)

        c3 = Card("ریسک", "سقف‌های سخت؛ هوش مصنوعی نمی‌تواند از این‌ها رد شود")
        self.s_cap = QDoubleSpinBox(); self.s_cap.setRange(1, 1e9); self.s_cap.setValue(s.risk.capital_limit)
        self.s_rpt = QDoubleSpinBox(); self.s_rpt.setRange(0.1, 10); self.s_rpt.setSuffix(" %"); self.s_rpt.setValue(s.risk.risk_per_trade * 100)
        self.s_dl = QDoubleSpinBox(); self.s_dl.setRange(0.5, 50); self.s_dl.setSuffix(" %"); self.s_dl.setValue(s.risk.max_daily_loss * 100)
        self.s_maxpos = QSpinBox(); self.s_maxpos.setRange(1, 20); self.s_maxpos.setValue(s.risk.max_open_positions)
        self.s_atr = QDoubleSpinBox(); self.s_atr.setRange(0.5, 6); self.s_atr.setValue(s.risk.atr_stop_mult)
        self.s_rr = QDoubleSpinBox(); self.s_rr.setRange(0.5, 10); self.s_rr.setValue(s.risk.reward_risk)
        self.s_trail = QDoubleSpinBox(); self.s_trail.setRange(0, 5); self.s_trail.setValue(s.risk.trail_after_r)
        c3.add(FormRow("سقف سرمایه‌ی ربات", self.s_cap, "ربات هرگز بیش از این مبلغ را درگیر نمی‌کند"))
        c3.add(FormRow("ریسک هر معامله", self.s_rpt, "حداکثر ضرر یک معامله، درصدی از سقف. ۱٪ = با سقف ۱۰۰ دلار، ۱ دلار"))
        c3.add(FormRow("حداکثر زیان روزانه", self.s_dl, "با رسیدن به آن، تا فردا معامله‌ی جدیدی باز نمی‌شود"))
        c3.add(FormRow("حداکثر پوزیشن باز", self.s_maxpos))
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
            self.s_agg.setCurrentText("high"); self.s_effort.setCurrentText("max"); self.s_llm.setChecked(True)
            self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT, DOGE/USDT, ADA/USDT, AVAX/USDT")
            self.s_maxpos.setValue(8); self.s_tf.setCurrentText("5m")
            self.s_cap.setValue(1000); self.s_paper_bal.setValue(1000); self.s_loop.setValue(3)
            msg = "حالت هوشمند چندارزی اعمال شد: ۸ ارز، دقت max، تا ۸ پوزیشن."
        elif kind == "serious":
            self.s_agg.setCurrentText("normal"); self.s_effort.setCurrentText("max"); self.s_llm.setChecked(True)
            self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT")
            self.s_maxpos.setValue(3); self.s_tf.setCurrentText("1d"); self.s_loop.setValue(60)
            msg = "حالت جدی و صبور اعمال شد: روزانه، normal، برای پول واقعی."
        else:  # scalp
            self.s_agg.setCurrentText("scalp"); self.s_symbols.setText("BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT")
            self.s_maxpos.setValue(6); self.s_tf.setCurrentText("1m"); self.s_loop.setValue(2)
            self.s_cap.setValue(1000); self.s_paper_bal.setValue(1000)
            msg = "حالت اسکالپ اعمال شد: پرتعداد و سریع، فقط برای تماشا (در بلندمدت ضرر می‌دهد)."
        self._save_settings()
        if self.settings.mode == "paper":
            from ..execution.paper import PaperBroker
            PaperBroker(self.settings.paper_start_balance).reset(self.settings.paper_start_balance)
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
        s.ai_provider = self.s_provider.currentText(); s.openai_api_key = self.s_oai_key.text().strip(); s.openai_model = self.s_oai_model.text().strip() or "gpt-5"
        s.anthropic_api_key = self.s_key.text().strip(); s.model = self.s_model.currentText(); s.effort = self.s_effort.currentText()
        s.use_llm_for_decisions = self.s_llm.isChecked(); s.auto_update = self.s_autoupd.isChecked()
        s.aggressiveness = self.s_agg.currentText()
        s.mode = self.s_mode.currentText(); s.market = self.s_market.currentText()
        s.exchange.exchange_id = self.s_exchange.currentText().strip().lower(); s.data_source = self.s_data_source.currentText()
        s.exchange.api_key = self.s_ex_key.text().strip(); s.exchange.secret = self.s_ex_secret.text().strip()
        s.exchange.password = self.s_ex_pass.text().strip(); s.exchange.proxy = self.s_proxy.text().strip(); s.proxy_mode = self.s_proxy_mode.currentText()
        s.symbols = [x.strip().upper() for x in self.s_symbols.text().split(",") if x.strip()]
        s.timeframe = self.s_tf.currentText(); s.loop_seconds = self.s_loop.value(); s.paper_start_balance = self.s_paper_bal.value()
        s.mt5_login = int(self.s_mt5_login.text()) if self.s_mt5_login.text().strip().isdigit() else 0
        s.mt5_password = self.s_mt5_pass.text(); s.mt5_server = self.s_mt5_server.text().strip()
        s.risk.capital_limit = self.s_cap.value(); s.risk.risk_per_trade = self.s_rpt.value() / 100; s.risk.max_daily_loss = self.s_dl.value() / 100
        s.risk.max_open_positions = self.s_maxpos.value(); s.risk.atr_stop_mult = self.s_atr.value(); s.risk.reward_risk = self.s_rr.value()
        s.risk.trail_after_r = self.s_trail.value()
        s.computer.enabled = self.s_cu_on.isChecked(); s.computer.confirm_before_submit = self.s_cu_confirm.isChecked()
        s.computer.exchange_notes = self.s_cu_notes.toPlainText()
        problems = s.validate(); s.save()
        for combo in (self.ch_symbol, self.bt_symbol):
            cur = combo.currentText(); combo.blockSignals(True); combo.clear(); combo.addItems(s.symbols)
            combo.setCurrentText(cur if cur in s.symbols else (s.symbols[0] if s.symbols else "")); combo.blockSignals(False)
        self._market = None; self._dash_chart_last = 0; self._start_feed(); self.refresh()
        QMessageBox.information(self, "ذخیره شد", "تنظیمات ذخیره شد." + ("\n\nهشدار:\n" + "\n".join(problems) if problems else ""))

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
    def refresh(self):
        s = self.settings; mode = s.mode
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
        pf = (f"{st['profit_factor']:.2f}" if st["profit_factor"] != float("inf") else "∞")
        self.lbl_stats_mini.setText(f"سود خالص {st['pnl']:+.4f} · ضریب سود {pf} · میانگین R {st['avg_r']:+.2f}" if st["trades"] else "")

        prices = self.engine.last_prices if self.engine else {}
        rows = []
        for r in opens:
            px = prices.get(r["symbol"])
            fl = ((px - r["entry_price"]) if r["side"] == "long" else (r["entry_price"] - px)) * r["qty"] if px else None
            rows.append([r["symbol"], "خرید" if r["side"] == "long" else "فروش", f"{r['entry_price']:g}",
                         f"{px:g}" if px else "—", f"{r['stop_price']:g}",
                         f"{r['take_profit']:g}" if r["take_profit"] else "—", f"{fl:+.4f}" if fl is not None else "—"])
        fill(self.tbl_positions, rows, tones={6: "pnl"}); self.tbl_positions.setVisible(bool(rows)); self.empty_pos.setVisible(not rows)
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
        if self.tbl_skills.rowCount() == 0 and not self.sk_search.text():
            self.refresh_skills()

    # ------------------------------------------------------------ live price feed
    def _watch_symbols(self) -> list[str]:
        syms = list(self.settings.symbols)
        try:
            cs = self.ch_symbol.currentText().strip().upper()
            if cs and cs not in syms:
                syms.append(cs)
        except Exception:
            pass
        return syms

    def _start_feed(self):
        self._stop_feed()
        self._feed = PriceFeed(self.settings, self._watch_symbols)
        self._feed.tick.connect(self._on_tick)
        self._feed.start()

    def _stop_feed(self):
        if getattr(self, "_feed", None):
            self._feed.stop()
            self._feed.wait(1500)
            self._feed = None

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
        # live floating P&L on the open positions + equity, without a full DB refresh cycle
        try:
            opens = [dict(r) for r in self.db.open_trades(self.settings.mode)]
        except Exception:
            return
        rows = []
        for r in opens:
            px = self._live.get(r["symbol"])
            fl = ((px - r["entry_price"]) if r["side"] == "long" else (r["entry_price"] - px)) * r["qty"] if px else None
            rows.append([r["symbol"], "خرید" if r["side"] == "long" else "فروش", f"{r['entry_price']:g}",
                         f"{px:g}" if px else "—", f"{r['stop_price']:g}",
                         f"{r['take_profit']:g}" if r["take_profit"] else "—", f"{fl:+.4f}" if fl is not None else "—"])
        fill(self.tbl_positions, rows, tones={6: "pnl"})
        self.tbl_positions.setVisible(bool(rows)); self.empty_pos.setVisible(not rows)
        if self.engine:
            try:
                self.kpi_equity.set(f"{self.engine.broker.equity(self._live):,.2f}")
            except Exception:
                pass

    def closeEvent(self, ev):
        self._stop_feed()
        if self.engine and self.engine.running():
            if QMessageBox.question(self, "خروج", "موتور در حال اجراست. با بستن برنامه معامله متوقف می‌شود (پوزیشن‌های باز روی صرافی می‌مانند). خارج شوم؟") != QMessageBox.Yes:
                ev.ignore(); return
            self.engine.stop()
        ev.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(theme.QSS)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow(); win.show()
    return app.exec()
