"""Farben und Stylesheet.

Angelehnt an die Anmutung von RapidRAW: dunkel, ruhig, wenig Rahmen,
abgerundete Flächen, ein einziger Akzentton. Fotos sollen wirken, nicht
die Oberflaeche.
"""

from __future__ import annotations

# -- Palette ----------------------------------------------------------

BG          = "#141417"   # Fensterhintergrund
PANEL       = "#1b1b20"   # Seitenleiste, Leisten
ELEVATED    = "#23232a"   # Eingabefelder, Knöpfe
HOVER       = "#2c2c35"
BORDER      = "#2e2e37"

TEXT        = "#e6e6ea"
TEXT_MUTED  = "#8e8e9c"

ACCENT      = "#6f8cf5"   # Auswahl, Fokus
ACCENT_DIM  = "#3d4a80"
STAR        = "#f0b429"   # Bewertungen
BADGE_BG    = "#000000"   # halbtransparent gezeichnet, siehe models.py

TILE_BG     = "#000000"   # hinter dem Bild in der Kachel
VIEWER_BG   = "#0d0d10"

RADIUS      = 8


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 13px;
}}

QMainWindow, QDialog {{ background: {BG}; }}

/* Werkzeugleiste */
QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    spacing: 6px;
}}
QToolBar QToolButton {{
    background: transparent;
    color: {TEXT};
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 6px 12px;
}}
QToolBar QToolButton:hover {{
    background: {HOVER};
    border-color: {BORDER};
}}
QToolBar QToolButton:pressed {{ background: {ELEVATED}; }}

/* Suchleiste */
QLineEdit {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 7px 14px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit::placeholder {{ color: {TEXT_MUTED}; }}

QComboBox {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px 10px;
    min-height: 18px;
}}
QComboBox:hover {{ border-color: {ACCENT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    selection-background-color: {ACCENT_DIM};
    outline: none;
    padding: 4px;
}}

QPushButton {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:checked {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #ffffff;
}}

/* Ordnerbaum */
QTreeWidget {{
    background: {PANEL};
    border: none;
    border-right: 1px solid {BORDER};
    outline: none;
    padding: 6px 4px;
}}
QTreeWidget::item {{
    padding: 5px 4px;
    border-radius: 6px;
    color: {TEXT_MUTED};
}}
QTreeWidget::item:hover {{ background: {HOVER}; color: {TEXT}; }}
QTreeWidget::item:selected {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}

/* Raster */
QListView {{
    background: {BG};
    border: none;
    outline: none;
}}

/* Bildlaufleisten */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 5px;
    min-width: 40px;
}}

/* Statuszeile */
QStatusBar {{
    background: {PANEL};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {TEXT_MUTED}; padding-right: 8px; }}

/* Reiter im Einstellungsfenster */
QTabWidget::pane {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    border: 1px solid transparent;
    border-top-left-radius: {RADIUS}px;
    border-top-right-radius: {RADIUS}px;
    padding: 8px 18px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{ color: {TEXT}; background: {HOVER}; }}
QTabBar::tab:selected {{
    background: {PANEL};
    color: {TEXT};
    border-color: {BORDER};
    border-bottom-color: {PANEL};
}}

QListWidget {{
    background: {ELEVATED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 5px; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {ACCENT_DIM}; color: #ffffff; }}

QSpinBox {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px 8px;
}}
QSpinBox:focus {{ border-color: {ACCENT}; }}

QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {ELEVATED};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QSplitter::handle {{ background: {BORDER}; width: 1px; }}

QToolTip {{
    background: {ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
}}

QMessageBox {{ background: {PANEL}; }}
"""
