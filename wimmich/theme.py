"""Farben und Stylesheet.

Angelehnt an die Anmutung von RapidRAW: ruhig, wenig Rahmen, abgerundete
Flaechen, ein einziger Akzentton. Fotos sollen wirken, nicht die
Oberflaeche.

Zwei Paletten - dunkel (Vorgabe) und hell. set_theme() schreibt die
Werte auf die Modulnamen um; jeder Aufrufer verwendet `theme.BG` &Co.
als normalen Attributzugriff (`from . import theme`), der bei jedem
Zugriff neu ausgewertet wird - eine Umschaltung zur Laufzeit wirkt also
sofort, auch im selbst gezeichneten Code (models.py, canvas.py, ...),
ohne dass jede Stelle einzeln Bescheid wissen muss.
"""

from __future__ import annotations

RADIUS = 8

# Schrift insgesamt 20% groesser als der urspruengliche Ausgangswert (13px)
FONT_SIZE = 16

_DARK = dict(
    BG="#141417", PANEL="#1b1b20", ELEVATED="#23232a", HOVER="#2c2c35",
    BORDER="#2e2e37", TEXT="#e6e6ea", TEXT_MUTED="#8e8e9c",
    ACCENT="#6f8cf5", ACCENT_DIM="#3d4a80", STAR="#f0b429",
    VIEWER_BG="#0d0d10",
    SCROLLBAR="#55555f", SCROLLBAR_HOVER="#7a7a86",
)

_LIGHT = dict(
    BG="#f4f4f6", PANEL="#ffffff", ELEVATED="#ffffff", HOVER="#e9e9ee",
    BORDER="#d6d6dc", TEXT="#1c1c22", TEXT_MUTED="#6b6b76",
    ACCENT="#3d5fd0", ACCENT_DIM="#c4d0f7", STAR="#b8790a",
    VIEWER_BG="#e7e7eb",
    SCROLLBAR="#b9b9c2", SCROLLBAR_HOVER="#8f8f9c",
)

PALETTES = {"dunkel": _DARK, "hell": _LIGHT}

_current_name = "dunkel"


def current_theme() -> str:
    return _current_name


def _apply(values: dict) -> None:
    module = globals()
    for key, value in values.items():
        module[key] = value


# Bauplan des Stylesheets. Bewusst eine normale Zeichenkette und
# keine f-String-Funktion: so laesst sie sich zur Laufzeit lesen
# und pruefen, auch im gebauten Programm ohne Quelltext.
_TEMPLATE = """
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: {FONT_SIZE}px;
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
QTreeWidget::branch {{ background: transparent; }}

/* Raster */
QListView {{
    background: {BG};
    border: none;
    outline: none;
}}

/* Bildlaufleisten - deutlich sichtbarer Griff, nicht nur bei Hover */
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {SCROLLBAR};
    border-radius: 5px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: {SCROLLBAR_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {SCROLLBAR};
    border-radius: 5px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{ background: {SCROLLBAR_HOVER}; }}

/* Fortschrittsbalken beim Immich-Abgleich */
QProgressBar {{
    background: {ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 7px;
    text-align: center;
    color: {TEXT};
    font-size: 12px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 6px;
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


def _build_stylesheet() -> str:
    """Setzt die aktuellen Farbwerte in den Bauplan ein."""
    werte = dict(PALETTES[_current_name])
    werte.update(RADIUS=RADIUS, FONT_SIZE=FONT_SIZE)
    return _TEMPLATE.format(**werte)


def set_theme(name: str) -> str:
    """Wechselt die Palette ('dunkel' oder 'hell') und baut STYLESHEET neu.

    Gibt das neue Stylesheet zurueck; main.py und die Einstellungen
    setzen es zusaetzlich auf die laufende QApplication.
    """
    global _current_name, STYLESHEET
    palette = PALETTES.get(name, _DARK)
    _current_name = "hell" if palette is _LIGHT else "dunkel"
    _apply(palette)
    STYLESHEET = _build_stylesheet()
    return STYLESHEET


def check_palettes() -> list[str]:
    """Prueft beide Paletten auf Vollstaendigkeit. Liefert die Maengelliste.

    Weil die Farbnamen erst zur Laufzeit ueber _apply() entstehen, kann
    kein Pruefwerkzeug einen Tippfehler im Stylesheet-Bauplan finden -
    er faellt sonst erst beim Themenwechsel als NameError auf. Diese
    Pruefung schliesst die Luecke und laeuft beim Import mit.
    """
    import re

    maengel = []
    if set(_DARK) != set(_LIGHT):
        fehlt_hell = set(_DARK) - set(_LIGHT)
        fehlt_dunkel = set(_LIGHT) - set(_DARK)
        if fehlt_hell:
            maengel.append(f"in der hellen Palette fehlen: {sorted(fehlt_hell)}")
        if fehlt_dunkel:
            maengel.append(f"in der dunklen Palette fehlen: {sorted(fehlt_dunkel)}")

    verwendet = set(re.findall(r"(?<!\{)\{([A-Z][A-Z_]*)\}(?!\})", _TEMPLATE))
    eigene = {"RADIUS", "FONT_SIZE"}
    unbekannt = verwendet - set(_DARK) - eigene
    if unbekannt:
        maengel.append(f"im Stylesheet ohne Palettenwert: {sorted(unbekannt)}")
    return maengel


# Einmal beim Import mit der Vorgabe (dunkel) fuellen
_MAENGEL = check_palettes()
if _MAENGEL:                      # nur bei echtem Fehler, nie im Normalbetrieb
    raise RuntimeError("theme.py unvollstaendig: " + "; ".join(_MAENGEL))

STYLESHEET = set_theme("dunkel")
