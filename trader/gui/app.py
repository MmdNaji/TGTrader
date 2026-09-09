"""PySide6 desktop app. Persian UI, RTL. The engine runs in its own thread; the GUI only reads."""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox,
    QCheckBox, QFileDialog, QMessageBox, QGroupBox, QFormLayout, QPlainTextEdit, QHeaderView, QSplitter,
    QProgressDialog, QTextBrowser,
)

from .. import __version__
from .. import updater
from ..config import Settings
from ..db import Database
from ..engine import Engine
from ..knowledge.skills import load_seed_skills, add_extracted, active_skills
from .help_fa import HELP_HTML

DARK = """
QWidget { background:#0b0e13; color:#e6e6e6; font-size:13px; }
QTabWidget::pane { border:1px solid #2a2f3a; }
QTabBar::tab { background:#151a23; padding:8px 16px; border:1px solid #2a2f3a; }
QTabBar::tab:selected { background:#1f2633; color:#E9C46A; }
QPushButton { background:#1f2633; border:1px solid #3a4152; padding:6px 12px; border-radius:6px; }
QPushButton:hover { border-color:#E9C46A; }
QPushButton#danger { background:#4a1d1d; border-color:#a33; }
QPushButton#gold { background:#E9C46A; color:#0b0e13; font-weight:bold; }
QTableWidget { gridline-color:#2a2f3a; background:#0f131a; }
QHeaderView::section { background:#151a23; padding:4px; border:0; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox { background:#0f131a; border:1px solid #2a2f3a; padding:4px; }
QGroupBox { border:1px solid #2a2f3a; margin-top:14px; padding-top:8px; }
QGroupBox::title { color:#E9C46A; subcontrol-origin: margin; left:10px; }
QLabel#big { font-size:22px; color:#E9C46A; font-weight:bold; }
"""


# ---------------------------------------------------------------- worker thread
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
    """Cross-thread signals: engine events and the computer-use confirmation dialog."""
    event = Signal(str)
    confirm_request = Signal(str)


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TGTrader {__version__}")
        self.resize(1200, 780)
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

        tabs = QTabWidget()
        tabs.addTab(self._dashboard_tab(), "داشبورد")
        tabs.addTab(self._trades_tab(), "معاملات")
        tabs.addTab(self._skills_tab(), "مهارت‌ها")
        tabs.addTab(self._learn_tab(), "یادگیری")
        tabs.addTab(self._backtest_tab(), "بک‌تست")
        tabs.addTab(self._settings_tab(), "تنظیمات")
        tabs.addTab(self._help_tab(), "📖 راهنما")
        self.setCentralWidget(tabs)
        self.tabs = tabs

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.refresh()
        self._pending_update: updater.Release | None = None
        QTimer.singleShot(4000, lambda: self._check_update(manual=False))

    # ------------------------------------------------------------ helpers
    def _run_bg(self, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        w = Worker(fn)
        w.done.connect(on_done)
        w.failed.connect(lambda m: QMessageBox.critical(self, "خطا", m))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _table(self, headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        return t

    @staticmethod
    def _fill(t: QTableWidget, rows: list[list[Any]]) -> None:
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                t.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))

    @staticmethod
    def _ts(ts: float | None) -> str:
        return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""

    # ------------------------------------------------------------ dashboard
    def _dashboard_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lbl_mode = QLabel("—"); self.lbl_mode.setObjectName("big")
        self.lbl_equity = QLabel("—"); self.lbl_equity.setObjectName("big")
        self.lbl_daily = QLabel("—"); self.lbl_daily.setObjectName("big")
        self.lbl_state = QLabel("متوقف")
        for cap, lbl in (("حالت", self.lbl_mode), ("سرمایه", self.lbl_equity), ("سود/زیان امروز", self.lbl_daily), ("وضعیت", self.lbl_state)):
            box = QVBoxLayout(); box.addWidget(QLabel(cap)); box.addWidget(lbl); top.addLayout(box)
        v.addLayout(top)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("▶ شروع"); self.btn_start.setObjectName("gold"); self.btn_start.clicked.connect(self.start_engine)
        self.btn_stop = QPushButton("■ توقف"); self.btn_stop.clicked.connect(self.stop_engine)
        self.btn_kill = QPushButton("⛔ کلید اضطراری"); self.btn_kill.setObjectName("danger"); self.btn_kill.clicked.connect(self.toggle_kill)
        self.btn_close_all = QPushButton("بستن همه پوزیشن‌ها"); self.btn_close_all.clicked.connect(self.close_all)
        self.btn_reset_paper = QPushButton("ریست حساب کاغذی"); self.btn_reset_paper.clicked.connect(self.reset_paper)
        self.btn_update = QPushButton(f"🔄 بررسی به‌روزرسانی (v{__version__})"); self.btn_update.clicked.connect(lambda: self._check_update(manual=True))
        for b in (self.btn_start, self.btn_stop, self.btn_kill, self.btn_close_all, self.btn_reset_paper, self.btn_update):
            btns.addWidget(b)
        v.addLayout(btns)

        split = QSplitter(Qt.Vertical)
        g1 = QGroupBox("پوزیشن‌های باز"); l1 = QVBoxLayout(g1)
        self.tbl_positions = self._table(["نماد", "جهت", "مقدار", "ورود", "قیمت", "حد ضرر", "هدف", "سود شناور", "استراتژی"])
        l1.addWidget(self.tbl_positions)
        g2 = QGroupBox("آخرین تصمیم‌ها"); l2 = QVBoxLayout(g2)
        self.tbl_decisions = self._table(["زمان", "نماد", "اقدام", "اطمینان", "منبع", "دلیل"])
        l2.addWidget(self.tbl_decisions)
        g3 = QGroupBox("گزارش"); l3 = QVBoxLayout(g3)
        self.txt_log = QPlainTextEdit(); self.txt_log.setReadOnly(True); self.txt_log.setMaximumBlockCount(500)
        l3.addWidget(self.txt_log)
        split.addWidget(g1); split.addWidget(g2); split.addWidget(g3)
        v.addWidget(split)
        return w

    def start_engine(self):
        problems = self.settings.validate()
        if problems:
            QMessageBox.warning(self, "تنظیمات ناقص", "\n".join(problems)); return
        if self.settings.mode == "live":
            ok = QMessageBox.question(self, "معامله واقعی",
                                      f"ربات با پول واقعی معامله می‌کند.\nسقف سرمایه: {self.settings.risk.capital_limit}\n"
                                      f"ریسک هر معامله: {self.settings.risk.risk_per_trade*100:.1f}%\nادامه؟")
            if ok != QMessageBox.Yes:
                return
        try:
            brain = None
            if self.settings.use_llm_for_decisions and self.settings.has_llm():
                from ..brain.claude import Brain
                brain = Brain(self.settings)
            broker = None
            if self.settings.computer.enabled and self.settings.mode == "live":
                from ..execution.computer import ComputerBroker
                if brain is None:
                    raise RuntimeError("کنترل صفحه به کلید Claude نیاز دارد")
                broker = ComputerBroker(self.settings, brain.client, confirm=self._confirm_blocking,
                                        on_step=lambda s: self.bridge.event.emit("[screen] " + s))
            self.engine = Engine(self.settings, self.db, broker=broker, brain=brain,
                                 on_event=self.bridge.event.emit)
            self.engine.start()
        except Exception as exc:
            QMessageBox.critical(self, "خطا در شروع", str(exc))

    def stop_engine(self):
        if self.engine:
            self.engine.stop()

    def toggle_kill(self):
        from ..risk.manager import RiskManager
        rm = RiskManager(self.settings.risk, self.db, self.settings.mode)
        rm.set_kill_switch(not rm.kill_switch_on())
        self.refresh()

    def close_all(self):
        if not self.engine:
            QMessageBox.information(self, "", "موتور فعال نیست"); return
        if QMessageBox.question(self, "", "همه پوزیشن‌ها با قیمت بازار بسته شوند؟") == QMessageBox.Yes:
            self._run_bg(lambda: self.engine.close_all("manual"), lambda _: self.refresh())

    def reset_paper(self):
        from ..execution.paper import PaperBroker
        if QMessageBox.question(self, "", "حساب کاغذی ریست شود؟ (معاملات باز کاغذی هم بسته حساب می‌شوند)") == QMessageBox.Yes:
            PaperBroker(self.settings.paper_start_balance).reset(self.settings.paper_start_balance)
            for r in self.db.open_trades("paper"):
                self.db.close_trade(r["id"], r["entry_price"], 0.0, 0.0)
            self.refresh()

    # confirmation for the computer executor: called from the engine thread, answered on the GUI thread
    def _confirm_blocking(self, summary: str) -> bool:
        ev = threading.Event()
        self._confirm_result = {"event": ev, "ok": False}
        self.bridge.confirm_request.emit(summary)
        ev.wait(timeout=120)
        return bool(self._confirm_result.get("ok"))

    def _on_confirm_request(self, summary: str):
        ok = QMessageBox.question(self, "تأیید سفارش روی صفحه", f"ربات می‌خواهد این سفارش را ثبت کند:\n\n{summary}\n\nتأیید می‌کنی؟")
        self._confirm_result["ok"] = ok == QMessageBox.Yes
        self._confirm_result["event"].set()

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
        w = Worker(updater.check)
        w.done.connect(lambda rel: self._update_checked(rel, manual))
        if manual:
            w.failed.connect(lambda m: QMessageBox.warning(self, "به‌روزرسانی", f"بررسی انجام نشد:\n{m.splitlines()[0]}"))
        else:
            w.failed.connect(lambda m: self._on_event("[update] check failed: " + m.splitlines()[0]))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w); w.start()

    def _update_checked(self, rel, manual: bool):
        if rel is None:
            if manual:
                QMessageBox.information(self, "به‌روزرسانی", f"نسخه‌ی فعلی ({__version__}) آخرین نسخه است.")
            return
        self._pending_update = rel
        self.btn_update.setText(f"🔄 نسخه‌ی {rel.version} آماده است - نصب")
        self.btn_update.setObjectName("gold"); self.btn_update.style().unpolish(self.btn_update); self.btn_update.style().polish(self.btn_update)
        self._on_event(f"[update] version {rel.version} is available")
        if manual:
            self._offer_update(rel)
        elif self.settings.auto_update and updater.is_frozen() and rel.asset_url:
            if self.engine and self.engine.running():
                self._on_event("[update] engine is running - will install when it is stopped (or press the update button)")
            else:
                self._offer_update(rel, auto=True)

    def _offer_update(self, rel, auto: bool = False):
        notes = (rel.notes[:800] + "…") if len(rel.notes) > 800 else rel.notes
        if not updater.is_frozen():
            QMessageBox.information(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} منتشر شده.\n{notes}\n\nاین نسخه از سورس اجرا شده؛ با git pull به‌روز کن یا نصب‌کننده را از این‌جا بگیر:\n{rel.page_url}")
            return
        if not rel.asset_url:
            QMessageBox.warning(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} فایل نصب ویندوز ندارد:\n{rel.page_url}"); return
        if self.engine and self.engine.running():
            QMessageBox.warning(self, "به‌روزرسانی", "اول موتور معامله را متوقف کن، بعد به‌روزرسانی کن."); return
        if not auto and QMessageBox.question(self, "به‌روزرسانی", f"نسخه‌ی {rel.version} دانلود و نصب شود؟ برنامه بسته و دوباره باز می‌شود.\n\n{notes}") != QMessageBox.Yes:
            return
        self._on_event(f"[update] downloading {rel.version}" + (" automatically" if auto else ""))
        dlg = QProgressDialog(f"در حال دانلود نسخه‌ی {rel.version}…", "لغو", 0, 100, self); dlg.setWindowTitle("به‌روزرسانی خودکار" if auto else "به‌روزرسانی"); dlg.setAutoClose(False); dlg.setMinimumDuration(0)
        w = Worker(lambda: updater.download(rel, progress=lambda d, t: w.progress.emit(d, t)))

        def on_prog(d, t):
            dlg.setMaximum(max(t, 1)); dlg.setValue(min(d, t) if t else 0)
            dlg.setLabelText(f"در حال دانلود… {d/1e6:.1f} / {t/1e6:.1f} MB")

        def done(path):
            dlg.close()
            if dlg.wasCanceled():
                return
            try:
                updater.install(path)
            except Exception as exc:
                QMessageBox.critical(self, "به‌روزرسانی", str(exc)); return
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

    # ------------------------------------------------------------ trades
    def _trades_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        self.lbl_stats = QLabel("—"); v.addWidget(self.lbl_stats)
        self.tbl_trades = self._table(["باز", "بسته", "نماد", "جهت", "مقدار", "ورود", "خروج", "سود/زیان", "R", "استراتژی", "دلیل"])
        v.addWidget(self.tbl_trades)
        return w

    # ------------------------------------------------------------ skills
    def _skills_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        for cap, st in (("تأیید", "approved"), ("غیرفعال", "disabled"), ("پیش‌نویس", "draft")):
            b = QPushButton(cap); b.clicked.connect(lambda _, s=st: self._skill_status(s)); h.addWidget(b)
        b = QPushButton("حذف"); b.setObjectName("danger"); b.clicked.connect(self._skill_delete); h.addWidget(b)
        h.addStretch()
        self.lbl_skill_count = QLabel(""); h.addWidget(self.lbl_skill_count)
        v.addLayout(h)
        split = QSplitter(Qt.Vertical)
        self.tbl_skills = self._table(["#", "وضعیت", "دسته", "نام", "منبع"])
        self.tbl_skills.itemSelectionChanged.connect(self._skill_selected)
        self.txt_skill = QTextEdit(); self.txt_skill.setReadOnly(True)
        split.addWidget(self.tbl_skills); split.addWidget(self.txt_skill)
        v.addWidget(split)
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
                self.txt_skill.setPlainText(f"{r['name']}\n[{r['category']}] وزن {r['weight']}\n\n{r['rule']}")

    def refresh_skills(self):
        rows = self.db.skills()
        self._fill(self.tbl_skills, [[r["id"], r["status"], r["category"], r["name"], r["source"]] for r in rows])
        self.lbl_skill_count.setText(f"{sum(1 for r in rows if r['status']=='approved')} فعال از {len(rows)}")

    # ------------------------------------------------------------ learn
    def _learn_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        g = QGroupBox("افزودن به کتابخانه"); h = QHBoxLayout(g)
        b1 = QPushButton("📄 کتاب / PDF"); b1.clicked.connect(self._add_pdf)
        self.in_url = QLineEdit(); self.in_url.setPlaceholderText("آدرس مقاله https://…")
        b2 = QPushButton("🌐 افزودن لینک"); b2.clicked.connect(self._add_url)
        b3 = QPushButton("✍ متن دستی"); b3.clicked.connect(self._add_text)
        h.addWidget(b1); h.addWidget(self.in_url); h.addWidget(b2); h.addWidget(b3)
        v.addWidget(g)

        g2 = QGroupBox("کتابخانه"); l2 = QVBoxLayout(g2)
        self.tbl_docs = self._table(["#", "نوع", "حروف", "عنوان", "افزوده شده"])
        l2.addWidget(self.tbl_docs)
        hb = QHBoxLayout()
        b4 = QPushButton("🧠 یادگیری از سند انتخاب‌شده (استخراج مهارت)"); b4.setObjectName("gold"); b4.clicked.connect(self._learn_doc)
        b5 = QPushButton("حذف سند"); b5.clicked.connect(self._delete_doc)
        hb.addWidget(b4); hb.addWidget(b5); l2.addLayout(hb)
        v.addWidget(g2)

        g3 = QGroupBox("آموزش مستقیم (چت)"); l3 = QVBoxLayout(g3)
        self.txt_chat = QPlainTextEdit(); self.txt_chat.setReadOnly(True); l3.addWidget(self.txt_chat)
        hc = QHBoxLayout()
        self.in_chat = QLineEdit(); self.in_chat.setPlaceholderText("مثلاً: وقتی RSI زیر ۳۰ بود ولی روند نزولی قوی بود، خرید نکن")
        self.in_chat.returnPressed.connect(self._send_chat)
        bs = QPushButton("ارسال"); bs.clicked.connect(self._send_chat)
        hc.addWidget(self.in_chat); hc.addWidget(bs); l3.addLayout(hc)
        v.addWidget(g3)
        return w

    def _selected_doc_id(self) -> int | None:
        rows = self.tbl_docs.selectionModel().selectedRows()
        return int(self.tbl_docs.item(rows[0].row(), 0).text()) if rows else None

    def _add_pdf(self):
        from ..knowledge.ingest import ingest_pdf
        path, _ = QFileDialog.getOpenFileName(self, "انتخاب PDF", "", "PDF (*.pdf);;Text (*.txt *.md)")
        if not path:
            return
        if path.lower().endswith(".pdf"):
            self._run_bg(lambda: ingest_pdf(self.db, path), lambda r: self._ingested(r))
        else:
            from ..knowledge.ingest import ingest_text
            text = open(path, encoding="utf-8", errors="ignore").read()
            self._ingested(ingest_text(self.db, path.split("/")[-1], text, "text", path))

    def _add_url(self):
        from ..knowledge.ingest import ingest_url
        url = self.in_url.text().strip()
        if url:
            self._run_bg(lambda: ingest_url(self.db, url), lambda r: self._ingested(r))

    def _add_text(self):
        from ..knowledge.ingest import ingest_text
        dlg = QTextEdit(); dlg.setWindowTitle("متن"); dlg.setMinimumSize(600, 400)
        box = QMessageBox(self); box.setWindowTitle("متن دستی"); box.setText("متن را وارد کن (بعد OK):")
        box.layout().addWidget(dlg, 1, 0, 1, box.layout().columnCount())
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if box.exec() == QMessageBox.Ok and dlg.toPlainText().strip():
            self._ingested(ingest_text(self.db, dlg.toPlainText()[:60].replace("\n", " "), dlg.toPlainText(), "text", "manual"))

    def _ingested(self, r):
        doc_id, text = r
        self.refresh_docs()
        QMessageBox.information(self, "افزوده شد", f"سند #{doc_id} با {len(text):,} حرف ذخیره شد. حالا «یادگیری» را بزن تا مهارت‌ها استخراج شوند.")

    def _learn_doc(self):
        doc_id = self._selected_doc_id()
        if doc_id is None:
            QMessageBox.information(self, "", "اول یک سند انتخاب کن"); return
        if not self.settings.has_llm():
            QMessageBox.warning(self, "", "کلید Claude در تنظیمات وارد نشده"); return
        from ..brain.claude import Brain
        doc = self.db.one("SELECT * FROM knowledge_docs WHERE id=?", (doc_id,))
        text = "\n\n".join(self.db.doc_chunks(doc_id))
        self.txt_chat.appendPlainText(f"… در حال خواندن «{doc['title']}» ({len(text):,} حرف). چند دقیقه طول می‌کشد.")

        def job():
            skills = Brain(self.settings).extract_skills(doc["title"], text)
            n = add_extracted(self.db, skills, source=f"{doc['kind']}:{doc['title']}")
            return len(skills), n
        self._run_bg(job, self._learned)

    def _learned(self, r):
        found, new = r
        self.refresh_skills()
        self.txt_chat.appendPlainText(f"✓ {found} قانون پیدا شد، {new} مهارت جدید به عنوان پیش‌نویس ذخیره شد. در تب مهارت‌ها بررسی و تأیید کن.")
        self.tabs.setCurrentIndex(2)

    def _delete_doc(self):
        doc_id = self._selected_doc_id()
        if doc_id is not None and QMessageBox.question(self, "", "سند حذف شود؟ (مهارت‌های استخراج‌شده می‌مانند)") == QMessageBox.Yes:
            self.db.delete_doc(doc_id); self.refresh_docs()

    def _send_chat(self):
        msg = self.in_chat.text().strip()
        if not msg:
            return
        if not self.settings.has_llm():
            QMessageBox.warning(self, "", "کلید Claude در تنظیمات وارد نشده"); return
        from ..brain.claude import Brain
        self.in_chat.clear()
        self.txt_chat.appendPlainText(f"شما: {msg}")
        self.teach_history.append({"role": "user", "content": msg})
        hist = list(self.teach_history)

        def job():
            return Brain(self.settings).teach_chat(hist, active_skills(self.db))

        def done(r):
            reply, skills = r
            self.teach_history.append({"role": "assistant", "content": reply})
            self.txt_chat.appendPlainText(f"ربات: {reply}")
            if skills:
                n = add_extracted(self.db, skills, source="user", status="approved")
                self.txt_chat.appendPlainText(f"✓ {n} مهارت ذخیره و فعال شد: " + "، ".join(s["name"] for s in skills))
                self.refresh_skills()
        self._run_bg(job, done)

    def refresh_docs(self):
        self._fill(self.tbl_docs, [[d["id"], d["kind"], f"{d['chars']:,}", d["title"], self._ts(d["added_at"])] for d in self.db.docs()])

    # ------------------------------------------------------------ backtest
    def _backtest_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.bt_symbol = QLineEdit(self.settings.symbols[0] if self.settings.symbols else "BTC/USDT")
        self.bt_tf = QComboBox(); self.bt_tf.addItems(["15m", "30m", "1h", "4h", "1d"]); self.bt_tf.setCurrentText(self.settings.timeframe)
        self.bt_bars = QSpinBox(); self.bt_bars.setRange(200, 5000); self.bt_bars.setValue(1000)
        b = QPushButton("اجرای بک‌تست"); b.setObjectName("gold"); b.clicked.connect(self._run_bt)
        for lab, wid in (("نماد", self.bt_symbol), ("تایم‌فریم", self.bt_tf), ("تعداد کندل", self.bt_bars)):
            h.addWidget(QLabel(lab)); h.addWidget(wid)
        h.addWidget(b); v.addLayout(h)
        self.txt_bt = QPlainTextEdit(); self.txt_bt.setReadOnly(True); v.addWidget(self.txt_bt)
        self.tbl_bt = self._table(["استراتژی", "جهت", "ورود", "خروج", "سود/زیان", "R", "دلیل"]); v.addWidget(self.tbl_bt)
        return w

    def _run_bt(self):
        from ..market.data import MarketData
        from ..backtest.engine import run_backtest
        sym, tf, bars = self.bt_symbol.text().strip(), self.bt_tf.currentText(), self.bt_bars.value()
        self.txt_bt.setPlainText("در حال دریافت داده و اجرا…")

        def job():
            df = MarketData(self.settings).candles(sym, tf, limit=bars)
            return run_backtest(sym, df, self.settings.risk, start_equity=self.settings.risk.capital_limit)

        def done(res):
            st = res.stats()
            self.txt_bt.setPlainText("\n".join(f"{k}: {v}" for k, v in st.items()))
            self._fill(self.tbl_bt, [[t.strategy, t.side, f"{t.entry:.6g}", f"{t.exit:.6g}", f"{t.pnl:+.4f}", f"{t.r:+.2f}", t.reason] for t in res.trades])
        self._run_bg(job, done)

    # ------------------------------------------------------------ help
    def _help_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        tb = QTextBrowser(); tb.setOpenExternalLinks(True); tb.setHtml(HELP_HTML)
        tb.setStyleSheet("QTextBrowser{background:#0f131a;border:1px solid #2a2f3a;padding:12px}")
        v.addWidget(tb)
        return w

    # ------------------------------------------------------------ settings
    def _settings_tab(self) -> QWidget:
        w = QWidget(); outer = QVBoxLayout(w)
        grid = QGridLayout(); outer.addLayout(grid)
        s = self.settings

        g1 = QGroupBox("Claude"); f1 = QFormLayout(g1)
        self.s_key = QLineEdit(s.anthropic_api_key); self.s_key.setEchoMode(QLineEdit.Password)
        self.s_model = QComboBox(); self.s_model.addItems(["claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"]); self.s_model.setCurrentText(s.model)
        self.s_effort = QComboBox(); self.s_effort.addItems(["low", "medium", "high", "xhigh", "max"]); self.s_effort.setCurrentText(s.effort)
        self.s_llm = QCheckBox("تصمیم نهایی با Claude (با مهارت‌ها)"); self.s_llm.setChecked(s.use_llm_for_decisions)
        bt = QPushButton("تست اتصال"); bt.clicked.connect(self._test_llm)
        self.s_autoupd = QCheckBox("به‌روزرسانی خودکار موقع باز شدن برنامه"); self.s_autoupd.setChecked(s.auto_update)
        f1.addRow("API key", self.s_key); f1.addRow("مدل", self.s_model); f1.addRow("دقت (effort)", self.s_effort); f1.addRow("", self.s_llm); f1.addRow("", bt); f1.addRow("", self.s_autoupd)
        grid.addWidget(g1, 0, 0)

        g2 = QGroupBox("بازار و صرافی"); f2 = QFormLayout(g2)
        self.s_mode = QComboBox(); self.s_mode.addItems(["paper", "live"]); self.s_mode.setCurrentText(s.mode)
        self.s_market = QComboBox(); self.s_market.addItems(["crypto", "forex"]); self.s_market.setCurrentText(s.market)
        self.s_exchange = QLineEdit(s.exchange.exchange_id)
        self.s_ex_key = QLineEdit(s.exchange.api_key); self.s_ex_secret = QLineEdit(s.exchange.secret); self.s_ex_secret.setEchoMode(QLineEdit.Password)
        self.s_ex_pass = QLineEdit(s.exchange.password); self.s_ex_pass.setEchoMode(QLineEdit.Password)
        self.s_proxy = QLineEdit(s.exchange.proxy)
        self.s_symbols = QLineEdit(", ".join(s.symbols))
        self.s_tf = QComboBox(); self.s_tf.addItems(["5m", "15m", "30m", "1h", "4h", "1d"]); self.s_tf.setCurrentText(s.timeframe)
        self.s_loop = QSpinBox(); self.s_loop.setRange(10, 3600); self.s_loop.setValue(s.loop_seconds)
        self.s_paper_bal = QDoubleSpinBox(); self.s_paper_bal.setRange(1, 1e9); self.s_paper_bal.setValue(s.paper_start_balance)
        f2.addRow("حالت", self.s_mode); f2.addRow("بازار", self.s_market); f2.addRow("صرافی (ccxt id)", self.s_exchange)
        f2.addRow("API key صرافی", self.s_ex_key); f2.addRow("Secret", self.s_ex_secret); f2.addRow("Passphrase", self.s_ex_pass)
        f2.addRow("پروکسی", self.s_proxy); f2.addRow("نمادها", self.s_symbols); f2.addRow("تایم‌فریم", self.s_tf)
        f2.addRow("فاصله بررسی (ثانیه)", self.s_loop); f2.addRow("موجودی کاغذی", self.s_paper_bal)
        self.s_mt5_login = QLineEdit(str(s.mt5_login or "")); self.s_mt5_pass = QLineEdit(s.mt5_password); self.s_mt5_pass.setEchoMode(QLineEdit.Password)
        self.s_mt5_server = QLineEdit(s.mt5_server)
        bmt5 = QPushButton("⬇ دانلود و نصب MetaTrader 5 (فارکس)"); bmt5.clicked.connect(self._install_mt5)
        f2.addRow("MT5 login", self.s_mt5_login); f2.addRow("MT5 password", self.s_mt5_pass); f2.addRow("MT5 server", self.s_mt5_server); f2.addRow("", bmt5)
        grid.addWidget(g2, 0, 1)

        g3 = QGroupBox("ریسک (سقف‌های سخت)"); f3 = QFormLayout(g3)
        self.s_cap = QDoubleSpinBox(); self.s_cap.setRange(1, 1e9); self.s_cap.setValue(s.risk.capital_limit)
        self.s_rpt = QDoubleSpinBox(); self.s_rpt.setRange(0.1, 10); self.s_rpt.setSuffix(" %"); self.s_rpt.setValue(s.risk.risk_per_trade * 100)
        self.s_dl = QDoubleSpinBox(); self.s_dl.setRange(0.5, 50); self.s_dl.setSuffix(" %"); self.s_dl.setValue(s.risk.max_daily_loss * 100)
        self.s_maxpos = QSpinBox(); self.s_maxpos.setRange(1, 20); self.s_maxpos.setValue(s.risk.max_open_positions)
        self.s_atr = QDoubleSpinBox(); self.s_atr.setRange(0.5, 6); self.s_atr.setValue(s.risk.atr_stop_mult)
        self.s_rr = QDoubleSpinBox(); self.s_rr.setRange(0.5, 10); self.s_rr.setValue(s.risk.reward_risk)
        self.s_trail = QDoubleSpinBox(); self.s_trail.setRange(0, 5); self.s_trail.setValue(s.risk.trail_after_r)
        f3.addRow("سقف سرمایه‌ی ربات", self.s_cap); f3.addRow("ریسک هر معامله", self.s_rpt); f3.addRow("حداکثر زیان روزانه", self.s_dl)
        f3.addRow("حداکثر پوزیشن باز", self.s_maxpos); f3.addRow("حد ضرر (ATR ×)", self.s_atr); f3.addRow("نسبت سود به ضرر", self.s_rr)
        f3.addRow("تریل بعد از (R)", self.s_trail)
        grid.addWidget(g3, 1, 0)

        g4 = QGroupBox("کنترل صفحه (صرافی بدون API)"); f4 = QFormLayout(g4)
        self.s_cu_on = QCheckBox("سفارش‌ها را با کنترل ماوس/کیبورد روی سایت صرافی ثبت کن"); self.s_cu_on.setChecked(s.computer.enabled)
        self.s_cu_confirm = QCheckBox("قبل از کلیک نهایی از من بپرس"); self.s_cu_confirm.setChecked(s.computer.confirm_before_submit)
        self.s_cu_notes = QTextEdit(s.computer.exchange_notes); self.s_cu_notes.setPlaceholderText("مثلاً: سایت صرافی در کروم باز است، تب اول. فرم سفارش سمت راست صفحه‌ی معامله است. …")
        f4.addRow("", self.s_cu_on); f4.addRow("", self.s_cu_confirm); f4.addRow("توضیح صرافی", self.s_cu_notes)
        grid.addWidget(g4, 1, 1)

        bs = QPushButton("💾 ذخیره تنظیمات"); bs.setObjectName("gold"); bs.clicked.connect(self._save_settings)
        outer.addWidget(bs)
        return w

    def _save_settings(self):
        s = self.settings
        s.anthropic_api_key = self.s_key.text().strip(); s.model = self.s_model.currentText(); s.effort = self.s_effort.currentText()
        s.use_llm_for_decisions = self.s_llm.isChecked(); s.auto_update = self.s_autoupd.isChecked()
        s.mode = self.s_mode.currentText(); s.market = self.s_market.currentText()
        s.exchange.exchange_id = self.s_exchange.text().strip().lower()
        s.exchange.api_key = self.s_ex_key.text().strip(); s.exchange.secret = self.s_ex_secret.text().strip()
        s.exchange.password = self.s_ex_pass.text().strip(); s.exchange.proxy = self.s_proxy.text().strip()
        s.symbols = [x.strip().upper() for x in self.s_symbols.text().split(",") if x.strip()]
        s.timeframe = self.s_tf.currentText(); s.loop_seconds = self.s_loop.value(); s.paper_start_balance = self.s_paper_bal.value()
        s.mt5_login = int(self.s_mt5_login.text() or 0) if self.s_mt5_login.text().strip().isdigit() else 0
        s.mt5_password = self.s_mt5_pass.text(); s.mt5_server = self.s_mt5_server.text().strip()
        s.risk.capital_limit = self.s_cap.value(); s.risk.risk_per_trade = self.s_rpt.value() / 100; s.risk.max_daily_loss = self.s_dl.value() / 100
        s.risk.max_open_positions = self.s_maxpos.value(); s.risk.atr_stop_mult = self.s_atr.value(); s.risk.reward_risk = self.s_rr.value()
        s.risk.trail_after_r = self.s_trail.value()
        s.computer.enabled = self.s_cu_on.isChecked(); s.computer.confirm_before_submit = self.s_cu_confirm.isChecked()
        s.computer.exchange_notes = self.s_cu_notes.toPlainText()
        problems = s.validate()
        s.save()
        QMessageBox.information(self, "ذخیره شد", "تنظیمات ذخیره شد." + ("\n\nهشدار:\n" + "\n".join(problems) if problems else "")
                                + "\n\nاگر موتور در حال اجراست، برای اعمال تغییرات آن را متوقف و دوباره شروع کن.")

    def _test_llm(self):
        from ..brain.claude import Brain
        self.settings.anthropic_api_key = self.s_key.text().strip(); self.settings.model = self.s_model.currentText()
        self._run_bg(lambda: Brain(self.settings).ping(), lambda r: QMessageBox.information(self, "Claude", f"پاسخ: {r}"))

    # ------------------------------------------------------------ periodic refresh
    def refresh(self):
        mode = self.settings.mode
        self.lbl_mode.setText("کاغذی" if mode == "paper" else "واقعی")
        running = bool(self.engine and self.engine.running())
        from ..risk.manager import RiskManager
        rm = RiskManager(self.settings.risk, self.db, mode)
        state = "در حال اجرا" if running else "متوقف"
        if rm.kill_switch_on():
            state += " · ⛔ کلید اضطراری فعال"
        if self.engine and self.engine.status.get("error"):
            state += " · خطا"
        self.lbl_state.setText(state)
        self.btn_kill.setText("⛔ کلید اضطراری: روشن" if rm.kill_switch_on() else "⛔ کلید اضطراری")
        curve = self.db.equity_curve(mode, limit=1)
        self.lbl_equity.setText(f"{curve[-1][1]:,.2f}" if curve else "—")
        self.lbl_daily.setText(f"{rm.daily_pnl():+,.2f}")

        prices = self.engine.last_prices if self.engine else {}
        rows = []
        for r in self.db.open_trades(mode):
            px = prices.get(r["symbol"])
            fl = ((px - r["entry_price"]) if r["side"] == "long" else (r["entry_price"] - px)) * r["qty"] if px else None
            rows.append([r["symbol"], r["side"], f"{r['qty']:g}", f"{r['entry_price']:g}", f"{px:g}" if px else "", f"{r['stop_price']:g}",
                         f"{r['take_profit']:g}" if r["take_profit"] else "", f"{fl:+.4f}" if fl is not None else "", r["strategy"]])
        self._fill(self.tbl_positions, rows)
        self._fill(self.tbl_decisions, [[self._ts(d["ts"]), d["symbol"], d["action"], f"{d['confidence']:.2f}" if d["confidence"] is not None else "",
                                         d["source"], d["reason"]] for d in self.db.recent_decisions(30)])
        st = self.db.trade_stats(mode)
        self.lbl_stats.setText(f"معاملات: {st['trades']} · نرخ برد: {st['win_rate']*100:.0f}% · سود خالص: {st['pnl']:+.4f} · "
                               f"ضریب سود: {st['profit_factor']:.2f} · میانگین R: {st['avg_r']:+.2f}")
        self._fill(self.tbl_trades, [[self._ts(r["opened_at"]), self._ts(r["closed_at"]), r["symbol"], r["side"], f"{r['qty']:g}", f"{r['entry_price']:g}",
                                      f"{r['exit_price']:g}" if r["exit_price"] else "", f"{r['pnl']:+.4f}" if r["pnl"] is not None else "",
                                      f"{r['r_multiple']:+.2f}" if r["r_multiple"] is not None else "", r["strategy"], r["reason"]]
                                     for r in self.db.closed_trades(mode, 200)])
        if self.tbl_skills.rowCount() == 0:
            self.refresh_skills()
        if self.tbl_docs.rowCount() == 0:
            self.refresh_docs()

    def closeEvent(self, ev):
        if self.engine and self.engine.running():
            if QMessageBox.question(self, "خروج", "موتور در حال اجراست. با بستن برنامه معامله متوقف می‌شود (پوزیشن‌های باز روی صرافی می‌مانند). خارج شوم؟") != QMessageBox.Yes:
                ev.ignore(); return
            self.engine.stop()
        ev.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(DARK)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    return app.exec()
