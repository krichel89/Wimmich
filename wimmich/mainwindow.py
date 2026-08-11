"""Hauptfenster.

Alles in einem Fenster: links der Baum, rechts entweder das Raster oder
die Lupe. In der Lupe sitzt die Bearbeitungsleiste rechts daneben.
Es gibt keine eigenen Fenster mehr für Ansicht und Bearbeitung.

Die Tastenbelegung ist die von Cammello - siehe _handle_key().
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import (
    QEvent, QFileSystemWatcher, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QIcon, QImage, QKeySequence, QPainter,
    QPainterPath, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
    QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget,
    QStatusBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import (APP_NAME, LICENSE_SHORT, __version__, crashlog, marks,
               previews, retouch, theme, thumbs)
from .config import Config, DB_PATH, ensure_dirs, find_exiftool, is_raw
from .db import Database
from .exif import ExifTool, ExifToolError, read_fast
from .immich import ImmichClient, ImmichError
from .models import PhotoDelegate, PhotoModel, ROLE_ID, ROLE_PATH, ROLE_REMOTE
from .previews import PreviewLoader
from .canvas import CanvasView, NONE as TOOL_NONE, PIPETTE
from .edit_panel import EditPanel
from .filterbar import FilterBar
from .icon import app_icon
from .jahresleiste import Jahresleiste
from . import remote_thumbs
from .edits import (
    CROP, EditStack, FADED, GEOMETRY, RED_EYE, SPOT, STROKE, Step, TONE,
    array_to_qimage, downscale, qimage_to_array,
)
from .previews import decode as decode_image
from .settings import SettingsDialog
from .sync import SyncWorker
from .scanner import ScanWorker

# Seitenverhältnisse im Zuschnitt - Ziffern 1 bis 6 wie in Cammello
ASPECT_PRESETS = {1: None, 2: 3 / 2, 3: 4 / 3, 4: 1.0, 5: 16 / 9, 6: 5 / 4}
ASPECT_NAMES = {1: "frei", 2: "3:2", 3: "4:3", 4: "1:1", 5: "16:9", 6: "5:4"}

PREVIEW_EDGE = 800      # Kantenlänge der Fassung, auf der beim Ziehen gerechnet wird

# Kachel-Option im Delegate -> Schlüssel in der Konfiguration
_KACHEL_KEYS = {
    "packed": "grid_packed",
    "filenames": "show_filenames",
    "stars": "show_stars",
    "labels": "show_labels",
    "stack": "show_stack_badge",
}

SORT_OPTIONS = [
    ("Aufnahmedatum", "taken_at"),
    ("Ordner", "folder"),
    ("Dateiname", "filename"),
    ("Bewertung", "rating"),
    ("Zuletzt geändert", "mtime"),
]


class FolderTree(QTreeWidget):
    """Baum mit eigenen, duennen Aufklapp-Pfeilen im RapidRAW-Stil.

    Qt zeichnet sonst den nativen Systempfeil (auf Windows ein fetter
    Rahmen-Winkel) - hier stattdessen ein schlankes, gefuelltes Dreieck,
    das sich beim Aufklappen um 90 Grad dreht. Es erscheint nur dort, wo
    das Element tatsaechlich Kinder hat (siehe _add_children).
    """

    def drawBranches(self, painter, rect, index) -> None:  # noqa: N802
        item = self.itemFromIndex(index)
        if item is None or item.childCount() == 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(theme.TEXT_MUTED)))

        cx, cy = rect.center().x(), rect.center().y()
        size = 3.6
        path = QPainterPath()
        if item.isExpanded():
            # Spitze nach unten
            path.moveTo(cx - size, cy - size * 0.6)
            path.lineTo(cx + size, cy - size * 0.6)
            path.lineTo(cx, cy + size * 0.75)
        else:
            # Spitze nach rechts
            path.moveTo(cx - size * 0.6, cy - size)
            path.lineTo(cx + size * 0.75, cy)
            path.lineTo(cx - size * 0.6, cy + size)
        path.closeSubpath()
        painter.fillPath(path, QBrush(QColor(theme.TEXT_MUTED)))
        painter.restore()


class MainWindow(QMainWindow):
    start_scan = pyqtSignal(list)

    def __init__(self, melde=None) -> None:
        super().__init__()
        # melde() schreibt eine Zeile aufs Startbild. Ohne Startbild
        # (Tests, Aufruf ohne main.py) tut es schlicht nichts.
        self._melde = melde if callable(melde) else (lambda _t: None)
        ensure_dirs()
        self.config = Config()
        self._melde("Datenbank wird geöffnet …")
        self.db = Database(DB_PATH)

        self._melde("exiftool wird gesucht …")
        exe = find_exiftool(self.config["exiftool"])
        self.exiftool = ExifTool(exe)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1360, 860)
        # Falls das Fenster ohne main.py erzeugt wird (Tests), trotzdem dunkel
        if not self.styleSheet():
            self.setStyleSheet(theme.STYLESHEET)

        self.loader = PreviewLoader(self)
        self._scan_thread: QThread | None = None
        self._sync_thread: QThread | None = None
        self._sync_worker: SyncWorker | None = None

        # Laufender Abgleich: ein Zeitgeber für den regelmäßigen Durchlauf,
        # ein Wächter für Änderungen in den Ordnern. Der Wächter meldet
        # oft mehrfach hintereinander (Kamera schreibt Datei für Datei),
        # deshalb sammelt ein Verzögerer die Meldungen ein.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(False)
        self._sync_timer.timeout.connect(self._auto_sync)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._folder_changed)
        self._watch_delay = QTimer(self)
        self._watch_delay.setSingleShot(True)
        self._watch_delay.setInterval(8000)
        self._watch_delay.timeout.connect(self._changed_settled)
        self._dirty = False
        self._sync_quiet = False
        self._sync_after_scan = False
        # Was der Baum gerade zeigt: ('folder'|'album'|'person', Schlüssel)
        self._selection: tuple[str, str] | None = None

        # Lupe und Bearbeitung
        self._loupe_row = -1
        self._loupe_path = ""
        self._loupe_source = None      # ungeschnittenes RGB-Feld der Ansicht
        self._loupe_small = None       # verkleinert, fürs Ziehen am Regler
        self._stack_edits = EditStack()
        self._want_full = False
        self._show_before = False
        self._info_visible = False
        self._brush = 26
        self._stroke: list[tuple[float, float]] = []
        self._crop_mode = False
        self._crop_aspect_key = None
        self._crop_portrait = False
        # Cammello-Eigenheit: die Ziffern setzen wahlweise Sterne oder Farben
        self._number_mode = "rating"

        # Beim Ziehen am Regler wird auf der kleinen Fassung gerechnet und
        # erst nach einer Ruhepause in voller Größe nachgezogen. Ohne das
        # hängt jeder Reglerschritt an einer knappen Sekunde Rechenzeit.
        self._render_delay = QTimer(self)
        self._render_delay.setSingleShot(True)
        self._render_delay.setInterval(220)
        self._render_delay.timeout.connect(self._render_sharp)

        # Auch das Schreiben in die Datenbank wird gesammelt
        self._save_delay = QTimer(self)
        self._save_delay.setSingleShot(True)
        self._save_delay.setInterval(600)
        self._save_delay.timeout.connect(self._save_edits)

        self._chrome_visible = True
        self._panel_hidden = not bool(self.config["edit_panel"])
        self._gesamt = 0        # Aufnahmen in der aktuellen Ansicht
        self._scan_worker: ScanWorker | None = None

        self._melde("Oberfläche wird aufgebaut …")
        self._build_ui()
        self._build_actions()
        # Erst jetzt anmelden - vorher gibt es die Bauteile nicht, auf
        # die _handle_key zugreift.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.panel_box.setVisible(False)
        self.loupe_header.setVisible(False)
        self.loader.ready.connect(self._image_arrived)
        self._melde("Ordner werden gelesen …")
        self._reload_folder_tree()
        self._update_status()
        self.grid.setFocus()
        self._apply_watch_settings()
        self._apply_sync_settings()
        self._apply_remote_client()

        if not self.config.libraries:
            QTimer.singleShot(300, self._first_run_hint)

    # -- Aufbau --------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Suchleiste
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 6, 8, 6)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "Suchen \u2013 Dateiname, Ordner, Kamera, Titel, Beschreibung, Stichwörter"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self._search_entered)
        bar_layout.addWidget(self.search_box, 1)

        self.filters = FilterBar(self._label_set(), self)
        self.filters.changed.connect(self._refresh_view)
        bar_layout.addWidget(self.filters)

        self.sort_box = QComboBox()
        for label, key in SORT_OPTIONS:
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._refresh_view)
        bar_layout.addWidget(self.sort_box)

        self.sort_desc_button = QPushButton("↓")
        self.sort_desc_button.setCheckable(True)
        self.sort_desc_button.setChecked(bool(self.config["sort_desc"]))
        self.sort_desc_button.setFixedWidth(34)
        self.sort_desc_button.setToolTip(
            "Sortierrichtung umkehren\n"
            "↓ absteigend (z. B. neueste zuerst) · ↑ aufsteigend"
        )
        self.sort_desc_button.toggled.connect(self._toggle_sort_direction)
        bar_layout.addWidget(self.sort_desc_button)
        self._update_sort_direction_label()

        self.recursive_button = QPushButton("Unterordner")
        self.recursive_button.setCheckable(True)
        self.recursive_button.setChecked(bool(self.config["show_subfolders"]))
        self.recursive_button.setToolTip("Bilder aus allen Unterordnern mitzeigen")
        self.recursive_button.toggled.connect(self._toggle_subfolders)
        bar_layout.addWidget(self.recursive_button)

        self.stack_button = QPushButton("RAW+JPG stapeln")
        self.stack_button.setCheckable(True)
        self.stack_button.setChecked(bool(self.config["stack_raw_jpeg"]))
        self.stack_button.setToolTip(
            "RAW und JPEG derselben Aufnahme als eine Kachel zeigen.\n"
            "Bewertungen gelten dann für beide Dateien."
        )
        self.stack_button.toggled.connect(self._toggle_stacking)
        bar_layout.addWidget(self.stack_button)

        self.filter_bar = bar
        outer.addWidget(bar)

        # Ordnerbaum und Raster
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree = FolderTree()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setAnimated(True)
        self.tree.setMinimumWidth(180)
        self.tree.itemSelectionChanged.connect(self._folder_selected)
        self.tree.itemExpanded.connect(self._expand_item)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        splitter.addWidget(self.tree)

        self.model = PhotoModel(
            exiftool=self.exiftool, thumb_edge=self.config["thumb_size"]
        )
        self.model.serverfehler.connect(self._zeige_serverfehler)
        self.model.masse_bekannt.connect(self._masse_bekannt)
        self.grid = QListView()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(
            PhotoDelegate(self.config["grid_size"], self._kachel_optionen()))
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(not self.config["grid_packed"])
        self.grid.setSpacing(2 if self.config["grid_packed"] else 6)
        self.grid.setMouseTracking(True)   # damit die Kachel beim Ueberfahren reagiert
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.doubleClicked.connect(lambda idx: self._show_loupe(idx.row()))
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)

        # Jahresleiste rechts neben dem Raster. Sie zeigt sich nur, wo
        # sie etwas nuetzt: in „Alle Fotos" und „Nur auf dem Server",
        # nach Aufnahmedatum sortiert.
        self.jahresleiste = Jahresleiste(self)
        self.jahresleiste.jahr_gewaehlt.connect(self._springe_zu_jahr)
        self.jahresleiste.setVisible(False)
        self._jahr_verzoegerer = QTimer(self)
        self._jahr_verzoegerer.setSingleShot(True)
        self._jahr_verzoegerer.setInterval(120)
        self._jahr_verzoegerer.timeout.connect(self._jahr_der_ansicht)
        self.grid.verticalScrollBar().valueChanged.connect(
            lambda _v: self._jahr_verzoegerer.start())

        raster_seite = QWidget()
        raster_layout = QHBoxLayout(raster_seite)
        raster_layout.setContentsMargins(0, 0, 0, 0)
        raster_layout.setSpacing(0)
        raster_layout.addWidget(self.grid, 1)
        raster_layout.addWidget(self.jahresleiste)

        # Rechts: Raster ODER Lupe, im selben Fenster
        self.pages = QStackedWidget()
        self.pages.addWidget(raster_seite)       # Seite 0

        loupe = QWidget()
        loupe_layout = QHBoxLayout(loupe)
        loupe_layout.setContentsMargins(0, 0, 0, 0)
        loupe_layout.setSpacing(8)

        self.canvas = CanvasView(self)
        self.canvas.zoom_requested.connect(self._need_full)
        self.canvas.zoom_changed.connect(lambda _f: self._update_overlay())
        self.canvas.fullscreen_requested.connect(self._toggle_fullscreen)
        self.canvas.clicked.connect(self._canvas_click)
        self.canvas.stroke_started.connect(self._stroke_start)
        self.canvas.stroke_moved.connect(self._stroke_move)
        self.canvas.stroke_ended.connect(self._stroke_end)
        self.canvas.crop_changed.connect(self._crop_changed)
        self.canvas.picked.connect(self._pipette_picked)
        loupe_layout.addWidget(self.canvas, 1)

        self.panel = EditPanel(self)
        self.panel.tone_changed.connect(self._tone_changed)
        self.panel.geometry_changed.connect(self._geometry_changed)
        self.panel.rotate_quarter.connect(self._rotate_quarter)
        self.panel.fade_changed.connect(self._fade_changed)
        self.panel.suggest_fade.connect(self._suggest_fade)
        self.panel.pipette_toggled.connect(self._set_pipette)
        self.panel.crop_cleared.connect(self._clear_crop)
        self.panel.reset_all.connect(self._reset_edits)
        self.panel.undo.connect(self._undo_edit)
        self.panel.export.connect(self._export_edited)
        # Die Leiste ist hoch: alle Regler untereinander brauchen 846 px.
        # Ohne Rollbereich zwingt sie dem GANZEN Fenster diese Hoehe als
        # Mindestmass auf (gemessen 810 x 929) - auf einem kleineren
        # Schirm laesst sich das Fenster dann nicht kleiner ziehen und
        # das Bild passt nicht mehr hinein.
        self.panel_box = QScrollArea()
        self.panel_box.setWidget(self.panel)
        self.panel_box.setWidgetResizable(True)
        self.panel_box.setFrameShape(QFrame.Shape.NoFrame)
        self.panel_box.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panel_box.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.panel_box.setFixedWidth(self.panel.width() + 14)
        self.panel_box.setMinimumHeight(80)
        loupe_layout.addWidget(self.panel_box)

        # Formate für den Zuschnitt - erscheint nur im Zuschnitt-Modus
        self.crop_bar = QWidget()
        crop_layout = QHBoxLayout(self.crop_bar)
        crop_layout.setContentsMargins(6, 4, 6, 4)
        crop_layout.addWidget(QLabel("Format:"))
        self._aspect_buttons: dict[int, QPushButton] = {}
        for digit in sorted(ASPECT_PRESETS):
            button = QPushButton(f"{ASPECT_NAMES[digit]}  {digit}")
            button.setCheckable(True)
            button.clicked.connect(lambda _c, d=digit: self._set_aspect(d))
            crop_layout.addWidget(button)
            self._aspect_buttons[digit] = button
        self.portrait_button = QPushButton("hoch / quer")
        self.portrait_button.setToolTip(
            "Gleiche Zifferntaste nochmal kippt ebenfalls")
        self.portrait_button.clicked.connect(self._flip_aspect)
        crop_layout.addWidget(self.portrait_button)
        crop_layout.addStretch(1)
        crop_layout.addWidget(QLabel("Enter übernimmt · Esc bricht ab · Shift+C hebt auf"))
        self.crop_bar.setVisible(False)

        # Kopfzeile der Lupe: zurück ins Raster, Dateiname + Verzeichnis,
        # rechts ein Schalter nur für die Bearbeitungsspalte (unabhängig
        # von Tab/F, die die ganze Oberfläche ausblenden).
        # Bewusst flach gehalten: jede Zeile hier ist Platz, der dem Bild
        # fehlt. Gemessen in 0.3.29 standen ueber der Bildflaeche 122 px
        # Leisten - Menue, Filterleiste und diese Kopfzeile.
        self.loupe_header = QWidget()
        header_layout = QHBoxLayout(self.loupe_header)
        header_layout.setContentsMargins(8, 1, 8, 1)
        header_layout.setSpacing(8)

        back_button = QPushButton("←  Raster")
        back_button.setToolTip("Zurück zur Rasteransicht (G)")
        back_button.clicked.connect(self._show_grid)
        header_layout.addWidget(back_button)

        self.loupe_title = QLabel("")
        self.loupe_title.setTextFormat(Qt.TextFormat.RichText)
        # Der Titel darf schrumpfen. Sonst diktiert der Dateiname die
        # Mindestbreite des ganzen Fensters.
        self.loupe_title.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        self.loupe_title.setMinimumWidth(0)
        header_layout.addWidget(self.loupe_title, 1)

        # Nur bei Serverbildern sichtbar: dort ist die Bearbeitungsleiste
        # gesperrt, weil es keine Datei gibt. Diese beiden Knoepfe sagen,
        # was stattdessen geht.
        self.remote_edit_button = QPushButton("Original holen und bearbeiten")
        self.remote_edit_button.setToolTip(
            "Holt die Datei vom Server in deine Bibliothek und öffnet sie\n"
            "als normales, bearbeitbares Bild.")
        self.remote_edit_button.clicked.connect(
            lambda: self._serverbild_bearbeiten(self._loupe_row))
        self.remote_edit_button.setVisible(False)
        header_layout.addWidget(self.remote_edit_button)

        self.remote_delete_button = QPushButton("Auf dem Server löschen")
        self.remote_delete_button.clicked.connect(
            lambda: self._serverbilder_loeschen([self._loupe_row]))
        self.remote_delete_button.setVisible(False)
        header_layout.addWidget(self.remote_delete_button)

        self.panel_toggle = QPushButton("✎")
        self.panel_toggle.setCheckable(True)
        self.panel_toggle.setChecked(bool(self.config["edit_panel"]))
        self.panel_toggle.setFixedWidth(38)
        self.panel_toggle.setToolTip(
            "Bearbeiten: die Bearbeitungsspalte ein-/ausblenden.\n"
            "Bei einem Bild, das nur auf dem Server liegt, holt Wimmich\n"
            "dafür zuerst das Original.")
        self.panel_toggle.toggled.connect(self._toggle_panel_column)
        header_layout.addWidget(self.panel_toggle)

        loupe_column = QWidget()
        column_layout = QVBoxLayout(loupe_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(0)
        column_layout.addWidget(self.loupe_header)
        column_layout.addWidget(self.crop_bar)
        column_layout.addWidget(loupe, 1)

        self.pages.addWidget(loupe_column)       # Seite 1
        splitter.addWidget(self.pages)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1000])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        # Fortschrittsbalken fuer den Immich-Abgleich - sonst gibt es bei
        # groesseren Bibliotheken lange keine sichtbare Regung. Mit Text
        # (Prozent), damit auch ein unbewegter Balken zeigt, wo es steht.
        self.sync_progress_bar = QProgressBar()
        self.sync_progress_bar.setFixedWidth(220)
        self.sync_progress_bar.setFixedHeight(18)
        self.sync_progress_bar.setTextVisible(True)
        self.sync_progress_bar.setVisible(False)

        # Eigenes Etikett statt showMessage(): eine Zeitmeldung waere von
        # jeder anderen Meldung ueberschrieben worden und nach Ablauf
        # verschwunden - der Stand des Abgleichs soll aber stehen bleiben.
        self.sync_label = QLabel("")
        self.sync_label.setVisible(False)
        self.statusBar().addPermanentWidget(self.sync_label)
        self.statusBar().addPermanentWidget(self.sync_progress_bar)

        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)

    def _build_actions(self) -> None:
        """Menüleiste und eine schlanke Werkzeugleiste.

        Alles, was man selten braucht, gehört ins Menü - so wie unter
        Windows üblich. In der Werkzeugleiste bleiben nur die drei
        Handgriffe, die im Alltag ständig vorkommen.
        """
        scan_action = QAction("Neu einlesen", self)
        scan_action.setShortcut(QKeySequence("F5"))
        scan_action.triggered.connect(self._rescan)

        immich_action = QAction("Immich abgleichen", self)
        immich_action.setShortcut(QKeySequence("F6"))
        immich_action.triggered.connect(self._start_sync)

        self.loupe_action = QAction("Lupe / Raster", self)
        self.loupe_action.setShortcut(QKeySequence("E"))
        self.loupe_action.triggered.connect(self._toggle_loupe)

        diag_action = QAction("Diagnose …", self)
        diag_action.triggered.connect(self._show_diagnose)

        keys_action = QAction("Tastenkürzel", self)
        keys_action.setShortcut(QKeySequence("F1"))
        keys_action.triggered.connect(self._show_keys)

        settings_action = QAction("Einstellungen …", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._open_settings)

        clear_action = QAction("Vorschau-Cache leeren", self)
        clear_action.triggered.connect(self._clear_cache)

        fehler_action = QAction("Fehlerprotokoll anzeigen", self)
        fehler_action.triggered.connect(self._show_error_log)

        beenden_action = QAction("Beenden", self)
        beenden_action.setShortcut(QKeySequence("Ctrl+Q"))
        beenden_action.triggered.connect(self.close)

        vollbild_action = QAction("Vollbild", self)
        vollbild_action.setShortcut(QKeySequence("F"))
        vollbild_action.triggered.connect(self._toggle_fullscreen)

        leisten_action = QAction("Leisten ausblenden", self)
        leisten_action.setShortcut(QKeySequence("Tab"))
        leisten_action.triggered.connect(
            lambda: self._set_chrome(not self._chrome_visible))

        baum_auf = QAction("Ordnerbaum ganz aufklappen", self)
        baum_auf.triggered.connect(self._expand_all_folders)
        baum_zu = QAction("Ordnerbaum zuklappen", self)
        baum_zu.triggered.connect(self._collapse_all_folders)

        ueber_action = QAction("Über Wimmich", self)
        ueber_action.triggered.connect(self._show_about)

        leiste = self.menuBar()
        menu_datei = leiste.addMenu("&Datei")
        menu_datei.addAction(scan_action)
        menu_datei.addSeparator()
        menu_datei.addAction(settings_action)
        menu_datei.addSeparator()
        menu_datei.addAction(beenden_action)

        menu_ansicht = leiste.addMenu("&Ansicht")
        menu_ansicht.addAction(self.loupe_action)
        menu_ansicht.addAction(vollbild_action)
        menu_ansicht.addAction(leisten_action)

        # Bearbeitungsspalte: derselbe Schalter wie der Stift in der
        # Lupenkopfzeile, damit beide immer dasselbe zeigen.
        self.panel_action = QAction("Bearbeitungsspalte  ✎", self)
        self.panel_action.setCheckable(True)
        self.panel_action.setChecked(bool(self.config["edit_panel"]))
        self.panel_action.setShortcut("Ctrl+E")
        self.panel_action.toggled.connect(self.panel_toggle.setChecked)
        menu_ansicht.addAction(self.panel_action)
        self.addAction(self.panel_action)
        menu_ansicht.addSeparator()

        # Gruppierung in „Alle Fotos" und „Nur auf dem Server"
        menu_gruppe = menu_ansicht.addMenu("Gruppieren nach")
        self._gruppe_actions: dict[str, QAction] = {}
        for schluessel, text in (("month", "Monat"), ("day", "Tag"),
                                 ("none", "gar nicht")):
            aktion = QAction(text, self)
            aktion.setCheckable(True)
            aktion.setChecked(str(self.config["group_by"]) == schluessel)
            aktion.triggered.connect(
                lambda _c=False, s=schluessel: self._set_gruppierung(s))
            menu_gruppe.addAction(aktion)
            self._gruppe_actions[schluessel] = aktion

        # Was auf der Kachel steht - alles einzeln abschaltbar
        menu_kachel = menu_ansicht.addMenu("Kacheln")
        for name, text in (("packed", "Dicht an dicht"),
                           ("filenames", "Dateinamen"),
                           ("stars", "Bewertung"),
                           ("labels", "Farbmarkierung"),
                           ("stack", "RAW+JPG-Abzeichen")):
            aktion = QAction(text, self)
            aktion.setCheckable(True)
            aktion.setChecked(bool(self.config[_KACHEL_KEYS[name]]))
            aktion.toggled.connect(
                lambda an, n=name: self._set_kachel_option(n, an))
            menu_kachel.addAction(aktion)

        self.jahr_action = QAction("Jahresleiste rechts", self)
        self.jahr_action.setCheckable(True)
        self.jahr_action.setChecked(bool(self.config["year_bar"]))
        self.jahr_action.toggled.connect(self._set_jahresleiste)
        menu_ansicht.addAction(self.jahr_action)

        menu_ansicht.addSeparator()
        menu_ansicht.addAction(baum_auf)
        menu_ansicht.addAction(baum_zu)

        menu_immich = leiste.addMenu("&Immich")
        menu_immich.addAction(immich_action)
        menu_immich.addSeparator()
        menu_immich.addAction("Neues Album …", self._album_neu)

        menu_extras = leiste.addMenu("E&xtras")
        menu_extras.addAction(clear_action)
        menu_extras.addAction(diag_action)
        menu_extras.addAction(fehler_action)

        menu_hilfe = leiste.addMenu("&Hilfe")
        menu_hilfe.addAction(keys_action)
        menu_hilfe.addSeparator()
        menu_hilfe.addAction(ueber_action)

        # Keine Werkzeugleiste mehr: seit 0.3.24 steht alles im Menue,
        # und die drei verbliebenen Knoepfe haben nur Platz gekostet.
        # Die Tastenkuerzel (F5, F6, E/G) bleiben unveraendert.

        # WICHTIG: im Vollbild wird die Menueleiste ausgeblendet - und
        # eine versteckte Menueleiste schaltet ihre Tastenkuerzel ab.
        # Nachgemessen: F5 loeste danach nicht mehr aus. Deshalb haengen
        # die Aktionen zusaetzlich am Fenster selbst.
        for aktion in (scan_action, immich_action, self.loupe_action,
                       keys_action, settings_action, beenden_action,
                       vollbild_action, leisten_action, diag_action,
                       clear_action, fehler_action):
            self.addAction(aktion)

    def _first_run_hint(self) -> None:
        QMessageBox.information(
            self, APP_NAME,
            "Noch kein Bibliotheksordner eingerichtet.\n\n"
            "Über 'Ordner hinzufügen' einen Fotoordner wählen. "
            "Wimmich liest ihn nur - es wird nichts verschoben oder umbenannt.",
        )

    def _add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Fotoordner wählen")
        if not folder:
            return
        if self.config.add_library(folder):
            self._reload_folder_tree()
            self._apply_watch_settings()
            self._sync_after_scan = bool(self.config["immich_auto"])
            self._rescan()

    def _reload_folder_tree(self) -> None:
        """Baut den Seitenbaum: Ordner, Alben, Personen."""
        self.tree.clear()

        self._all_item = QTreeWidgetItem(["Alle Fotos"])
        self._all_item.setData(0, Qt.ItemDataRole.UserRole, ("all", ""))
        self._all_item.setToolTip(
            0, "Alle Ordner untereinander, wie in Picasa")
        self.tree.addTopLevelItem(self._all_item)

        self._remote_item = QTreeWidgetItem(["Nur auf dem Server"])
        self._remote_item.setData(0, Qt.ItemDataRole.UserRole, ("remote", ""))
        self._remote_item.setToolTip(
            0, "Bilder, die auf Immich liegen, aber nicht in deinen Ordnern.\n"
               "Vorschau kommt vom Server; das Original wird nur auf Wunsch geholt.")
        self.tree.addTopLevelItem(self._remote_item)

        self._folders_root = QTreeWidgetItem(["Ordner"])
        self._folders_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(self._folders_root)
        for root in self.config.libraries:
            item = QTreeWidgetItem([Path(root).name or root])
            item.setData(0, Qt.ItemDataRole.UserRole, ("folder", root))
            item.setToolTip(0, root)
            self._folders_root.addChild(item)
            self._add_children(item, root)
        self._folders_root.setExpanded(True)

        self._albums_root = QTreeWidgetItem(["Alben"])
        self._albums_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(self._albums_root)

        self._people_root = QTreeWidgetItem(["Personen"])
        self._people_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(self._people_root)

        self._reload_immich_tree()

    def _reload_immich_tree(self) -> None:
        """Alben und Personen aus dem lokalen Spiegel nachtragen."""
        # Vor dem Abraeumen merken: takeChildren() loescht den gerade
        # gewaehlten Eintrag, danach ist er nicht mehr zu finden.
        gewaehlt = self._selection
        for root, rows, kind in (
            (self._albums_root, self.db.albums(), "album"),
            (self._people_root, self.db.people(), "person"),
        ):
            root.takeChildren()
            for row in rows:
                name = row["name"] or "(ohne Namen)"
                if kind == "album":
                    label = f"{name}  ({self.db.album_count(row['id'])})"
                else:
                    label = name
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, (kind, row["id"]))
                if kind == "album" and not row["immich_id"]:
                    # Noch nicht auf dem Server - beim Abgleich geht es hin
                    item.setToolTip(0, "Nur in Wimmich; wird beim nächsten "
                                       "Immich-Abgleich angelegt")
                    item.setText(0, label + "  \u2022")
                root.addChild(item)
            if not rows:
                hint = QTreeWidgetItem(
                    ["— noch keins (Rechtsklick: Neues Album) —"]
                    if kind == "album" else ["— noch nicht abgeglichen —"])
                hint.setData(0, Qt.ItemDataRole.UserRole, None)
                hint.setFlags(Qt.ItemFlag.NoItemFlags)
                root.addChild(hint)
            root.setExpanded(bool(rows))
        self._auswahl_wiederherstellen(gewaehlt)

    def _auswahl_wiederherstellen(self, gewaehlt) -> None:
        """Nach dem Neuaufbau wieder auf denselben Eintrag stellen.

        _reload_immich_tree() wirft die Baumeintraege weg und legt sie neu
        an - damit verliert der Baum seine Auswahl und die Ansicht fiel
        auf „Alle Fotos" zurueck. Beim Zuordnen eines Bildes sprang
        Wimmich dadurch mitten in der Arbeit aus dem Album heraus.
        Gemessen: nach dem Entfernen standen 5 statt 3 Bildern da.
        """
        if not gewaehlt or gewaehlt[0] not in ("album", "person"):
            return
        wurzel = (self._albums_root if gewaehlt[0] == "album"
                  else self._people_root)
        for i in range(wurzel.childCount()):
            item = wurzel.child(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == gewaehlt:
                self._selection = gewaehlt
                gesperrt = self.tree.blockSignals(True)
                self.tree.setCurrentItem(item)
                self.tree.blockSignals(gesperrt)
                return

    def _add_children(self, parent: QTreeWidgetItem, path: str) -> None:
        """Legt die Unterordner an, aber nur eine Ebene tief (lazy)."""
        parent.takeChildren()
        try:
            entries = sorted(
                (p for p in Path(path).iterdir()
                 if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return
        for entry in entries:
            ausgeschlossen = self.config.is_excluded(str(entry))
            label = f"{entry.name}  (ausgeschlossen)" if ausgeschlossen else entry.name
            child = QTreeWidgetItem([label])
            child.setData(0, Qt.ItemDataRole.UserRole, ("folder", str(entry)))
            child.setToolTip(0, str(entry))
            if ausgeschlossen:
                child.setForeground(0, QColor(theme.TEXT_MUTED))
            # Platzhalter fuer den Aufklapppfeil - aber nur, wenn es
            # wirklich Unterordner gibt, sonst zeigt Qt einen Pfeil ins
            # Leere.
            if _has_subfolders(entry):
                child.addChild(QTreeWidgetItem(["..."]))
            parent.addChild(child)

    def _expand_item(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "folder":
            return
        if item.childCount() == 1 and item.child(0).text(0) == "...":
            self._add_children(item, data[1])

    def _expand_all_folders(self) -> None:
        """Klappt den ganzen Ordnerbaum auf - laedt fehlende Ebenen nach.

        Bei tiefen Baeumen auf langsamen Laufwerken (Netzwerkfreigabe,
        externe Platte) kostet das spuerbar Zeit, weil jede noch nicht
        geladene Ebene einmal von der Platte gelesen wird. Gemessen auf
        lokaler Platte: 44 ms fuer 590 Ordner. Sanduhr deshalb als
        Rueckmeldung, damit es nicht wie ein Haenger wirkt.
        """
        def rekursiv(item: QTreeWidgetItem) -> None:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "folder" and item.childCount() == 1 \
                    and item.child(0).text(0) == "...":
                self._add_children(item, data[1])
            if item.childCount():
                item.setExpanded(True)
            for i in range(item.childCount()):
                rekursiv(item.child(i))

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rekursiv(self._folders_root)
            self._folders_root.setExpanded(True)
        finally:
            QApplication.restoreOverrideCursor()

    def _collapse_all_folders(self) -> None:
        def rekursiv(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                rekursiv(item.child(i))
            item.setExpanded(False)

        for i in range(self._folders_root.childCount()):
            rekursiv(self._folders_root.child(i))
        self._folders_root.setExpanded(True)

    def _tree_menu(self, position) -> None:
        """Rechtsklick im Baum: Alben verwalten, Ordner aus-/einschließen."""
        menu = QMenu(self)
        item = self.tree.itemAt(position)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None

        if item is self._albums_root or (data and data[0] == "album"):
            menu.addAction("Neues Album …", self._album_neu)
            if data and data[0] == "album":
                album_id = data[1]
                menu.addSeparator()
                menu.addAction("Umbenennen …",
                               lambda: self._album_umbenennen(album_id))
                menu.addAction("Album löschen …",
                               lambda: self._album_loeschen(album_id))
            menu.addSeparator()

        menu.addAction("Baum ganz aufklappen", self._expand_all_folders)
        menu.addAction("Baum wieder zuklappen", self._collapse_all_folders)

        if data and data[0] == "folder":
            folder = data[1]
            menu.addSeparator()
            if self.config.is_excluded(folder):
                menu.addAction("Wieder aufnehmen",
                               lambda: self._include_folder(folder))
            else:
                menu.addAction("Ordner ausschließen",
                               lambda: self._exclude_folder(folder))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    # -- Eigene Alben --------------------------------------------------

    def _album_neu(self, pfade: list[str] | None = None,
                   immich_ids: list[str] | None = None) -> str:
        """Album anlegen; wahlweise gleich mit den ausgewählten Bildern."""
        name, ok = QInputDialog.getText(self, "Neues Album", "Name des Albums:")
        if not ok or not name.strip():
            return ""
        album_id = self.db.create_album_local(name.strip())
        if pfade or immich_ids:
            self.db.album_add(album_id, pfade or [], immich_ids or [])
        self._reload_immich_tree()
        self.statusBar().showMessage(
            f"Album \u201e{name.strip()}\u201c angelegt \u2013 geht beim "
            "n\u00e4chsten Immich-Abgleich auf den Server", 8000)
        return album_id

    def _album_umbenennen(self, album_id: str) -> None:
        album = self.db.album(album_id)
        if album is None:
            return
        name, ok = QInputDialog.getText(self, "Album umbenennen",
                                        "Neuer Name:", text=album["name"])
        if not ok or not name.strip():
            return
        self.db.rename_album(album_id, name.strip())
        self._reload_immich_tree()

    def _album_loeschen(self, album_id: str) -> None:
        """Album entfernen - die Bilder bleiben in jedem Fall erhalten."""
        album = self.db.album(album_id)
        if album is None:
            return
        auf_server = bool(album["immich_id"])
        text = (f"Album \u201e{album['name']}\u201c l\u00f6schen?\n\n"
                "Die Bilder bleiben unangetastet — es verschwindet nur die "
                "Zusammenstellung.")
        if auf_server:
            text += ("\n\nDas Album gibt es auch auf dem Server. Soll es dort "
                     "ebenfalls gelöscht werden?")
            antwort = QMessageBox.question(
                self, "Album löschen", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if antwort == QMessageBox.StandardButton.Cancel:
                return
            if antwort == QMessageBox.StandardButton.Yes:
                client = self._immich_verbunden()
                if client is None:
                    return
                try:
                    client.delete_album(album["immich_id"])
                except ImmichError as exc:
                    QMessageBox.critical(self, "Nicht gelöscht", str(exc))
                    return
            else:
                QMessageBox.information(
                    self, "Hinweis",
                    "Das Album bleibt auf dem Server und taucht beim nächsten "
                    "Abgleich wieder auf.")
        else:
            antwort = QMessageBox.question(
                self, "Album löschen", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if antwort != QMessageBox.StandardButton.Yes:
                return

        self.db.delete_album(album_id)
        if self._selection and self._selection == ("album", album_id):
            self.tree.setCurrentItem(self._all_item)
        self._reload_immich_tree()
        self._refresh_view()

    def _album_zuordnen(self, rows: list[int], album_id: str) -> None:
        """Ausgewählte Bilder in ein Album legen."""
        pfade, kennungen = self._auswahl_schluessel(rows)
        anzahl = self.db.album_add(album_id, pfade, kennungen)
        album = self.db.album(album_id)
        self._reload_immich_tree()
        if self._selection and self._selection[0] == "album":
            self._refresh_view()
        self.statusBar().showMessage(
            f"{anzahl} Bild(er) zu \u201e{album['name'] if album else ''}"
            "\u201c hinzugef\u00fcgt", 6000)

    def _album_entfernen(self, rows: list[int], album_id: str) -> None:
        pfade, kennungen = self._auswahl_schluessel(rows)
        anzahl = self.db.album_remove(album_id, pfade, kennungen)
        self._reload_immich_tree()
        self._refresh_view()
        self.statusBar().showMessage(
            f"{anzahl} Bild(er) aus dem Album genommen — auf dem Server beim "
            "nächsten Abgleich", 6000)

    def _auswahl_schluessel(self, rows: list[int]) -> tuple[list[str], list[str]]:
        """Pfade lokaler Dateien und Kennungen reiner Serverbilder trennen."""
        pfade, kennungen = [], []
        for row in rows:
            item = self.model.row_data(row) or {}
            if item.get("_remote"):
                if item.get("immich_id"):
                    kennungen.append(item["immich_id"])
            elif item.get("path"):
                pfade.append(item["path"])
        return pfade, kennungen

    def _album_menu(self, menu: QMenu, rows: list[int]) -> None:
        """Untermenü „Zu Album hinzufügen" und ggf. „Aus Album entfernen"."""
        alben = self.db.albums()
        unter = menu.addMenu("Zu Album hinzufügen")
        unter.addAction("Neues Album …",
                        lambda: self._album_neu(*self._auswahl_schluessel(rows)))
        if alben:
            unter.addSeparator()
        for album in alben:
            album_id = album["id"]
            unter.addAction(album["name"],
                            lambda _c=False, a=album_id:
                            self._album_zuordnen(rows, a))
        if self._selection and self._selection[0] == "album":
            aktuell = self._selection[1]
            menu.addAction(f"Aus diesem Album entfernen ({len(rows)})",
                           lambda: self._album_entfernen(rows, aktuell))

    def _exclude_folder(self, folder: str) -> None:
        """Ordner samt Unterordnern übergehen.

        Die Einträge fliegen aus dem Index, die DATEIEN bleiben liegen.
        Beim nächsten Einlesen läuft der Scanner gar nicht erst hinein.
        """
        answer = QMessageBox.question(
            self, "Ordner ausschließen",
            f"{folder}\n\nDiesen Ordner samt Unterordnern übergehen?\n\n"
            "Die Einträge verschwinden aus dem Index, die Bilder auf der "
            "Platte bleiben unangetastet.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.config.exclude(folder)
        entfernt = self.db.delete_under(folder)
        if self._selection and self._selection[1].startswith(folder):
            self._selection = None
        self._reload_folder_tree()
        self._apply_watch_settings()
        self._refresh_view()
        self.statusBar().showMessage(
            f"Ausgeschlossen: {Path(folder).name} — {entfernt} Einträge "
            "aus dem Index entfernt", 6000)

    def _include_folder(self, folder: str) -> None:
        self.config.include_again(folder)
        self._reload_folder_tree()
        self._apply_watch_settings()
        self.statusBar().showMessage(
            f"Wieder aufgenommen: {Path(folder).name} — F5 liest ihn ein", 6000)

    def _folder_selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        self._selection = data
        self.search_box.clear()
        # Unterordner ergibt nur bei Ordnern Sinn
        self.recursive_button.setEnabled(data[0] == "folder")
        self._refresh_view()

    # -- Kacheln, Gruppierung, Jahresleiste ----------------------------

    def _kachel_optionen(self) -> dict:
        return {name: bool(self.config[key])
                for name, key in _KACHEL_KEYS.items()}

    def _set_kachel_option(self, name: str, an: bool) -> None:
        """Eine Beschriftung auf der Kachel ein- oder ausschalten."""
        self.config[_KACHEL_KEYS[name]] = bool(an)
        delegate = self.grid.itemDelegate()
        if isinstance(delegate, PhotoDelegate):
            delegate.setze(name, an)
        if name == "packed":
            self.grid.setSpacing(2 if an else 6)
            self.grid.setUniformItemSizes(not an)
        # Die Kachelgroesse aendert sich mit - Qt muss die Anordnung neu
        # rechnen, sonst bleiben die alten Kaesten stehen.
        self.grid.doItemsLayout()
        self.grid.viewport().update()

    def _set_gruppierung(self, schluessel: str) -> None:
        self.config["group_by"] = schluessel
        for key, aktion in self._gruppe_actions.items():
            aktion.setChecked(key == schluessel)
        self._refresh_view()

    def _set_jahresleiste(self, an: bool) -> None:
        self.config["year_bar"] = bool(an)
        self._jahresleiste_fuellen()

    def _gruppierung_fuer(self, kind: str, sortierung: str) -> str | None:
        """Wonach in dieser Ansicht Kopfzeilen gesetzt werden.

        Nach Ordner gruppiert nur „Alle Fotos"; nach Datum beide
        Datumsansichten, sobald auch danach sortiert wird.
        """
        wahl = str(self.config["group_by"] or "month")
        if kind == "all" and sortierung == "folder":
            return "folder"
        if sortierung != "taken_at" or wahl == "none":
            return None
        if kind not in ("all", "remote"):
            return None
        return "day" if wahl == "day" else "date"

    def _jahresleiste_fuellen(self) -> None:
        """Jahre der aktuellen Ansicht in die Leiste rechts schreiben."""
        if not bool(self.config["year_bar"]) or self._selection is None \
                or self.search_box.text().strip():
            self.jahresleiste.setze_jahre([])
            self.jahresleiste.setVisible(False)
            return
        kind = self._selection[0]
        if kind == "remote":
            jahre = self.db.remote_jahre()
        elif kind == "all":
            jahre = self.db.jahre(
                self.config.libraries,
                min_rating=self._current_min_rating(),
                stacked=self.stack_button.isChecked(),
                show_rejects=self.filters.show_rejects(),
                labels=self._current_labels(),
                unlabeled=self.filters.include_unlabeled())
        else:
            jahre = []
        if self.sort_desc_button.isChecked():
            jahre = list(reversed(jahre))
        self.jahresleiste.setze_jahre(jahre)
        self._jahr_der_ansicht()

    def _jahr_der_ansicht(self) -> None:
        """Welches Jahr steht gerade oben? - hebt es in der Leiste hervor."""
        if not self.jahresleiste.isVisible():
            return
        index = self.grid.indexAt(self.grid.viewport().rect().topLeft())
        row = index.row() if index.isValid() else -1
        for kandidat in (row, row + 1):
            item = self.model.row_data(kandidat) or {}
            taken = str(item.get("taken_at") or "")
            if len(taken) >= 4 and "_header" not in item:
                self.jahresleiste.setze_aktuell(taken[:4])
                return

    def _springe_zu_jahr(self, jahr: str) -> None:
        """Zum ersten Bild dieses Jahres blättern.

        Die Ansicht laedt stueckweise nach; das Ziel kann also noch gar
        nicht geholt sein. Deshalb wird nachgeschoben, bis es auftaucht
        oder nichts mehr kommt.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            geprueft = 0
            while True:
                for row in range(geprueft, self.model.rowCount()):
                    item = self.model.row_data(row) or {}
                    if "_header" in item:
                        continue
                    if str(item.get("taken_at") or "")[:4] == jahr:
                        self._go_to_jahr(row)
                        return
                geprueft = self.model.rowCount()
                if not self.model.canFetchMore():
                    break
                self.model.fetchMore()
        finally:
            QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"{jahr}: nichts gefunden", 4000)

    def _go_to_jahr(self, row: int) -> None:
        # Die Kopfzeile ueber dem ersten Bild soll mit ins Bild kommen
        ziel = row - 1 if row > 0 and self.model.is_header(row - 1) else row
        index = self.model.index(ziel, 0)
        self.grid.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtTop)
        self.grid.setCurrentIndex(self.model.index(row, 0))
        self.jahresleiste.setze_aktuell(jahr_von(self.model.row_data(row)))

    def _toggle_subfolders(self, checked: bool) -> None:
        self.config["show_subfolders"] = checked
        self._refresh_view()

    def _toggle_sort_direction(self, checked: bool) -> None:
        self.config["sort_desc"] = checked
        self._update_sort_direction_label()
        self._refresh_view()

    def _update_sort_direction_label(self) -> None:
        absteigend = self.sort_desc_button.isChecked()
        self.sort_desc_button.setText("↓" if absteigend else "↑")

    # -- Anzeige -------------------------------------------------------

    def _current_min_rating(self) -> int:
        return self.filters.min_rating()

    def _label_set(self) -> str:
        return str(self.config["label_set"] or "de")

    def _current_labels(self) -> list[str] | None:
        return self.filters.label_texts()

    def _refresh_view(self) -> None:
        query = self.search_box.text().strip()
        common = dict(
            min_rating=self._current_min_rating(),
            stacked=self.stack_button.isChecked(),
            prefer_raw=bool(self.config["prefer_raw"]),
            show_rejects=self.filters.show_rejects(),
            labels=self._current_labels(),
            unlabeled=self.filters.include_unlabeled(),
        )
        richtung = dict(desc=self.sort_desc_button.isChecked())

        if query:
            rows = self.db.search(query, **common)
        elif self._selection is None:
            rows = []
        else:
            kind, key = self._selection
            if kind == "remote":
                rows = [_remote_zeile(r) for r in self.db.remote_only(
                    order=self.sort_box.currentData(),
                    desc=self.sort_desc_button.isChecked())]
            elif kind == "all":
                rows = []          # wird weiter unten als Cursor geholt
            elif kind == "folder":
                rows = self.db.photos_in_folder(
                    key, recursive=self.recursive_button.isChecked(),
                    order=self.sort_box.currentData(), **richtung, **common,
                )
            elif kind == "album":
                # Eigene Zuordnung UND Serverspiegel - ein selbst
                # angelegtes Album zeigt seine Bilder sofort, auch wenn
                # noch nichts hochgeladen ist.
                rows = self.db.photos_in_album(
                    key, order=self.sort_box.currentData(),
                    **richtung, **common,
                )
                rows = list(rows) + [
                    _remote_zeile(r) for r in self.db.remote_in_album(key)]
            else:
                rows = self.db.photos_by_immich(
                    "person_assets", "person_id", key,
                    order=self.sort_box.currentData(), **richtung, **common,
                )
                rows = list(rows) + [
                    _remote_zeile(r) for r in
                    self.db.remote_by_link("person_assets", "person_id", key)]

        # „Alle Fotos" läuft endlos durch: die Zeilen werden stückweise
        # nachgeholt, wenn die Ansicht sie braucht. Gruppiert wird nach
        # dem, wonach auch sortiert wird - Ordner oder Monat.
        alle = bool(self._selection and self._selection[0] == "all")
        sortierung = self.sort_box.currentData()
        gruppierung = None
        if self._selection and not query:
            gruppierung = self._gruppierung_fuer(self._selection[0], sortierung)

        # Im dichten Raster ist jede Kachel anders BREIT (gleich hoch) -
        # dann darf Qt nicht mit einer Einheitsgroesse rechnen.
        self.grid.setUniformItemSizes(
            gruppierung is None and not self.config["grid_packed"])
        if alle and not query:
            cursor = self.db.all_photos_cursor(
                self.config.libraries, order=sortierung, **richtung, **common)
            self.model.set_cursor(cursor, group_by=gruppierung)
            gezeigt = self.db.count_photos(
                self.config.libraries, min_rating=common["min_rating"],
                stacked=common["stacked"], show_rejects=common["show_rejects"],
                labels=common["labels"], unlabeled=common["unlabeled"])
        else:
            self.model.set_rows(rows, group_by=gruppierung)
            gezeigt = len([r for r in rows if "_header" not in r])

        self.model.set_edited(self.db.edited_paths())
        self._jahresleiste_fuellen()
        self._gesamt = gezeigt
        if self.in_loupe:
            # Die Liste hat sich unter der Lupe verändert - zurück ins Raster
            self._show_grid()
        self._update_status(gezeigt)

    def _run_search(self) -> None:
        self._refresh_view()

    def _search_entered(self) -> None:
        """Enter in der Suchleiste: suchen und den Fokus zurückgeben,
        damit die Tastenkürzel sofort wieder greifen."""
        self._refresh_view()
        self.grid.setFocus()

    def _toggle_stacking(self, checked: bool) -> None:
        self.config["stack_raw_jpeg"] = bool(checked)
        self._refresh_view()

    def _update_status(self, shown: int | None = None) -> None:
        total, pending = self.db.counts()
        parts = [f"{total} Bilder im Index"]
        if shown is not None:
            parts.insert(0, f"{shown} angezeigt")
        if pending:
            parts.append(f"{pending} ohne Metadaten")
        if not self.exiftool.available:
            parts.append("exiftool fehlt")
        if not self.db.has_fts:
            parts.append("Volltextsuche nicht verfügbar (LIKE-Fallback)")
        if self.config["immich_url"]:
            gesamt, abgeglichen = self.db.sync_counts()
            parts.append(f"{abgeglichen}/{gesamt} mit Immich")
        else:
            parts.append("Immich nicht eingerichtet")
        self.status_label.setText("  |  ".join(parts))

    def _download_remote(self, rows: list[int]) -> None:
        """Originale vom Server holen - nur auf ausdruecklichen Wunsch.

        Die Dateien landen in einem Ordner, den Harald waehlt. Wimmich
        legt sie NUR ab; eingelesen werden sie erst beim naechsten
        Durchlauf, wenn der Ordner zur Bibliothek gehoert.
        """
        if not rows:
            return
        ziel = QFileDialog.getExistingDirectory(
            self, "Wohin sollen die Originale?",
            self.config.libraries[0] if self.config.libraries else "")
        if not ziel:
            return

        client = ImmichClient(self.config["immich_url"], self.config["immich_key"])
        try:
            client.connect()
        except ImmichError as exc:
            QMessageBox.critical(self, "Immich nicht erreichbar", str(exc))
            return

        geholt, fehler = 0, []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for nummer, row in enumerate(rows, start=1):
                item = self.model.row_data(row) or {}
                if not item.get("_remote"):
                    continue
                self.statusBar().showMessage(
                    f"Lade herunter: {item['filename']} ({nummer} von {len(rows)})")
                QApplication.processEvents()
                try:
                    daten = client.download_original(item["immich_id"])
                except ImmichError as exc:
                    fehler.append(f"{item['filename']}: {exc}")
                    continue
                if not daten:
                    fehler.append(f"{item['filename']}: nichts erhalten")
                    continue
                pfad = _freier_name(Path(ziel) / item["filename"])
                try:
                    pfad.write_bytes(daten)
                    geholt += 1
                except OSError as exc:
                    fehler.append(f"{item['filename']}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

        self.statusBar().showMessage(
            f"{geholt} Original(e) nach {ziel} geholt"
            + (f", {len(fehler)} fehlgeschlagen" if fehler else ""), 8000)
        if fehler:
            QMessageBox.warning(
                self, "Nicht alles geholt",
                "\n".join(fehler[:10]) + ("\n…" if len(fehler) > 10 else ""))

    # -- Serverbilder: bearbeiten und löschen ---------------------------

    def _immich_verbunden(self) -> ImmichClient | None:
        """Verbundener Client oder None samt Meldung an den Benutzer."""
        client = ImmichClient(self.config["immich_url"], self.config["immich_key"])
        if not client.configured:
            QMessageBox.information(self, "Immich",
                                    "Immich ist nicht eingerichtet.")
            return None
        try:
            client.connect()
        except ImmichError as exc:
            QMessageBox.critical(self, "Immich nicht erreichbar", str(exc))
            return None
        return client

    def _zielordner(self) -> str | None:
        """Wohin geholte Originale kommen - einmal wählen, dann gemerkt.

        Liegt der Ordner in keiner Bibliothek, taucht das Bild nachher
        nirgends auf. Deshalb wird angeboten, ihn aufzunehmen.
        """
        ziel = str(self.config["download_dir"] or "")
        if not ziel or not Path(ziel).is_dir():
            start = self.config.libraries[0] if self.config.libraries else ""
            ziel = QFileDialog.getExistingDirectory(
                self, "Wohin sollen geholte Originale?", start)
            if not ziel:
                return None
            self.config["download_dir"] = ziel

        if not any(str(Path(ziel)).startswith(str(Path(lib)))
                   for lib in self.config.libraries):
            antwort = QMessageBox.question(
                self, "Ordner gehört nicht zur Bibliothek",
                f"{ziel}\n\nDieser Ordner ist keine Bibliothek von Wimmich. "
                "Ohne ihn erscheint das geholte Bild in keiner Ansicht.\n\n"
                "Ordner jetzt aufnehmen?")
            if antwort == QMessageBox.StandardButton.Yes:
                self.config.add_library(ziel)
                self._reload_folder_tree()
                self._apply_watch_settings()
        return ziel

    def _original_holen(self, item: dict, client: ImmichClient) -> Path | None:
        """Ein Original vom Server holen, ablegen und einlesen."""
        ziel = self._zielordner()
        if not ziel:
            return None
        try:
            daten = client.download_original(item["immich_id"])
        except ImmichError as exc:
            QMessageBox.critical(self, "Nicht geholt", str(exc))
            return None
        if not daten:
            QMessageBox.warning(self, "Nicht geholt",
                                f"{item['filename']}: nichts erhalten")
            return None

        pfad = _freier_name(Path(ziel) / item["filename"])
        try:
            pfad.write_bytes(daten)
        except OSError as exc:
            QMessageBox.critical(self, "Nicht gespeichert", str(exc))
            return None
        self._indiziere(pfad, item["immich_id"])
        return pfad

    def _indiziere(self, pfad: Path, immich_id: str) -> None:
        """Frisch geholte Datei sofort in den Index aufnehmen.

        Ohne das müsste erst F5 laufen, bevor das Bild sichtbar wird.
        Die Immich-Kennung wird gleich mit eingetragen - dadurch fällt
        das Bild aus „Nur auf dem Server" heraus (dort steht nur, wozu
        es KEINE lokale Datei gibt) und wird beim nächsten Abgleich
        nicht erneut hochgeladen.
        """
        st = pfad.stat()
        photo_id, _neu = self.db.upsert_file(
            str(pfad), str(pfad.parent), pfad.name, pfad.suffix.lower(),
            is_raw(pfad), st.st_size, st.st_mtime)

        meta: dict = {}
        if not is_raw(pfad):
            try:
                meta = read_fast([str(pfad)]).get(str(pfad), {})
            except Exception:
                meta = {}
        if not meta and self.exiftool.available:
            try:
                meta = self.exiftool.read_batch([str(pfad)]).get(str(pfad), {})
            except Exception:
                meta = {}
        self.db.store_metadata(photo_id, meta or {})
        self.db.set_immich(photo_id, immich_id)
        self.db.commit()

    def _zeile_mit_pfad(self, pfad: str) -> int:
        """Zeilennummer einer Datei in der aktuellen Ansicht, sonst -1."""
        geprueft = 0
        while True:
            for row in range(geprueft, self.model.rowCount()):
                item = self.model.row_data(row) or {}
                if item.get("path") == pfad:
                    return row
            geprueft = self.model.rowCount()
            if not self.model.canFetchMore():
                return -1
            self.model.fetchMore()

    def _zeige_ordner(self, ordner: Path) -> None:
        """Im Baum den Knoten dieses Ordners auswählen.

        Gibt es ihn nicht (Ordner gehört zu keiner Bibliothek oder ist
        noch nicht aufgeklappt), fällt es auf „Alle Fotos" zurück - dort
        steht die Datei auf jeden Fall, sobald sie im Index ist.
        """
        gesucht = str(ordner)
        stapel = [self.tree.topLevelItem(i)
                  for i in range(self.tree.topLevelItemCount())]
        while stapel:
            item = stapel.pop()
            if item is None:
                continue
            daten = item.data(0, Qt.ItemDataRole.UserRole)
            if daten and daten[0] == "folder" and str(daten[1]) == gesucht:
                self.tree.setCurrentItem(item)   # loest _folder_selected aus
                return
            stapel.extend(item.child(i) for i in range(item.childCount()))
        self.tree.setCurrentItem(self._all_item)

    def _serverbild_bearbeiten(self, row: int) -> None:
        """Serverbild bearbeitbar machen: Original holen, dann lokal öffnen.

        Bearbeitet wird bei Wimmich immer eine DATEI - die Schrittfolge
        hängt am Pfad, und die Ausgabe rechnet in voller Auflösung neu.
        Ein Bild, das nur auf dem Server liegt, hat beides nicht. Statt
        die Vorschau zu verbiegen, wird deshalb das Original geholt,
        eingelesen und ganz normal als lokales Bild geöffnet.
        """
        item = self.model.row_data(row) or {}
        if not item.get("_remote"):
            return
        client = self._immich_verbunden()
        if client is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Hole Original: {item['filename']} …")
        try:
            pfad = self._original_holen(item, client)
        finally:
            QApplication.restoreOverrideCursor()
        if pfad is None:
            return

        self._refresh_view()
        ziel = self._zeile_mit_pfad(str(pfad))
        if ziel < 0:
            # Wir stehen noch in „Nur auf dem Server" - dort taucht die
            # frisch geholte Datei naturgemaess nicht auf. Also in den
            # Ordner wechseln, in dem sie jetzt liegt.
            self._zeige_ordner(pfad.parent)
            ziel = self._zeile_mit_pfad(str(pfad))
        self.statusBar().showMessage(
            f"{pfad.name} liegt jetzt in {pfad.parent} und ist bearbeitbar", 8000)
        if ziel >= 0:
            self._show_loupe(ziel)

    def _serverbilder_loeschen(self, rows: list[int]) -> None:
        """Bilder auf dem Immich-Server löschen - nach Rückfrage.

        Lokale Dateien rührt Wimmich weiterhin nicht an; hier geht es
        ausschließlich um Aufnahmen, die es NUR auf dem Server gibt.
        """
        eintraege = [self.model.row_data(r) or {} for r in rows]
        ids = [e["immich_id"] for e in eintraege if e.get("_remote")]
        if not ids:
            return
        namen = ", ".join(e["filename"] for e in eintraege[:5] if e.get("_remote"))
        antwort = QMessageBox.question(
            self, "Auf dem Server löschen",
            f"{len(ids)} Aufnahme(n) auf dem Immich-Server löschen?\n\n"
            f"{namen}{' …' if len(ids) > 5 else ''}\n\n"
            "Sie landen im Papierkorb von Immich und lassen sich dort "
            "zurückholen. Lokale Dateien sind nicht betroffen.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if antwort != QMessageBox.StandardButton.Yes:
            return

        client = self._immich_verbunden()
        if client is None:
            return
        try:
            client.delete_assets(ids)
        except ImmichError as exc:
            QMessageBox.critical(self, "Nicht gelöscht", str(exc))
            return
        self.db.forget_remote(ids)
        if self.in_loupe:
            self._show_grid()
        self._refresh_view()
        self.statusBar().showMessage(
            f"{len(ids)} Aufnahme(n) auf dem Server gelöscht "
            "(Papierkorb von Immich)", 8000)

    def _grid_menu(self, position) -> None:
        """Rechtsklick auf eine Kachel.

        Die Tastenkürzel sind schnell, aber unsichtbar. Dieses Menü macht
        sie auffindbar und zeigt gleich, welche Taste dahintersteckt.
        """
        index = self.grid.indexAt(position)
        if index.isValid() and index not in self.grid.selectedIndexes():
            self.grid.setCurrentIndex(index)
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        if not rows:
            return

        nur_server = [r for r in rows
                      if (self.model.row_data(r) or {}).get("_remote")]
        if nur_server and len(nur_server) == len(rows):
            # Reine Serverbilder: Bewerten und Bearbeiten geht nicht,
            # dafuer gibt es hier das Herunterladen.
            menu = QMenu(self)
            if len(nur_server) == 1:
                menu.addAction(
                    "Original holen und bearbeiten …",
                    lambda: self._serverbild_bearbeiten(nur_server[0]))
            menu.addAction(f"{len(rows)} Original(e) herunterladen …",
                           lambda: self._download_remote(nur_server))
            menu.addSeparator()
            self._album_menu(menu, rows)
            menu.addSeparator()
            menu.addAction(f"{len(rows)} Aufnahme(n) auf dem Server löschen …",
                           lambda: self._serverbilder_loeschen(nur_server))
            menu.exec(self.grid.viewport().mapToGlobal(position))
            return

        menu = self._build_grid_menu(len(rows))
        menu.addSeparator()
        self._album_menu(menu, rows)
        menu.exec(self.grid.viewport().mapToGlobal(position))

    def _build_grid_menu(self, count: int) -> QMenu:
        menu = QMenu(self)
        menu.addAction("In der Lupe öffnen …\tE", self._loupe_from_selection)
        menu.addSeparator()

        stars = menu.addMenu("Bewertung")
        for value in range(5, 0, -1):
            stars.addAction("\u2605" * value + f"\t{value}",
                            lambda _c=False, v=value: self._rate_selection(v))
        stars.addAction("keine\t0", lambda: self._rate_selection(0))
        menu.addAction("Ablehnen\tX",
                       lambda: self._rate_selection(marks.REJECT))

        colours = menu.addMenu("Farbmarkierung")
        for i, key in enumerate(marks.LABEL_KEYS):
            name = marks.label_text(i, self._label_set())
            action = colours.addAction(f"{name}\t{key}",
                                       lambda _c=False, v=i: self._label_selection(v))
            pixmap = QPixmap(12, 12)
            pixmap.fill(QColor(marks.LABEL_COLORS[i]))
            action.setIcon(QIcon(pixmap))
        colours.addSeparator()
        colours.addAction("keine Farbe\tStrg+0",
                          lambda: self._label_selection(None))
        return menu

    # -- Bewertungen ---------------------------------------------------

    def _rate_selection(self, rating: int) -> None:
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        if not rows:
            return
        self._apply_rating(rows, rating)

    def _apply_rating(self, rows: list[int], rating: int) -> None:
        failed: list[str] = []
        files = 0

        uebergangen = 0
        for row in rows:
            item = self.model.row_data(row)
            if item is None:
                continue
            if item.get("_remote"):
                # Kein lokales Gegenstueck - es gibt keine Datei und keinen
                # Sidecar, in den die Bewertung geschrieben werden koennte.
                uebergangen += 1
                continue

            # Bei einem Stapel gilt die Bewertung für alle Dateien der
            # Aufnahme - sonst hätte das NEF vier Sterne und das JPEG keine.
            targets = self._rating_targets(item)
            for target in targets:
                self.db.set_rating(target["id"], rating)
                if self.config["write_xmp"] and self.exiftool.available:
                    try:
                        self.exiftool.write_rating(target["path"], rating)
                    except (ExifToolError, OSError) as exc:
                        failed.append(f"{target['filename']}: {exc}")
                files += 1
            self.model.update_rating(row, rating)

        if failed:
            QMessageBox.warning(
                self, "Bewertung nicht geschrieben",
                "Der Index wurde aktualisiert, aber diese Dateien nicht:\n\n"
                + "\n".join(failed[:10]),
            )

        if uebergangen and uebergangen == len(rows):
            self.statusBar().showMessage(
                "Nur auf dem Server: Bewertung braucht eine lokale Datei — "
                "erst herunterladen (Rechtsklick)", 6000)
            return

        extra = f" ({files} Dateien)" if files != len(rows) else ""
        if uebergangen:
            extra += f", {uebergangen} nur auf dem Server übergangen"
        wording = ("als abgelehnt markiert" if marks.is_reject(rating)
                   else f"mit {rating} Stern(en) bewertet")
        self.statusBar().showMessage(
            f"{len(rows) - uebergangen} Aufnahme(n) {wording}{extra}", 4000
        )

    def _label_selection(self, index: int | None) -> None:
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        if rows:
            self._apply_label(rows, index)

    def _apply_label(self, rows: list[int], index: int | None) -> None:
        """Farbmarkierung setzen; index=None entfernt sie."""
        text = marks.label_text(index, self._label_set()) if index is not None else ""
        failed: list[str] = []

        for row in rows:
            item = self.model.row_data(row)
            if item is None:
                continue
            for target in self._rating_targets(item):
                self.db.set_label(target["id"], text)
                if self.config["write_xmp"] and self.exiftool.available:
                    try:
                        self.exiftool.write_marks(target["path"], label=text)
                    except (ExifToolError, OSError) as exc:
                        failed.append(f"{target['filename']}: {exc}")
            self.model.update_label(row, text)

        if failed:
            QMessageBox.warning(
                self, "Markierung nicht geschrieben",
                "Der Index wurde aktualisiert, aber diese Dateien nicht:\n\n"
                + "\n".join(failed[:10]),
            )
        wording = text or "keine Farbe"
        self.statusBar().showMessage(
            f"{len(rows)} Aufnahme(n) markiert: {wording}", 4000
        )

    def _rating_targets(self, item: dict) -> list[dict]:
        """Die Dateien, die eine Bewertung tatsächlich betrifft."""
        if int(item.get("stack_count") or 1) <= 1 or not item.get("stack_key"):
            return [item]
        members = self.db.stack_members(item["stack_key"])
        return [dict(m) for m in members] if members else [item]

    # -- Vollbild ------------------------------------------------------

    # -- Lupe und Bearbeitung ------------------------------------------

    @property
    def in_loupe(self) -> bool:
        return self.pages.currentIndex() == 1

    def _loupe_from_selection(self) -> None:
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        self._show_loupe(rows[0] if rows else 0)

    def _toggle_loupe(self) -> None:
        if self.in_loupe:
            self._show_grid()
        else:
            self._loupe_from_selection()

    def _show_grid(self) -> None:
        self._set_tool(TOOL_NONE)
        self._crop_mode = False
        self.pages.setCurrentIndex(0)
        self.panel_box.setVisible(False)
        self.loupe_header.setVisible(False)
        self.crop_bar.setVisible(False)
        self.filter_bar.setVisible(self._chrome_visible)
        self.grid.setFocus()
        if 0 <= self._loupe_row < self.model.rowCount():
            index = self.model.index(self._loupe_row, 0)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self._update_status(self._gesamt)

    def _show_loupe(self, row: int) -> None:
        if row < 0 or row >= self.model.rowCount():
            return
        self.pages.setCurrentIndex(1)
        self.panel_box.setVisible(self._chrome_visible and not self._panel_hidden)
        self.loupe_header.setVisible(self._chrome_visible)
        self.filter_bar.setVisible(False)
        self._load_loupe(row)
        self.canvas.setFocus()

    def _toggle_panel_column(self, checked: bool) -> None:
        """Blendet NUR die Bearbeitungsspalte aus - Bild und Kopfzeile bleiben.

        Steht ein reines Serverbild in der Lupe, ist „Bearbeiten“ ohne
        Datei sinnlos. Statt eine gesperrte Leiste aufzuklappen, holt
        Wimmich hier gleich das Original - das ist genau der Griff, den
        der Stift meint.
        """
        self._panel_hidden = not checked
        self.config["edit_panel"] = bool(checked)
        aktion = getattr(self, "panel_action", None)
        if aktion is not None and aktion.isChecked() != bool(checked):
            aktion.setChecked(bool(checked))
        self.panel_box.setVisible(checked and self._chrome_visible and self.in_loupe)
        if checked and self.in_loupe and self._remote_aktiv():
            self._serverbild_bearbeiten(self._loupe_row)

    def _load_remote_loupe(self, row: int, item: dict) -> None:
        """Grossansicht eines Bildes, das nur auf dem Server liegt.

        Gezeigt wird die groessere Server-Vorschau, NICHT das Original -
        das holt Wimmich nur auf ausdruecklichen Wunsch. Bearbeiten ist
        hier ausgeschaltet: es gibt keine Datei, in die etwas
        zurueckgeschrieben werden koennte.
        """
        self._loupe_row = row
        self._loupe_path = ""
        self._want_full = False
        self._show_before = False
        self._stroke = []
        self.panel.sperre(
            "Dieses Bild liegt nur auf dem Server. Bearbeiten braucht "
            "eine Datei — mit „Original holen und bearbeiten“ (oder dem "
            "Stift oben) holt Wimmich sie in deine Bibliothek.")
        self.remote_edit_button.setVisible(True)
        self.remote_delete_button.setVisible(True)
        # Reste des vorigen Bildes wegräumen. Sonst zeigen Zuschnitt,
        # Vorher/Nachher und die Regler noch auf dessen Daten - genau
        # daran sind Aktionen wie „Zuschnitt aufheben" abgestürzt.
        self._stack_edits = EditStack()
        self._loupe_small = None
        self._loupe_full = None
        self._crop_mode = False
        self.crop_bar.setVisible(False)
        self._set_tool(TOOL_NONE)

        # Reihenfolge: grosse Vorschau (Cache oder Server), sonst die
        # kleine aus dem Cache, sonst die Kachel, die schon im Fenster
        # steht. Erst wenn NICHTS davon da ist, wird gemeckert - die
        # Meldung „Vorschau vom Server nicht verfuegbar“ ueber einem
        # Bild, das man sieht, ist nur Laerm.
        kennung = item["immich_id"]
        client = getattr(self.model, "_immich_client", None)
        bild = None
        for daten in self._vorschau_quellen(client, kennung):
            versuch = previews.lade_qimage_aus_bytes(daten)
            if not versuch.isNull():
                bild = versuch
                break

        if bild is None:
            bild = self.model.kachel_bild(row)

        if bild is None or bild.isNull():
            self.canvas.clear_image()
            self.canvas.set_info_overlay("Vorschau vom Server nicht verfügbar")
            self.canvas.show_info_overlay(True)
        else:
            self.canvas.set_image(bild)

        self.loupe_title.setText(
            f'<b>{_html_escape(item["filename"])}</b>'
            f'&nbsp;&nbsp;&nbsp;<span style="color:{theme.TEXT_MUTED}">'
            f'nur auf dem Server — Vorschau, nicht das Original</span>')
        self.setWindowTitle(
            f"{APP_NAME} {__version__} — {item['filename']} (nur auf dem Server)")
        self.canvas.set_overlay("Nur auf dem Server")

    @staticmethod
    def _vorschau_quellen(client, kennung: str):
        """Vorschaudaten eines Serverbildes, beste zuerst.

        Liefert nacheinander: grosse Vorschau (Cache, sonst Server),
        kleine Vorschau aus dem Cache. Jeder Schritt darf scheitern -
        dann kommt der naechste dran.
        """
        for gross in (True, False):
            try:
                daten = remote_thumbs.fetch(client, kennung, gross=gross)
            except Exception:
                daten = None
            if not daten and not gross:
                daten = remote_thumbs.cached(kennung, gross=False)
            if daten:
                yield daten

    def _load_loupe(self, row: int) -> None:
        item = self.model.row_data(row)
        if item is None:
            return
        if item.get("_remote"):
            self._load_remote_loupe(row, item)
            return
        # Beim Stapel wird das JPEG gezeigt und bearbeitet - eine
        # RAW-Datei lässt sich nicht pixelweise verändern.
        self._loupe_row = row
        self._loupe_path = item.get("thumb_path") or item["path"]
        self.panel.sperre("")            # nach einem Serverbild wieder frei
        self.remote_edit_button.setVisible(False)
        self.remote_delete_button.setVisible(False)
        self._want_full = False
        self._show_before = False
        self._stroke = []

        self._stack_edits = EditStack.from_json(
            self.db.load_edits(self._loupe_path))
        self.panel.load(self._stack_edits.single(TONE),
                        self._stack_edits.single(FADED),
                        self._crop_text(),
                        base_kelvin=item.get("color_temp"),
                        geo=self._stack_edits.single(GEOMETRY))

        image = self.loader.request(self._loupe_path, "screen")
        if image is not None:
            self._set_source(qimage_to_array(image))
            self._render_loupe()
        else:
            self._loupe_source = None
            self._loupe_small = None
            self.canvas.clear_image()
            self.statusBar().showMessage("Wird geladen …")
        self._prefetch_neighbours()
        self.loupe_title.setText(
            f'<b>{_html_escape(item["filename"])}</b>'
            f'&nbsp;&nbsp;&nbsp;<span style="color:{theme.TEXT_MUTED}">'
            f'{_html_escape(item.get("folder") or "")}</span>'
        )
        self.setWindowTitle(
            f"{APP_NAME} {__version__} — {item['filename']}  "
            f"({row + 1}/{self.model.rowCount()})"
        )

    def _prefetch_neighbours(self) -> None:
        paths = []
        for offset in (1, 2, -1, -2):
            other = self.model.row_data(self._loupe_row + offset)
            if other:
                paths.append(other.get("thumb_path") or other["path"])
        self.loader.prefetch(paths, "screen")

    def _image_arrived(self, path: str, level: str, image) -> None:
        if path != self._loupe_path or not self.in_loupe:
            return
        self._set_source(qimage_to_array(image))
        self._render_loupe(keep_view=(level == "full"))

    def _render_loupe(self, keep_view: bool = False, fast: bool = False) -> None:
        """Bild neu rechnen und anzeigen.

        fast=True rechnet auf der verkleinerten Fassung - das ist beim
        Ziehen am Regler der Unterschied zwischen rund 45 ms und knapp
        einer Sekunde. Die scharfe Fassung zieht _render_sharp nach.
        """
        source = self._loupe_small if fast else self._sharp_source()
        if source is None:
            return
        # Im Zuschnitt-Modus wird das GANZE Bild gezeigt und der Rahmen
        # darübergelegt; sobald der Modus aus ist, wirkt der Zuschnitt
        # auch in der Ansicht. Vorher/Nachher zeigt immer das Ganze.
        cropping = self._crop_mode or self._show_before
        if self._show_before:
            result = source
        else:
            result = self._stack_edits.apply(source, with_crop=not cropping)
        self.canvas.set_image(array_to_qimage(result), keep_view=keep_view)

        crop_step = self._stack_edits.single(CROP)
        self.canvas.set_crop(
            (crop_step.x, crop_step.y, crop_step.width, crop_step.height)
            if (crop_step is not None and self._crop_mode) else None
        )
        self.panel.set_crop_text(self._crop_text())
        self._update_overlay()

    def _render_soon(self) -> None:
        """Sofort grob zeigen, gleich darauf scharf nachziehen."""
        self._render_loupe(keep_view=True, fast=True)
        self._render_delay.start()
        self._save_delay.start()

    def _render_sharp(self) -> None:
        if self.in_loupe and self._loupe_source is not None:
            self._render_loupe(keep_view=True, fast=False)

    def _sharp_source(self):
        """Fassung für die scharfe Darstellung.

        Im eingepassten Zustand reicht die Größe der Anzeigefläche - ein
        2560 px breites Feld auf einer 1000 px breiten Fläche zu rechnen
        kostet das Sechsfache und sieht gleich aus. Erst beim Zoomen wird
        das ganze Feld gebraucht.
        """
        if self._loupe_source is None:
            return None
        if not self.canvas.is_fit:
            return self._loupe_source
        viewport = self.canvas.viewport()
        needed = int(max(viewport.width(), viewport.height()) * 1.15)
        return downscale(self._loupe_source, max(needed, PREVIEW_EDGE))

    def _crop_text(self) -> str:
        step = self._stack_edits.single(CROP)
        if step is None:
            return "Zuschnitt: keiner"
        item = self.model.row_data(self._loupe_row) or {}
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if width and height:
            return (f"Zuschnitt: {int(step.width * width)}×"
                    f"{int(step.height * height)} px")
        return f"Zuschnitt: {step.width * 100:.0f} × {step.height * 100:.0f} %"

    def _set_source(self, array) -> None:
        self._loupe_source = array
        self._loupe_small = downscale(array, PREVIEW_EDGE)

    def _need_full(self) -> None:
        if self._want_full or not self._loupe_path:
            return
        self._want_full = True
        image = self.loader.request(self._loupe_path, "full")
        if image is not None:
            self._set_source(qimage_to_array(image))
            self._render_loupe(keep_view=True)

    def _step_loupe(self, delta: int) -> None:
        self._save_edits()
        new_row = self._loupe_row + delta
        if new_row < 0:
            return
        # In der Lupe wird Bild für Bild geblättert; liegt das nächste
        # jenseits des bisher Geholten, erst nachladen.
        while new_row >= self.model.rowCount() and self.model.canFetchMore():
            self.model.fetchMore()
        if new_row >= self.model.rowCount():
            return
        if self.model.is_header(new_row):
            new_row += 1 if delta > 0 else -1
            while new_row >= self.model.rowCount() and self.model.canFetchMore():
                self.model.fetchMore()
        if 0 <= new_row < self.model.rowCount():
            self._load_loupe(new_row)

    def _update_overlay(self) -> None:
        item = self.model.row_data(self._loupe_row)
        if item is None or not self.in_loupe:
            return
        rating = int(item.get("rating") or 0)
        if marks.is_reject(rating):
            parts = ['<span style="color:#ff6b6b">✕ abgelehnt</span>']
        elif rating > 0:
            parts = [f'<span style="color:{theme.STAR}">{"★" * rating}</span>']
        else:
            parts = ['<span style="color:#888">ohne Bewertung</span>']

        colour = marks.label_color(item.get("label"))
        if colour:
            # Breiter Farbbalken statt Pünktchen - im Vollbild soll die
            # Markierung von der anderen Zimmerseite erkennbar sein.
            parts.append(
                f'<span style="background:{colour}; color:{colour}">'
                f'&nbsp;&nbsp;&nbsp;&nbsp;</span> '
                f'<span style="color:{colour}">{item.get("label")}</span>'
            )
        if not self.canvas.is_fit:
            parts.append(
                f'<span style="color:#aaa">'
                f'{self.canvas.zoom_factor() * 100:.0f}\u2009%</span>'
            )
        mode = ("Ziffern = Sterne" if self._number_mode == "rating"
                else "Ziffern = Farben")
        parts.append(f'<span style="color:#777">[M] {mode}</span>')
        self.canvas.set_overlay("&nbsp;&nbsp;".join(parts))
        self.canvas.show_overlay(True)

        if self._info_visible:
            self.canvas.set_info_overlay(_info_html(item))

    # -- Vollbild und Leisten ------------------------------------------

    def _set_chrome(self, visible: bool) -> None:
        """Leisten, Baum und Panel ein- oder ausblenden.

        Im Vollbild und mit Tab bleibt nur das Bild stehen - alles, was
        nicht das Foto ist, verschwindet. Dazu gehoert seit 0.3.30 auch
        die MENUELEISTE: sie blieb bisher stehen und hat im Vollbild
        24 px ueber dem Bild belegt. Die Tastenkuerzel ueberleben das,
        weil die Aktionen zusaetzlich am Fenster haengen (_build_actions).

        Die Filterleiste gehoert zum Raster; in der Lupe sucht und
        sortiert niemand, sie waere dort nur Rand ueber dem Bild.
        """
        self._chrome_visible = visible
        self.menuBar().setVisible(visible)
        self.filter_bar.setVisible(visible and not self.in_loupe)
        self.tree.setVisible(visible)
        self.statusBar().setVisible(visible)
        self.loupe_header.setVisible(visible and self.in_loupe)
        self.panel_box.setVisible(visible and self.in_loupe and not self._panel_hidden)
        self.crop_bar.setVisible(visible and self._crop_mode)

    def _toggle_chrome(self) -> None:
        self._set_chrome(not self._chrome_visible)
        self.statusBar().showMessage(
            "" if self._chrome_visible else "", 1)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._set_chrome(True)
        else:
            if not self.in_loupe:
                self._loupe_from_selection()
            # Vollbild heißt: nur das Bild
            self._set_chrome(False)
            self.showFullScreen()

    # -- Werkzeuge -----------------------------------------------------

    def _set_tool(self, kind: str) -> None:
        self.canvas.set_mode(kind)
        if kind != PIPETTE:
            self.panel.set_pipette_checked(False)

    def _set_pipette(self, on: bool) -> None:
        self._set_tool(PIPETTE if on else TOOL_NONE)
        self.panel.set_pipette_checked(on)
        if on:
            self.canvas.set_info_overlay(
                "Auf eine neutrale graue oder weiße Stelle klicken — W beendet")
            self.canvas.show_info_overlay(True)
        elif not self._info_visible:
            self.canvas.show_info_overlay(False)

    def _pipette_picked(self, x: float, y: float) -> None:
        """Pipettenklick.

        Zwei Fallstricke, an denen es vorher scheiterte:

        1. Der Klick kommt in Koordinaten der ANGEZEIGTEN Fassung. Die
           ist beim Reglerziehen verkleinert und kann zugeschnitten sein
           - wer damit direkt in die Quelle greift, misst an völlig
           anderer Stelle.
        2. Gemessen werden muss auf dem Stand VOR den Grundeinstellungen,
           aber NACH dem Auffrischen - denn genau dort setzen Wärme und
           Tint an.
        """
        if self._loupe_source is None:
            return
        rx, ry = self._relative(x, y)
        height, width = self._loupe_source.shape[:2]
        sample_x = int(min(max(rx, 0.0), 1.0) * (width - 1))
        sample_y = int(min(max(ry, 0.0), 1.0) * (height - 1))

        base = self._loupe_source
        faded = self._stack_edits.single(FADED)
        if faded is not None:
            base = retouch.restore_faded(base, faded.strength,
                                         faded.neutralise, faded.saturation)
        warmth, tint = retouch.white_balance_from_pixel(
            base, sample_x, sample_y)
        self.panel.set_white_balance(warmth, tint)
        self._set_pipette(False)
        self.statusBar().showMessage(
            f"Weißabgleich gesetzt: Wärme {warmth:+.2f}, Tint {tint:+.2f}", 4000)

    def _relative(self, x: float, y: float) -> tuple[float, float]:
        """Klickpunkt in Anteile des GANZEN Bildes umrechnen.

        Die Anzeige kann zugeschnitten sein - dann liegt der Nullpunkt
        nicht bei 0/0 des Originals. Retuschen sind aber immer relativ
        zum ganzen Bild gespeichert, sonst wandern sie beim Ändern des
        Zuschnitts.
        """
        size = self.canvas.image_size()
        if not size or self._loupe_source is None:
            return 0.0, 0.0
        rx, ry = x / size[0], y / size[1]

        # 1. Zuschnitt herausrechnen
        crop_step = self._stack_edits.single(CROP)
        if crop_step is not None and not self._crop_mode:
            rx = crop_step.x + rx * crop_step.width
            ry = crop_step.y + ry * crop_step.height

        # 2. Drehung und Entzerrung herausrechnen. Die Abbildung, mit der
        #    das Bild gedreht wird, zeigt schon in die richtige Richtung:
        #    von der Ausgabe zur Eingabe. Genau die wird hier benutzt.
        geo = self._stack_edits.single(GEOMETRY)
        if geo is not None:
            height, width = self._loupe_source.shape[:2]
            out_w, out_h, M = retouch.geometry_matrix(
                width, height, geo.quarters, geo.angle, geo.persp_h, geo.persp_v)
            ix, iy = retouch.map_point(M, rx * out_w, ry * out_h)
            rx, ry = ix / max(width, 1), iy / max(height, 1)
        return rx, ry

    def _masse_bekannt(self, index) -> None:
        """Kachelbreite neu rechnen, sobald die Bildmasse feststehen.

        Qt merkt von sich aus nichts davon: die Groesse kommt aus dem
        Delegate, und der wird nur neu gefragt, wenn er es selbst meldet.
        """
        delegate = self.grid.itemDelegate()
        if isinstance(delegate, PhotoDelegate) and self.config["grid_packed"]:
            delegate.sizeHintChanged.emit(index)

    def _relative_radius(self) -> float:
        """Pinselradius in Anteilen der kürzeren Kante des GANZEN Bildes."""
        size = self.canvas.image_size()
        if not size:
            return 0.02
        radius = (self._brush / 2.0) / min(size)
        crop_step = self._stack_edits.single(CROP)
        if crop_step is not None and not self._crop_mode:
            # Die Anzeige ist kleiner als das Bild - der Radius entsprechend auch
            radius *= min(crop_step.width, crop_step.height)

        # Bei einer Vierteldrehung tauschen Breite und Höhe die Rollen;
        # der Radius bezieht sich aber auf die kürzere Kante des
        # ursprünglichen Bildes. Ohne diese Umrechnung wäre der Pinsel
        # im Hochformat anders groß als im Querformat.
        geo = self._stack_edits.single(GEOMETRY)
        if geo is not None and self._loupe_source is not None:
            height, width = self._loupe_source.shape[:2]
            out_w, out_h, _M = retouch.geometry_matrix(
                width, height, geo.quarters, geo.angle, geo.persp_h, geo.persp_v)
            radius *= min(out_w, out_h) / max(min(width, height), 1)
        return radius

    def _canvas_click(self, x: float, y: float) -> None:
        if self._loupe_source is None:
            return
        rx, ry = self._relative(x, y)
        self._stack_edits.add(Step(kind=self.canvas.mode(), x=rx, y=ry,
                                   radius=self._relative_radius()))
        self._render_loupe(keep_view=True)
        self._save_edits()

    def _stroke_start(self, x: float, y: float) -> None:
        self._stroke = [(x, y)]

    def _stroke_move(self, x: float, y: float) -> None:
        if self._stroke:
            self._stroke.append((x, y))

    def _stroke_end(self) -> None:
        points, self._stroke = self._stroke, []
        if len(points) < 2 or self._loupe_source is None:
            return
        self._stack_edits.add(Step(
            kind=STROKE,
            points=[list(self._relative(x, y)) for x, y in points],
            radius=self._relative_radius(),
        ))
        self._render_loupe(keep_view=True)
        self._save_edits()

    def _crop_changed(self, x: float, y: float, width: float,
                      height: float) -> None:
        if width < 0.02 or height < 0.02:
            return
        self._stack_edits.add(Step(kind=CROP, x=x, y=y,
                                   width=width, height=height))
        self._render_loupe(keep_view=True)
        self._save_edits()

    def _remote_aktiv(self) -> bool:
        """Steht gerade ein reines Serverbild in der Lupe?

        Für solche Bilder gibt es keine lokale Datei: Zuschnitt, Regler
        und Retusche haben nichts, worauf sie wirken könnten.
        """
        item = self.model.row_data(self._loupe_row) if self.in_loupe else None
        return bool(item and item.get("_remote"))

    def _clear_crop(self) -> None:
        if self._remote_aktiv():
            return
        self._stack_edits.remove_kind(CROP)
        self.canvas.set_crop(None)
        self._render_loupe()
        self._save_edits()

    def _toggle_crop_mode(self, on: bool | None = None) -> None:
        if self._remote_aktiv():
            return
        self._crop_mode = (not self._crop_mode) if on is None else bool(on)
        self._crop_aspect_key = None
        self._crop_portrait = False
        self.canvas.set_aspect(None)
        self._set_tool(CROP if self._crop_mode else TOOL_NONE)
        self.crop_bar.setVisible(self._crop_mode and self._chrome_visible)
        for button in self._aspect_buttons.values():
            button.setChecked(False)
        self._aspect_buttons[1].setChecked(self._crop_mode)
        if not self._crop_mode:
            self.statusBar().clearMessage()
        # Der Zuschnitt wirkt in der Ansicht, sobald der Modus aus ist
        self._render_loupe()

    def _flip_aspect(self) -> None:
        if self._remote_aktiv():
            return
        if self._crop_aspect_key:
            self._set_aspect(self._crop_aspect_key)

    def _set_aspect(self, digit: int) -> None:
        """Seitenverhältnis wählen.

        Wirkt AUCH auf einen bereits gezogenen Rahmen: der wird auf das
        neue Verhältnis gebracht, statt erst beim nächsten Ziehen zu
        greifen. Mittelpunkt und Fläche bleiben dabei so weit wie
        möglich erhalten.
        """
        if self._remote_aktiv():
            return
        if not self._crop_mode:
            self._toggle_crop_mode(True)
        ratio = ASPECT_PRESETS.get(digit)
        if ratio and digit == self._crop_aspect_key:
            self._crop_portrait = not self._crop_portrait
        else:
            self._crop_portrait = False
        self._crop_aspect_key = digit
        if ratio and self._crop_portrait:
            ratio = 1.0 / ratio

        self.canvas.set_aspect(ratio)
        for key, button in self._aspect_buttons.items():
            button.setChecked(key == digit)
        self.portrait_button.setEnabled(bool(ratio) and digit != 4)
        self.portrait_button.setText(
            "hoch ✓" if (self._crop_portrait and ratio) else "hoch / quer")

        if ratio:
            self._reshape_crop(ratio)

        self.statusBar().showMessage(
            f"Format: {ASPECT_NAMES[digit]}"
            + (" hoch" if self._crop_portrait and ratio else ""), 3000)

    def _reshape_crop(self, ratio: float) -> None:
        """Bestehenden Zuschnittrahmen auf ein Seitenverhältnis bringen."""
        step = self._stack_edits.single(CROP)
        size = self.canvas.image_size()
        if step is None or not size:
            return
        out_w, out_h = size

        # In Bildpunkten rechnen - das Verhältnis meint Pixel, nicht Anteile
        w = step.width * out_w
        h = step.height * out_h
        cx = (step.x + step.width / 2) * out_w
        cy = (step.y + step.height / 2) * out_h

        # Fläche beibehalten, damit der Rahmen nicht springt
        flaeche = max(w * h, 1.0)
        neu_w = (flaeche * ratio) ** 0.5
        neu_h = neu_w / ratio

        # Ins Bild hineinpassen, notfalls verkleinern
        faktor = min(1.0, out_w / neu_w, out_h / neu_h)
        neu_w *= faktor
        neu_h *= faktor

        x = min(max(cx - neu_w / 2, 0.0), out_w - neu_w)
        y = min(max(cy - neu_h / 2, 0.0), out_h - neu_h)

        self._stack_edits.add(Step(kind=CROP, x=x / out_w, y=y / out_h,
                                   width=neu_w / out_w, height=neu_h / out_h))
        self._render_loupe(keep_view=True)
        self._save_delay.start()

    def _tone_changed(self, step) -> None:
        self._stack_edits.remove_kind(TONE)
        if step is not None:
            self._stack_edits.add(step)
        self._render_soon()

    def _geometry_changed(self, step) -> None:
        self._stack_edits.remove_kind(GEOMETRY)
        if step is not None:
            self._stack_edits.add(step)
        # Die Bildgröße ändert sich - deshalb NICHT keep_view
        self._render_loupe(fast=True)
        self._render_delay.start()
        self._save_delay.start()

    def _rotate_quarter(self, richtung: int) -> None:
        self.panel.set_quarters(self.panel.quarters() + int(richtung))

    def _fade_changed(self, strength: float, neutralise: bool) -> None:
        self._stack_edits.remove_kind(FADED)
        if strength > 0:
            self._stack_edits.add(Step(kind=FADED, strength=strength,
                                       neutralise=neutralise, saturation=0.25))
        self._render_soon()

    def _suggest_fade(self) -> None:
        if self._loupe_source is None:
            return
        value = retouch.auto_faded_strength(self._loupe_source)
        self.panel.fade_slider.setValue(int(round(value * 100)))
        self.statusBar().showMessage(
            f"Vorschlag: {value * 100:.0f} % Auffrischen" if value
            else "Das Bild wirkt nicht verblasst.", 4000)

    def _undo_edit(self) -> None:
        removed = self._stack_edits.undo()
        if removed and removed.kind in (TONE, FADED):
            item = self.model.row_data(self._loupe_row) or {}
            self.panel.load(self._stack_edits.single(TONE),
                            self._stack_edits.single(FADED), self._crop_text(),
                            base_kelvin=item.get("color_temp"),
                        geo=self._stack_edits.single(GEOMETRY))
        self._render_loupe()
        self._save_edits()

    def _reset_edits(self) -> None:
        self._stack_edits.clear()
        self.canvas.set_crop(None)
        item = self.model.row_data(self._loupe_row) or {}
        self.panel.load(None, None, self._crop_text(),
                        base_kelvin=item.get("color_temp"),
                        geo=self._stack_edits.single(GEOMETRY))
        self._render_loupe()
        self._save_edits()

    def _toggle_before(self) -> None:
        self._show_before = not self._show_before
        self._render_loupe()
        self.statusBar().showMessage(
            "Vorher" if self._show_before else "Nachher", 2000)

    def _save_edits(self) -> None:
        if not self._loupe_path:
            return
        self.db.save_edits(self._loupe_path, self._stack_edits.to_json())
        self.model.set_edited(self.db.edited_paths())

    def _export_edited(self) -> None:
        if not self._stack_edits or not self._loupe_path:
            QMessageBox.information(self, "Nichts zu speichern",
                                    "Für dieses Bild sind keine Schritte gesetzt.")
            return
        source = Path(self._loupe_path)
        target, _ = QFileDialog.getSaveFileName(
            self, "Bild speichern",
            str(source.with_name(source.stem + "_bearbeitet.jpg")),
            "JPEG (*.jpg);;PNG (*.png)")
        if not target:
            return

        self.statusBar().showMessage("Wird in voller Auflösung gerechnet …")
        self.statusBar().repaint()
        full = decode_image(self._loupe_path, None)
        if full is None or full.isNull():
            QMessageBox.warning(self, "Fehler",
                                "Das Bild konnte nicht geladen werden.")
            return
        result = self._stack_edits.apply(qimage_to_array(full))
        if array_to_qimage(result).save(target, quality=95):
            self.statusBar().showMessage(f"Gespeichert: {Path(target).name}", 6000)
        else:
            QMessageBox.warning(self, "Fehler",
                                "Die Datei konnte nicht geschrieben werden.")

    # -- Tasten --------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802
        """Tasten aus Raster, Lupe und Baum zentral behandeln.

        Bis 0.3.29 stand diese Methode zwar da, war aber NIRGENDS
        angemeldet - installEventFilter fehlte. Folge (nachgemessen):
        die Pfeiltasten kamen nie im Fenster an. In der Lupe hat der
        QGraphicsView sie zum Scrollen verbraucht, im Raster hat die
        Liste sie fuer ihre eigene Auswahl genommen und aus Ziffern eine
        Namenssuche gemacht - „3" setzte also keine Bewertung.
        """
        if event.type() == QEvent.Type.KeyPress and self._tasten_hier(obj):
            if self._handle_key(event):
                return True
        return super().eventFilter(obj, event)

    def _tasten_hier(self, obj) -> bool:
        """Darf das Fenster diese Taste an sich ziehen?

        Nein, solange ein eigenes Fenster (Einstellungen, Meldung) offen
        ist oder in einem Eingabefeld getippt wird - sonst liesse sich
        kein Suchwort und kein Serverpfad mehr eintippen.
        """
        if QApplication.activeModalWidget() is not None:
            return False
        if isinstance(obj, QWidget) and obj.window() is not self:
            return False
        fokus = QApplication.focusWidget()
        if isinstance(fokus, (QLineEdit, QComboBox, QAbstractSpinBox)):
            return False
        if isinstance(fokus, QWidget) and fokus.window() is not self:
            return False
        return True

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._handle_key(event):
            super().keyPressEvent(event)

    def _handle_key(self, event) -> bool:
        """Tastenbelegung von Cammello.

        Bewusst identisch, damit die Handgriffe von dort hier sitzen:

          Pfeile      blättern; hoch/runter eine Rasterzeile
          Pos1/Ende   erstes/letztes Bild
          0-5         Sterne (oder Farben, siehe M)
          6-9         Rot, Gelb, Grün, Blau
          X           ablehnen
          M           Ziffern umschalten: Sterne <-> Farben
          E           Lupe
          G           Raster ein/aus
          Z           Zoom umschalten
          Strg +/-    Zoom stufenweise
          +/-         Belichtung
          W           Pipette
          C           Zuschnitt an/aus; darin 1-6, Enter, Esc, Shift+C
          F           Vollbild
          I           Bildangaben
          Strg+A/D    alles auswählen / Auswahl aufheben
          Strg+Z      Bearbeitungsschritt zurück
        """
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # In der Suchleiste hat die Tastatur Vorrang
        if self.search_box.hasFocus():
            return False

        # Zuschnitt-Modus schluckt die Tasten, die er braucht
        if self._crop_mode and self.in_loupe:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._toggle_crop_mode()
                return True
            if key == Qt.Key.Key_Escape:
                self._toggle_crop_mode()
                return True
            if key == Qt.Key.Key_C and shift:
                self._clear_crop()
                self._toggle_crop_mode()
                return True
            if Qt.Key.Key_1 <= key <= Qt.Key.Key_6:
                self._set_aspect(key - Qt.Key.Key_1 + 1)
                return True

        if key == Qt.Key.Key_C and not ctrl:
            if self.in_loupe:
                self._toggle_crop_mode()
            return True
        if key == Qt.Key.Key_R and not ctrl:
            if self.in_loupe:
                self._rotate_quarter(-1 if shift else 1)
            return True
        if key == Qt.Key.Key_W and not ctrl:
            if self.in_loupe:
                self._set_pipette(self.canvas.mode() != PIPETTE)
            return True

        # + und - ohne Steuerungstaste ändern die Belichtung,
        # MIT Steuerungstaste bleiben sie der Zoom.
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            if ctrl:
                self.canvas.zoom_step(1)
            elif self.in_loupe:
                self.panel.step_exposure(1)
            return True
        if key == Qt.Key.Key_Minus:
            if ctrl:
                self.canvas.zoom_step(-1)
            elif self.in_loupe:
                self.panel.step_exposure(-1)
            return True

        if ctrl and key == Qt.Key.Key_F:
            self.search_box.setFocus()
            self.search_box.selectAll()
            return True
        if ctrl and key == Qt.Key.Key_Z:
            if self.in_loupe:
                self._undo_edit()
            return True
        if ctrl and key == Qt.Key.Key_A:
            self.model.alles_laden()
            self.grid.selectAll()
            return True
        if ctrl and key == Qt.Key.Key_D:
            self.grid.clearSelection()
            return True
        if ctrl and key == Qt.Key.Key_0:
            self._label_here(None)
            return True

        if key == Qt.Key.Key_Right:
            self._step(1)
            return True
        if key == Qt.Key.Key_Left:
            self._step(-1)
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            step = 1 if self.in_loupe else self._grid_columns()
            self._step(step if key == Qt.Key.Key_Down else -step)
            return True
        if key == Qt.Key.Key_Home:
            self._go_to(0)
            return True
        if key == Qt.Key.Key_End:
            # Ans Ende heißt: alles holen. Bei sehr großen Beständen
            # dauert das einen Moment - dafür stimmt danach auch die
            # Bildlaufleiste.
            self.model.alles_laden()
            self._go_to(self.model.rowCount() - 1)
            return True

        if key == Qt.Key.Key_I:
            self._info_visible = not self._info_visible
            self.canvas.show_info_overlay(self._info_visible)
            self._update_overlay()
            return True
        if key == Qt.Key.Key_M:
            self._number_mode = ("color" if self._number_mode == "rating"
                                 else "rating")
            self._update_overlay()
            self.statusBar().showMessage(
                "Ziffern setzen jetzt "
                + ("Farben" if self._number_mode == "color" else "Sterne"), 3000)
            return True
        if key == Qt.Key.Key_X:
            self._rate_here(marks.REJECT)
            return True
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_5:
            number = key - Qt.Key.Key_0
            if self._number_mode == "rating":
                self._rate_here(number)
            else:
                self._label_here(number - 1 if number else None)
            return True
        if Qt.Key.Key_6 <= key <= Qt.Key.Key_9:
            self._label_here(key - Qt.Key.Key_6)
            return True

        if key == Qt.Key.Key_Z:
            if self.in_loupe:
                self._need_full()
                self.canvas.toggle_zoom()
            return True
        if key == Qt.Key.Key_E:
            if not self.in_loupe:
                self._loupe_from_selection()
            return True
        if key == Qt.Key.Key_G:
            self._toggle_loupe()
            return True
        if key == Qt.Key.Key_B:
            if self.in_loupe:
                self._toggle_before()
            return True
        if key == Qt.Key.Key_F:
            self._toggle_fullscreen()
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._toggle_chrome()
            return True
        if key == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
                self._set_chrome(True)
            elif not self._chrome_visible:
                self._set_chrome(True)
            elif self.in_loupe and not self.canvas.is_fit:
                self.canvas.fit()
            elif self.in_loupe:
                self._show_grid()
            return True
        return False

    # -- Auswahl und Blättern ------------------------------------------

    def _grid_columns(self) -> int:
        """Wie viele Kacheln nebeneinander passen - für hoch/runter."""
        delegate = self.grid.itemDelegate()
        width = delegate.sizeHint(None, None).width() if delegate else 190
        return max(1, self.grid.viewport().width() // max(width + 6, 1))

    def _current_rows(self) -> list[int]:
        if self.in_loupe:
            return [self._loupe_row] if self._loupe_row >= 0 else []
        return sorted({idx.row() for idx in self.grid.selectedIndexes()})

    def _step(self, delta: int) -> None:
        if self.in_loupe:
            self._step_loupe(delta)
            return
        rows = self._current_rows()
        current = rows[-1] if rows else -1
        self._go_to(max(0, min(self.model.rowCount() - 1, current + delta)))

    def _go_to(self, row: int) -> None:
        while row >= self.model.rowCount() and self.model.canFetchMore():
            self.model.fetchMore()
        if row < 0 or row >= self.model.rowCount():
            return
        if self.model.is_header(row):
            row = min(row + 1, self.model.rowCount() - 1)
        if self.in_loupe:
            self._save_edits()
            self._load_loupe(row)
        else:
            index = self.model.index(row, 0)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)

    def _rate_here(self, rating: int) -> None:
        rows = self._current_rows()
        if rows:
            self._apply_rating(rows, rating)
            self._update_overlay()

    def _label_here(self, index: int | None) -> None:
        rows = self._current_rows()
        if rows:
            self._apply_label(rows, index)
            self._update_overlay()

    # -- Einlesen ------------------------------------------------------

    def _show_error_log(self) -> None:
        """Fehlerprotokoll anzeigen - die Grundlage jeder Fehlermeldung."""
        pfad = crashlog.log_path()
        if not pfad.exists():
            QMessageBox.information(
                self, "Fehlerprotokoll",
                "Es gibt noch keine Einträge — bisher ist nichts schiefgegangen.")
            return
        try:
            text = pfad.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, "Fehlerprotokoll", str(exc))
            return
        # Nur das Ende zeigen: das Jüngste ist das Interessante
        letzte = text[-8000:]
        box = QMessageBox(self)
        box.setWindowTitle("Fehlerprotokoll")
        box.setText(f"Datei: {pfad}")
        box.setDetailedText(letzte)
        box.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "Über Wimmich",
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            "Lokale Fotoverwaltung mit Immich-Anbindung.<br>"
            f"{LICENSE_SHORT} — ohne jede Gewährleistung.<br><br>"
            "Copyright (C) 2026 Harald Krichel")

    def _rescan(self) -> None:
        if self._scan_thread is not None:
            self.statusBar().showMessage("Es läuft bereits ein Durchlauf", 3000)
            return
        roots = self.config.libraries
        if not roots:
            self._first_run_hint()
            return

        thread = QThread(self)
        worker = ScanWorker(DB_PATH, self.exiftool.executable,
                            excluded=self.config.excluded)
        worker.moveToThread(thread)

        self.start_scan.connect(worker.scan)
        worker.progress.connect(self._scan_progress)
        worker.finished.connect(self._scan_finished)
        worker.failed.connect(self._scan_failed)
        thread.start()

        self._scan_thread = thread
        self._scan_worker = worker
        self.statusBar().showMessage("Einlesen läuft ...")
        self.start_scan.emit(roots)

    def _scan_progress(self, message: str, count: int) -> None:
        self.statusBar().showMessage(message)

    def _scan_finished(self, found: int, removed: int) -> None:
        self._teardown_scan()
        self.statusBar().showMessage(
            f"Fertig: {found} Dateien gepruft, {removed} verschwundene Einträge entfernt",
            6000,
        )
        self._refresh_view()
        self._update_status()

        if self._sync_after_scan:
            self._sync_after_scan = False
            self._auto_sync()

    def _scan_failed(self, message: str) -> None:
        self._teardown_scan()
        QMessageBox.critical(self, "Einlesen fehlgeschlagen", message)

    def _teardown_scan(self) -> None:
        if self._scan_worker is not None:
            try:
                self.start_scan.disconnect(self._scan_worker.scan)
            except TypeError:
                pass
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(5000)
        self._scan_thread = None
        self._scan_worker = None

    # -- Sonstiges -----------------------------------------------------

    # -- Immich --------------------------------------------------------

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        removed = list(dialog.removed_folders)
        values = dialog.values()
        old_grid = int(self.config["grid_size"])
        old_theme = str(self.config["theme"] or "dunkel")

        for key, value in values.items():
            self.config[key] = value

        # Entfernte Ordner aus dem Index werfen. Die Dateien bleiben liegen.
        entfernt = 0
        for folder in removed:
            entfernt += self.db.delete_under(folder)

        if old_grid != values["grid_size"]:
            self.grid.setItemDelegate(
                PhotoDelegate(values["grid_size"], self._kachel_optionen()))

        if old_theme != values["theme"]:
            self._apply_theme(values["theme"])

        self._apply_watch_settings()
        self._apply_sync_settings()
        self._reload_folder_tree()

        # Zeigte die Ansicht gerade einen entfernten Ordner? Dann leeren.
        if (self._selection and self._selection[0] == "folder"
                and any(self._selection[1].startswith(f) for f in removed)):
            self._selection = None
        self._refresh_view()
        self._update_status()

        if removed:
            self.statusBar().showMessage(
                f"{len(removed)} Ordner entfernt, {entfernt} Einträge aus dem "
                "Index gelöscht (Dateien unangetastet)", 6000)

    # -- Laufender Abgleich --------------------------------------------

    def _apply_theme(self, name: str) -> None:
        """Wechselt live zwischen hellem und dunklem Erscheinungsbild.

        Das QSS-Stylesheet neu setzen reicht für alle normalen Widgets;
        selbst gezeichnete Flächen (Raster, Baum-Pfeile, Bildvorschau)
        lesen theme.XXX erst beim naechsten Zeichnen neu ein - deshalb
        zusaetzlich gezielt neu zeichnen lassen.
        """
        stylesheet = theme.set_theme(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet)
        self.tree.viewport().update()
        self.grid.viewport().update()
        self.canvas.viewport().update()
        self.update()

    def _apply_remote_client(self) -> None:
        """Dem Rastermodell einen Client fuer Server-Vorschauen geben.

        Ohne ihn blieben die Kacheln der reinen Serverbilder leer. Der
        Client wird NUR fuer Vorschauen benutzt; Originale holt allein
        _download_remote() auf ausdruecklichen Wunsch.
        """
        url, key = self.config["immich_url"], self.config["immich_key"]
        if url and key:
            client = ImmichClient(url, key)
            try:
                client.connect()
            except ImmichError:
                client = None       # spaeter erneut versuchen
        else:
            client = None
        self.model._immich_client = client
        self._remote_item.setHidden(client is None and not self.db.remote_count())

    def _apply_sync_settings(self) -> None:
        """Zeitgeber nach den Einstellungen an- oder abschalten."""
        auto = (bool(self.config["immich_auto"])
                and bool(self.config["immich_url"])
                and bool(self.config["immich_key"]))
        if auto:
            minutes = max(1, int(self.config["immich_interval_min"]))
            self._sync_timer.start(minutes * 60 * 1000)
        else:
            self._sync_timer.stop()

    def _apply_watch_settings(self) -> None:
        """Bibliotheksordner überwachen - oder eben nicht."""
        for group in (self._watcher.directories(), self._watcher.files()):
            if group:
                self._watcher.removePaths(group)
        if not self.config["watch_folders"]:
            return

        # Qt kann nur begrenzt viele Pfade gleichzeitig überwachen; auf
        # Windows ist die Grenze eng. Deshalb gedeckelt und nur Ordner.
        paths: list[str] = []
        for root in self.config.libraries:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            paths.append(str(root_path))
            try:
                for sub in root_path.rglob("*"):
                    if len(paths) >= 400:
                        break
                    if (sub.is_dir() and not sub.name.startswith(".")
                            and not self.config.is_excluded(str(sub))):
                        paths.append(str(sub))
            except OSError:
                continue
        if paths:
            self._watcher.addPaths(paths)

    def _folder_changed(self, path: str) -> None:
        """Meldung des Wächters - erst einmal nur merken."""
        self._dirty = True
        self._watch_delay.start()      # sammelt weitere Meldungen ein

    def _changed_settled(self) -> None:
        """Es ist Ruhe eingekehrt: neu einlesen, danach abgleichen."""
        if not self._dirty or self._scan_thread is not None:
            return
        self._dirty = False
        self._sync_quiet = False
        self._sync_after_scan = False
        self._sync_after_scan = bool(self.config["immich_auto"])
        self._rescan()

    def _auto_sync(self) -> None:
        """Regelmäßiger Durchlauf. Still: keine Fenster, keine Störung."""
        if self._sync_thread is not None or self._scan_thread is not None:
            return
        if not (self.config["immich_url"] and self.config["immich_key"]):
            return
        self._start_sync(quiet=True)

    def _start_sync(self, quiet: bool = False) -> None:
        """quiet=True: der laufende Abgleich. Meldet sich nur in der
        Statuszeile und öffnet bei Fehlern kein Fenster - sonst würde
        ein kurz nicht erreichbarer Server alle 15 Minuten stören."""
        if self._sync_thread is not None:
            if not quiet:
                self.statusBar().showMessage("Der Abgleich läuft bereits", 3000)
            return
        if not (self.config["immich_url"] and self.config["immich_key"]):
            if not quiet:
                self._open_settings()
            return
        self._sync_quiet = quiet

        thread = QThread(self)
        worker = SyncWorker(
            self.config["immich_url"], self.config["immich_key"],
            upload=bool(self.config["immich_upload"]),
            fetch_people=bool(self.config["immich_people"]),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._sync_progress)
        worker.finished.connect(self._sync_finished)
        worker.failed.connect(self._sync_failed)
        thread.start()

        self._sync_thread = thread
        self._sync_worker = worker
        self.sync_progress_bar.setRange(0, 0)   # unbestimmt, bis der erste Wert kommt
        self.sync_progress_bar.setValue(0)
        self.sync_progress_bar.setVisible(True)
        self.sync_label.setText("Abgleich mit Immich läuft …")
        self.sync_label.setVisible(True)

    def _sync_progress(self, message: str, done: int, total: int) -> None:
        self.sync_label.setText(message)
        self.sync_label.setVisible(True)
        if total > 0:
            self.sync_progress_bar.setRange(0, total)
            self.sync_progress_bar.setValue(min(done, total))
        else:
            # Kein Gesamtwert bekannt (z. B. beim Verbinden oder zwischen
            # Alben/Personen) - unbestimmter Balken statt eingefrorener Wert.
            self.sync_progress_bar.setRange(0, 0)

    def _sync_finished(self, result) -> None:
        self._teardown_sync()
        self._reload_immich_tree()
        self._refresh_view()
        self._update_status()

        parts = [f"{result.checked} geprüft"]
        if result.uploaded:
            parts.append(f"{result.uploaded} hochgeladen")
        if result.already_there:
            parts.append(f"{result.already_there} waren schon da")
        if result.skipped:
            parts.append(f"{result.skipped} übersprungen (Typ nicht angenommen)")
        if result.failed:
            parts.append(f"{result.failed} fehlgeschlagen")
        self.sync_label.setText("Abgleich fertig: " + ", ".join(parts))
        self.sync_label.setVisible(True)

        self._apply_remote_client()
        if result.errors and not self._sync_quiet:
            QMessageBox.warning(
                self, "Abgleich mit Fehlern",
                "Diese Dateien konnten nicht übertragen werden:\n\n"
                + "\n".join(result.errors[:10])
                + ("\n…" if len(result.errors) > 10 else ""),
            )
        elif result.errors:
            # Auch im stillen Modus darf ein dauerhaft scheiternder Abgleich
            # nicht spurlos bleiben - sonst laeuft er wochenlang ins Leere.
            self.statusBar().showMessage(
                f"Abgleich mit {result.failed} Fehlern: {result.errors[0]}", 15000)

    def _sync_failed(self, message: str) -> None:
        quiet = self._sync_quiet
        self._teardown_sync()
        if quiet:
            # Still weiterlaufen lassen: beim nächsten Durchlauf wird
            # erneut versucht. Nur die Statuszeile sagt Bescheid.
            self.sync_label.setText(f"Abgleich nicht möglich: {message}")
            self.sync_label.setVisible(True)
        else:
            QMessageBox.critical(self, "Abgleich fehlgeschlagen", message)
            self.statusBar().clearMessage()

    def _teardown_sync(self) -> None:
        if self._sync_thread is not None:
            self._sync_thread.quit()
            self._sync_thread.wait(5000)
        self._sync_thread = None
        self._sync_worker = None
        # Balken noch kurz stehen lassen. Ein Abgleich, bei dem schon alles
        # zugeordnet ist, ist in Sekundenbruchteilen vorbei - der Balken
        # waere sonst nur ein unsichtbares Aufblitzen, und es sieht aus,
        # als sei nie etwas passiert.
        self.sync_progress_bar.setRange(0, 1)
        self.sync_progress_bar.setValue(1)
        QTimer.singleShot(2000, lambda: self.sync_progress_bar.setVisible(
            self._sync_thread is not None))
        # Das Ergebnis bleibt STEHEN, bis der naechste Abgleich es
        # ersetzt. Frueher verschwand es nach 30 Sekunden - wer in der
        # Zeit nicht hinsah, erfuhr nie, wie der Abgleich ausgegangen ist.

    def _zeige_serverfehler(self, grund: str) -> None:
        """Ausbleibende Server-Vorschauen sichtbar machen.

        Vorher blieb die Kachel einfach grau - kein Hinweis, kein
        Logeintrag, nichts zum Nachgehen. Der Text steht in der
        Statuszeile, bis etwas anderes ihn ersetzt.
        """
        self.sync_label.setText(f"Server-Vorschau kommt nicht an — {grund}")
        self.sync_label.setVisible(True)

    def _show_diagnose(self) -> None:
        """Sagt, woran es hängt, statt raten zu lassen.

        Die drei häufigsten Ursachen für Zähigkeit lassen sich hier in
        Sekunden auseinanderhalten: fehlendes rawpy, fehlendes exiftool,
        oder schlicht ein noch nicht durchgelaufener Metadatenlauf.
        """
        import time
        from . import previews, retouch as _r
        from .exif import read_fast

        zeilen = [f"<b>Wimmich {__version__}</b><table cellpadding='4'>"]

        def zeile(name, wert, hinweis=""):
            zeilen.append(f"<tr><td>{name}</td><td><b>{wert}</b></td>"
                          f"<td style='color:#8e8e9c'>{hinweis}</td></tr>")

        zeile("rawpy", "vorhanden" if previews.HAVE_RAWPY else "FEHLT",
              "" if previews.HAVE_RAWPY else
              "RAW-Vorschauen laufen über den Notweg. "
              "pip install rawpy")
        zeile("exiftool", self.exiftool.executable or "FEHLT",
              "" if self.exiftool.available else
              "RAW-Dateien bleiben ohne Aufnahmedatum")
        zeile("OpenCV", "vorhanden" if _r.HAVE_CV2 else "nicht vorhanden",
              "nur für die Rissreparatur")

        # Qt liest JPEG über ein nachladbares Modul. Fehlt es, bleiben
        # ALLE Kacheln leer, ohne dass irgendwo ein Fehler auftaucht.
        formate = previews.qt_formate()
        zeile("Qt-Bildformate",
              ", ".join(formate) if formate else "KEINE",
              "" if previews.qt_kann_jpeg() else
              "Qt kann kein JPEG - Wimmich weicht auf Pillow aus")

        gesamt, offen = self.db.counts()
        zeile("Bilder im Index", f"{gesamt}")
        zeile("ohne Metadaten", f"{offen}",
              "F5 lässt den Rest nachlaufen" if offen else "")

        # Stand der Kacheln in der laufenden Ansicht - das ist die Frage,
        # um die es geht: erscheinen Vorschauen oder nicht?
        bildzeilen, vorhanden, leer = self.model.kachel_stand()
        server_zeilen = sum(1 for r in self.model.photo_rows()
                            if (self.model.row_data(r) or {}).get("_remote"))
        zeile("Kacheln in der Ansicht",
              f"{vorhanden} von {bildzeilen} geladen",
              (f"{server_zeilen} davon nur auf dem Server"
               if server_zeilen else "") +
              (f", {leer} leer geblieben" if leer else ""))

        # Ein echter Messwert statt Vermutung.
        #
        # ACHTUNG: Zeile 0 ist in "Alle Fotos" eine KOPFZEILE ("August
        # 2026") ohne Pfad. Bis 0.3.26 wurde genau die gemessen - die
        # Diagnose meldete "FEHLGESCHLAGEN", obwohl gar keine Datei
        # gemeint war. Gemessen wird das erste echte Bild MIT Pfad.
        zeitmessung = "keine Datei zum Messen"
        item = None
        for r in self.model.photo_rows():
            kandidat = self.model.row_data(r)
            if kandidat and (kandidat.get("thumb_path") or kandidat.get("path")):
                item = kandidat
                break
        aus_datenbank = False
        if item is None:
            # Die Ansicht kann ausschliesslich Serverbilder enthalten
            # (Album ohne hochgeladene Bilder). Dann wird trotzdem eine
            # echte lokale Datei geprueft - irgendeine aus dem Index.
            try:
                treffer = self.db.all_photos(self.config.libraries)[:1]
                if treffer:
                    item = dict(treffer[0])
                    aus_datenbank = True
            except Exception:
                item = None
        if item:
            pfad = item.get("thumb_path") or item["path"]
            t = time.perf_counter()
            try:
                read_fast([pfad])
            except Exception:
                pass
            lesen = (time.perf_counter() - t) * 1000

            t = time.perf_counter()
            bild = previews.decode(pfad, 1200)
            dekodieren = (time.perf_counter() - t) * 1000
            geglueckt = bild is not None and not bild.isNull()
            zeitmessung = (f"Metadaten {lesen:.0f} ms, "
                           f"Vorschau {dekodieren:.0f} ms")
            if not geglueckt:
                # Nicht nur MELDEN, dass es scheitert - sagen, WARUM.
                zeitmessung += " (FEHLGESCHLAGEN: " + _vorschau_grund(pfad) + ")"
            zeilen.append(
                f"<tr><td>Datei</td><td colspan='2' style='color:#8e8e9c'>"
                f"{_html_escape(pfad)}"
                + (" (aus dem Index, nicht aus der Ansicht)"
                   if aus_datenbank else "")
                + "</td></tr>")

            # Und getrennt davon: kommt die KACHEL zustande? Das ist ein
            # anderer Weg als die Grossansicht (Pillow schreibt eine
            # JPEG-Datei, Qt zeigt sie an) und kann fuer sich scheitern.
            kachel = "?"
            try:
                schluessel = thumbs.cache_key(
                    pfad,
                    item.get("thumb_mtime") or item.get("mtime") or 0.0,
                    item.get("thumb_size") or item.get("filesize") or 0,
                    self.config["thumb_size"])
                ziel = thumbs.cache_path(schluessel)
                if not ziel.exists():
                    erzeugt = thumbs.get_thumbnail(
                        pfad,
                        item.get("thumb_mtime") or item.get("mtime") or 0.0,
                        item.get("thumb_size") or item.get("filesize") or 0,
                        self.config["thumb_size"], self.exiftool)
                    ziel = erzeugt if erzeugt is not None else ziel
                if ziel is None or not Path(ziel).exists():
                    kachel = "wird nicht erzeugt"
                else:
                    pix = previews.pixmap_aus_datei(str(ziel))
                    kachel = (f"{pix.width()}×{pix.height()} aus {ziel}"
                              if not pix.isNull()
                              else f"Datei da ({Path(ziel).stat().st_size} B), "
                                   f"laesst sich aber nicht anzeigen")
            except Exception as fehler:
                kachel = f"Fehler: {fehler}"
            zeile("Kachel dieser Datei", kachel)
        zeile("erste Datei der Ansicht", zeitmessung)

        # -- Serverbilder: den Weg wirklich gehen, nicht nur beschreiben --
        if server_zeilen:
            erste = next((self.model.row_data(r) for r in self.model.photo_rows()
                          if (self.model.row_data(r) or {}).get("_remote")), None)
            client = getattr(self.model, "_immich_client", None)
            zeile("Immich-Client",
                  "vom Fenster" if client is not None else "keiner - wird bei Bedarf gebaut")
            if erste:
                t = time.perf_counter()
                try:
                    daten = remote_thumbs.fetch(client, erste["immich_id"])
                except Exception as fehler:
                    daten = None
                    remote_thumbs._letzte_meldung = f"Ausnahme: {fehler}"
                dauer = (time.perf_counter() - t) * 1000
                if daten:
                    zeile("Server-Vorschau",
                          f"{len(daten)} B in {dauer:.0f} ms", "kommt an")
                else:
                    zeile("Server-Vorschau", "KEINE",
                          _html_escape(remote_thumbs.letzte_meldung() or "ohne Angabe"))
                # Welcher der vier Wege was geantwortet hat - genau das
                # entscheidet bei einem aelteren Server.
                aktiv = client or getattr(remote_thumbs, "_ersatz_client", None)
                versuche = getattr(aktiv, "letzte_vorschauversuche", []) if aktiv else []
                for eintrag in versuche:
                    zeile("", _html_escape(eintrag))

        zeilen.append("</table>")
        box = QMessageBox(self)
        box.setWindowTitle("Diagnose")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText("".join(zeilen))
        box.exec()

    def _show_keys(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Tastenkürzel")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(KEY_HELP.format(version=__version__))
        box.exec()

    def _clear_cache(self) -> None:
        removed = thumbs.clear_cache()
        self.model.set_rows([])
        self._refresh_view()
        self.statusBar().showMessage(f"{removed} Vorschaubilder gelöscht", 4000)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._scan_worker is not None:
            self._scan_worker.cancel()
        self._sync_timer.stop()
        self._watch_delay.stop()
        if self._sync_worker is not None:
            self._sync_worker.cancel()
        self._teardown_scan()
        self._teardown_sync()
        # Laufende Kachelaufgaben abwarten: sie melden sich ueber
        # Signale zurueck: kommt die Meldung, nachdem das Fenster schon
        # abgeraeumt ist, wirft Qt „wrapped C/C++ object has been
        # deleted". Beim Beenden im Testlauf nachgestellt.
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(3000)
        self.exiftool.stop()
        self.db.close()
        super().closeEvent(event)


def _has_subfolders(path: Path) -> bool:
    """Schneller Blick, ob ein Ordner echte Unterordner hat - fuer den
    Aufklapppfeil im Baum: der soll nur erscheinen, wenn es etwas zum
    Aufklappen gibt.
    """
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir() and not entry.name.startswith("."):
                    return True
    except OSError:
        pass
    return False


def _freier_name(pfad: Path) -> Path:
    """Nie eine vorhandene Datei ueberschreiben - Wimmich loescht nichts."""
    if not pfad.exists():
        return pfad
    stamm, endung = pfad.stem, pfad.suffix
    nummer = 1
    while True:
        kandidat = pfad.with_name(f"{stamm} ({nummer}){endung}")
        if not kandidat.exists():
            return kandidat
        nummer += 1


def jahr_von(item: dict | None) -> str:
    """Jahr einer Modellzeile, leer wenn ohne Aufnahmedatum."""
    return str((item or {}).get("taken_at") or "")[:4]


def _remote_zeile(row) -> dict:
    """Serverbild als Modellzeile.

    Bekommt bewusst dieselben Schluessel wie eine lokale Zeile, damit
    Raster und Lupe nichts Besonderes wissen muessen - erkennbar ist es
    allein an _remote. path bleibt leer: es GIBT keine lokale Datei.
    """
    return {
        "id": -1,
        "_remote": True,
        "immich_id": row["immich_id"],
        "path": "",
        "filename": row["filename"] or "(ohne Namen)",
        "folder": "Nur auf dem Server",
        "taken_at": row["taken_at"],
        "width": row["width"],
        "height": row["height"],
        "rating": 0,
        "label": None,
        "stack_count": 1,
        "is_raw": 0,
        "filesize": 0,
    }


def _vorschau_grund(pfad: str) -> str:
    """Sagt, WARUM eine Vorschau nicht zustande kommt.

    Die vier moeglichen Ursachen liegen weit auseinander und verlangen
    ganz verschiedene Gegenmassnahmen: Datei weg, Datei nicht lesbar,
    Qt hat keinen Dekoder, oder der Dekoder scheitert am Inhalt.
    """
    from pathlib import Path as _Pfad
    from PyQt6.QtGui import QImageReader

    try:
        datei = _Pfad(pfad)
        if not datei.exists():
            return "Datei nicht gefunden (Laufwerk weg? Ordner verschoben?)"
        if datei.stat().st_size == 0:
            return "Datei ist 0 Bytes gross"
        with open(pfad, "rb") as f:
            f.read(16)
    except OSError as fehler:
        return f"Datei nicht lesbar: {fehler}"

    leser = QImageReader(pfad)
    leser.setDecideFormatFromContent(True)
    erkannt = bytes(leser.format()).decode("ascii", "replace") or "unbekannt"
    if not leser.canRead():
        return (f"Qt hat keinen Dekoder (erkanntes Format: {erkannt}; "
                f"{leser.errorString() or 'ohne naehere Angabe'})")
    if leser.read().isNull():
        return f"Qt-Dekoder bricht ab: {leser.errorString() or 'ohne Angabe'}"

    from . import previews as _p
    if _p._qimage_von_pillow(pfad) is None:
        return "Qt liest die Datei, Pillow nicht - Inhalt beschaedigt?"
    return "Qt liest die Datei - der Fehler liegt hinter dem Dekodieren"


def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


def _info_html(item: dict) -> str:
    rows = []
    if item.get("taken_at"):
        rows.append(item["taken_at"])
    if item.get("camera"):
        rows.append(item["camera"])
    if item.get("lens"):
        rows.append(item["lens"])
    exif_bits = []
    if item.get("focal_length"):
        exif_bits.append(f"{item['focal_length']:g} mm")
    if item.get("aperture"):
        exif_bits.append(f"f/{item['aperture']:g}")
    if item.get("exposure_time"):
        exif_bits.append(f"{item['exposure_time']}")
    if item.get("iso"):
        exif_bits.append(f"ISO {item['iso']}")
    if exif_bits:
        rows.append("  ·  ".join(exif_bits))
    if item.get("width") and item.get("height"):
        rows.append(f"{item['width']} × {item['height']} px")
    if item.get("filesize"):
        rows.append(_dateigroesse(int(item["filesize"])))
    if int(item.get("stack_count") or 1) > 1:
        rows.append(f"Stapel aus {item['stack_count']} Dateien")
    rows.append(item["filename"])
    return "<br>".join(rows)


def _dateigroesse(bytes_: int) -> str:
    """Dateigröße lesbar - KB unter 1 MB, sonst MB mit einer Nachkommastelle."""
    if bytes_ >= 1024 * 1024:
        return f"{bytes_ / (1024 * 1024):.1f} MB"
    return f"{bytes_ / 1024:.0f} KB"


KEY_HELP = """<b>Belegung wie in Cammello</b><table cellpadding="3">
<tr><td>← →</td><td>blättern</td></tr>
<tr><td>↑ ↓</td><td>eine Rasterzeile (in der Lupe ein Bild)</td></tr>
<tr><td>Pos1 / Ende</td><td>erstes / letztes Bild</td></tr>
<tr><td>0–5</td><td>Sterne</td></tr>
<tr><td>6 7 8 9</td><td>Rot, Gelb, Grün, Blau</td></tr>
<tr><td>Strg+0</td><td>Farbmarkierung entfernen</td></tr>
<tr><td>X</td><td>ablehnen</td></tr>
<tr><td>M</td><td>Ziffern umschalten: Sterne ⇄ Farben</td></tr>
<tr><td>E</td><td>Lupe</td></tr>
<tr><td>G</td><td>Raster ein/aus</td></tr>
<tr><td>Z</td><td>Zoom umschalten</td></tr>
<tr><td>Strg + / −</td><td>Zoom stufenweise</td></tr>
<tr><td>Mausrad</td><td>stufenlos zoomen (Einpassen bzw. 50 % bis 200 %)</td></tr>
<tr><td>+ / −</td><td>Belichtung</td></tr>
<tr><td>W</td><td>Pipette (Weißabgleich)</td></tr>
<tr><td>R</td><td>90° drehen (Umschalt+R andersherum)</td></tr>
<tr><td>C</td><td>Zuschnitt an/aus</td></tr>
<tr><td>&nbsp;&nbsp;darin 1–6</td><td>frei, 3:2, 4:3, 1:1, 16:9, 5:4<br>
gleiche Ziffer nochmal: hoch ⇄ quer</td></tr>
<tr><td>&nbsp;&nbsp;Enter / Esc</td><td>übernehmen / abbrechen</td></tr>
<tr><td>&nbsp;&nbsp;Shift+C</td><td>Zuschnitt aufheben</td></tr>
<tr><td>B</td><td>Vorher / Nachher</td></tr>
<tr><td>F</td><td>Vollbild — nur das Bild</td></tr>
<tr><td>Tab</td><td>Leisten und Baum ein/aus</td></tr>
<tr><td>Strg+E</td><td>Bearbeitungsspalte (✎ in der Lupe)</td></tr>
<tr><td>I</td><td>Bildangaben</td></tr>
<tr><td>Strg+A / Strg+D</td><td>alles wählen / Auswahl aufheben</td></tr>
<tr><td>Strg+Z</td><td>Bearbeitungsschritt zurück</td></tr>
<tr><td>Strg+F</td><td>in die Suchleiste; Enter führt zurück</td></tr>
<tr><td>F5 / F6</td><td>neu einlesen / Immich abgleichen</td></tr>
<tr><td>Strg+,</td><td>Einstellungen</td></tr>
</table>
<p style="color:#8e8e9c">Wimmich {version} — freie Software unter der
GNU GPL v3 oder später. Ohne jede Gewährleistung.</p>"""
