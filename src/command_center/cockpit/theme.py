from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#070b0f"
BG2 = "#04070a"
SURFACE = "#0d1420"
SURFACE2 = "#111b2a"
LINE = "#1d2c40"
LINE2 = "#2a3f5a"
INK = "#dbeaf5"
INK2 = "#9fb6cc"
MUT = "#6b8299"
CHROME = "#3fd0e6"
CHROME2 = "#1e7f93"
GOOD = "#34d399"
WARN = "#f0b429"
BAD = "#ef6a6a"
INFO = "#6ea8fe"

QSS = f"""
QMainWindow, QWidget {{
  background: {BG};
  color: {INK};
  font-family: "DejaVu Sans", "Noto Sans", sans-serif;
  font-size: 10pt;
}}
QFrame#panel {{
  background: {SURFACE};
  border: 1px solid {LINE};
  border-radius: 8px;
}}
QTabWidget::pane {{
  border: 1px solid {LINE};
  border-radius: 8px;
  top: -1px;
  background: {SURFACE};
}}
QTabBar::tab {{
  background: transparent;
  color: {INK2};
  border: 1px solid {LINE2};
  border-bottom: 0;
  border-top-left-radius: 8px;
  border-top-right-radius: 8px;
  padding: 7px 18px;
  margin-right: 4px;
  min-width: 88px;
  text-transform: uppercase;
  letter-spacing: 1px;
}}
QTabBar::tab:selected {{
  background: {CHROME};
  border-color: {CHROME};
  color: {BG2};
  font-weight: 600;
}}
QTabBar::tab:hover {{ color: {INK}; border-color: {CHROME2}; }}
QPushButton {{
  background: {CHROME};
  color: {BG2};
  border: 1px solid {CHROME};
  border-radius: 5px;
  padding: 6px 12px;
  font-weight: 600;
}}
QPushButton:hover {{ background: #63dcea; }}
QPushButton:disabled {{
  background: {SURFACE2};
  border-color: {LINE};
  color: {MUT};
}}
QPushButton#ghost {{
  background: transparent;
  color: {INK2};
  border-color: {LINE2};
  font-weight: 500;
}}
QPushButton#ghost:hover {{ color: {INK}; border-color: {CHROME2}; }}
QPushButton#ghost:disabled {{
  color: #3f5367;
  border-color: {LINE};
  background: transparent;
}}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QTextEdit, QPlainTextEdit {{
  background: {BG2};
  color: {INK};
  border: 1px solid {LINE2};
  border-radius: 5px;
  padding: 5px 7px;
  selection-background-color: {CHROME2};
  selection-color: {INK};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus {{
  border-color: {CHROME2};
}}
QComboBox QAbstractItemView {{
  background: {SURFACE2};
  color: {INK};
  border: 1px solid {LINE2};
  selection-background-color: {CHROME2};
}}
QLabel#muted {{ color: {MUT}; }}
QLabel#value {{ color: {INK}; font-family: "DejaVu Sans Mono", monospace; }}
QLabel#kicker {{
  color: {CHROME};
  font-size: 8pt;
  letter-spacing: 2px;
}}
QLabel#title {{ font-size: 18pt; font-weight: 600; letter-spacing: 2px; }}
QGroupBox {{
  border: 1px solid {LINE};
  border-radius: 7px;
  margin-top: 12px;
  padding: 10px 8px 8px;
  color: {INK2};
}}
QGroupBox::title {{
  subcontrol-origin: margin;
  left: 10px;
  padding: 0 5px;
  color: {CHROME};
  font-size: 8pt;
  letter-spacing: 1px;
  text-transform: uppercase;
}}
QCheckBox {{
  spacing: 8px;
}}
QCheckBox::indicator {{
  width: 14px;
  height: 14px;
  border: 1px solid {LINE2};
  border-radius: 3px;
  background: {BG2};
}}
QCheckBox::indicator:hover {{
  border-color: {CHROME2};
}}
QCheckBox::indicator:checked {{
  background: {CHROME};
  border-color: {CHROME};
}}
QTableWidget, QTreeWidget, QListWidget {{
  background: {SURFACE};
  alternate-background-color: {SURFACE2};
  color: {INK};
  border: 1px solid {LINE};
  border-radius: 6px;
  gridline-color: {LINE};
  selection-background-color: rgba(63, 208, 230, 0.18);
  selection-color: {INK};
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
  padding: 4px 5px;
}}
QHeaderView::section {{
  background: {SURFACE2};
  color: {MUT};
  border: 0;
  border-bottom: 1px solid {LINE2};
  padding: 6px 5px;
  font-size: 8pt;
  letter-spacing: 1px;
  text-transform: uppercase;
}}
QScrollBar:vertical {{
  background: {BG};
  width: 10px;
  margin: 0;
}}
QScrollBar::handle:vertical {{
  background: {LINE2};
  min-height: 24px;
  border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: {CHROME2}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{
  background: {BG};
  height: 10px;
}}
QScrollBar::handle:horizontal {{
  background: {LINE2};
  min-width: 24px;
  border-radius: 5px;
}}
QPlainTextEdit#log {{
  background: {BG2};
  color: {INK2};
  font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace;
  font-size: 9pt;
}}
QStatusBar {{
  background: {BG2};
  color: {MUT};
  border-top: 1px solid {LINE};
}}
QMenu {{
  background: {SURFACE2};
  color: {INK};
  border: 1px solid {LINE2};
}}
QMenu::item:selected {{ background: {CHROME2}; }}
QToolTip {{
  background: {SURFACE2};
  color: {INK};
  border: 1px solid {LINE2};
}}
QSplitter::handle {{ background: {LINE}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(INK))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE2))
    palette.setColor(QPalette.ColorRole.Text, QColor(INK))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE2))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(INK))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(CHROME2))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(INK))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE2))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(INK))
    app.setPalette(palette)
    app.setStyleSheet(QSS)
    app.setFont(QFont("DejaVu Sans", 10))
