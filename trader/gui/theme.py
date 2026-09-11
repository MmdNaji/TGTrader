"""Design tokens and the application stylesheet (dark, gold accent, RTL-friendly)."""

BG = "#0b0e13"
SURFACE = "#12161f"
SURFACE2 = "#181d29"
BORDER = "#252c3a"
TEXT = "#e8ebf0"
MUTED = "#8b94a7"
ACCENT = "#E9C46A"
ACCENT_DIM = "#8a7433"
SUCCESS = "#34d399"
DANGER = "#f87171"
WARN = "#fbbf24"
INFO = "#60a5fa"

FONT = "'Segoe UI', 'Vazirmatn', 'Tahoma', sans-serif"

QSS = f"""
* {{ font-family: {FONT}; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QLabel, QCheckBox, QRadioButton, QWidget#transparent {{ background: transparent; }}
QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

/* ---------- sidebar ---------- */
#sidebar {{ background: {SURFACE}; border-left: 1px solid {BORDER}; }}
#brand {{ color: {ACCENT}; font-size: 20px; font-weight: 800; padding: 18px 16px 6px 16px; letter-spacing: 1px; }}
#brandSub {{ color: {MUTED}; font-size: 11px; padding: 0 16px 14px 16px; }}
QPushButton#navBtn {{
    text-align: right; padding: 10px 14px; margin: 2px 10px; border-radius: 10px;
    border: 1px solid transparent; background: transparent; color: {MUTED}; font-size: 14px;
}}
QPushButton#navBtn:hover {{ background: {SURFACE2}; color: {TEXT}; }}
QPushButton#navBtn:checked {{ background: {SURFACE2}; color: {ACCENT}; border-color: {BORDER}; font-weight: 600; }}
#sideFoot {{ color: {MUTED}; font-size: 11px; padding: 10px 16px; }}

/* ---------- top bar ---------- */
#topbar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
#pageTitle {{ font-size: 20px; font-weight: 700; color: {TEXT}; }}
#pageSub {{ font-size: 12px; color: {MUTED}; }}

/* ---------- cards ---------- */
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#cardAccent {{ background: {SURFACE}; border: 1px solid {ACCENT_DIM}; border-radius: 14px; }}
#cardTitle {{ font-size: 14px; font-weight: 700; color: {TEXT}; }}
#cardSub {{ font-size: 12px; color: {MUTED}; }}
#kpiLabel {{ font-size: 12px; color: {MUTED}; }}
#kpiValue {{ font-size: 24px; font-weight: 800; color: {TEXT}; }}
#kpiValueGold {{ font-size: 24px; font-weight: 800; color: {ACCENT}; }}
#kpiValueGreen {{ font-size: 24px; font-weight: 800; color: {SUCCESS}; }}
#kpiValueRed {{ font-size: 24px; font-weight: 800; color: {DANGER}; }}
#kpiSub {{ font-size: 11px; color: {MUTED}; }}
#hint {{ font-size: 11px; color: {MUTED}; }}
#sectionTitle {{ font-size: 13px; font-weight: 700; color: {ACCENT}; padding-top: 6px; }}
#empty {{ color: {MUTED}; font-size: 13px; padding: 24px; }}

/* ---------- pills ---------- */
QLabel#pill, QLabel#pillOk, QLabel#pillWarn, QLabel#pillDanger, QLabel#pillMuted, QLabel#pillGold {{ min-height: 20px; max-height: 20px; }}
QLabel#pillOk {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; background: rgba(52,211,153,0.15); color: {SUCCESS}; }}
QLabel#pillWarn {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; background: rgba(251,191,36,0.15); color: {WARN}; }}
QLabel#pillDanger {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; background: rgba(248,113,113,0.15); color: {DANGER}; }}
QLabel#pillMuted {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; background: {SURFACE2}; color: {MUTED}; }}
QLabel#pillGold {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; background: rgba(233,196,106,0.15); color: {ACCENT}; }}

/* ---------- buttons ---------- */
QPushButton {{ background: {SURFACE2}; border: 1px solid {BORDER}; padding: 8px 14px; border-radius: 9px; color: {TEXT}; }}
QPushButton:hover {{ border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {SURFACE2}; }}
QPushButton#primary {{ background: {ACCENT}; color: {BG}; font-weight: 700; border: none; }}
QPushButton#primary:hover {{ background: #f0d187; }}
QPushButton#danger {{ background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); color: {DANGER}; }}
QPushButton#danger:hover {{ background: rgba(248,113,113,0.22); }}
QPushButton#ghost {{ background: transparent; border-color: {BORDER}; color: {MUTED}; }}
QPushButton#ghost:hover {{ color: {TEXT}; }}
/* A destructive action that sits in a row of similar buttons. Below the compact width they
   all lose their words and become four icons, and one of them erases the account - so this
   one keeps a red edge whatever the width, because an icon on its own says nothing. */
QPushButton#dangerGhost {{ background: transparent; border-color: {DANGER}; color: {DANGER}; }}
QPushButton#dangerGhost:hover {{ background: {DANGER}; color: #ffffff; }}
QPushButton#link {{ background: transparent; border: none; color: {INFO}; padding: 2px 4px; }}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px; color: {TEXT};
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: {SURFACE2}; border: 1px solid {BORDER}; selection-background-color: {BORDER}; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; border: none; background: transparent; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* ---------- tables ---------- */
QTableWidget {{ background: {SURFACE}; border: none; gridline-color: transparent; alternate-background-color: {SURFACE2}; selection-background-color: {BORDER}; }}
QTableWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{ background: {SURFACE}; color: {MUTED}; padding: 8px; border: none; border-bottom: 1px solid {BORDER}; font-size: 11px; font-weight: 600; }}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

/* ---------- misc ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: transparent; }}
QProgressDialog {{ background: {SURFACE}; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 6px; background: {BG}; text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 12px; margin-top: 14px; padding: 12px 8px 8px 8px; }}
QGroupBox::title {{ color: {ACCENT}; subcontrol-origin: margin; subcontrol-position: top right; right: 12px; padding: 0 6px; font-weight: 700; }}
QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; padding: 14px; }}
"""
