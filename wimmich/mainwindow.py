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
    QEvent, QFileSystemWatcher, QSize, Qt, QThread, QThreadPool, QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QIcon, QKeySequence, QPainter,
    QPainterPath, QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QDialog,
    QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSplitter, QStackedWidget, QStatusBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import (APP_NAME, LICENSE_SHORT, __version__, crashlog, marks,
               previews, retouch, theme, thumbs)
from .config import Config, DB_PATH, ensure_dirs, find_exiftool
from .db import Database
from .exif import ExifTool, ExifToolError, read_fast
from .immich import ImmichError
from .models import PhotoDelegate, PhotoModel
from .previews import PreviewLoader
from .canvas import CanvasView, NONE as TOOL_NONE, PIPETTE
from .edit_panel import EditPanel
from .filterbar import FilterBar
from .icon import app_icon
from .jahresleiste import Jahresleiste, monatstitel
from . import remote_thumbs
from .edits import (
    CROP, EditStack, FADED, GEOMETRY, STROKE, Step, TONE,
    array_to_qimage, downscale, qimage_to_array,
)
from .previews import decode as decode_image
from .export import ExportDialog, ExportWorker
from .masse import MasseWorker
from .serverbilder import (
    ServerbilderMixin, _ServerSignale, _VorschauSignale,
)
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

    # Halbe Kantenlaenge des Dreiecks. 0.3.45: von 3,6 auf 5,0 - der
    # alte Pfeil war kaum zu treffen und kaum zu sehen.
    PFEIL = 5.0

    def mousePressEvent(self, event) -> None:
        """Ein Klick LINKS vom Text klappt auf und zu.

        GEMESSEN 0.3.45: mit der Regel `QTreeWidget::branch` im
        Stylesheet uebernimmt Qts Stylesheet-Stil die Aufklappflaeche und
        macht sie faktisch null Pixel breit - von x=0 bis x=14 klappte
        nur x=0 auf. Deshalb wird hier selbst zugeschlagen: die ganze
        Einrueckung vor dem Text zaehlt als Pfeil. Doppelklick auf den
        Titel klappt weiterhin ueber Qts eigenen Weg auf
        (expandsOnDoubleClick).
        """
        item = self.itemAt(event.position().toPoint())
        if (item is not None and item.childCount()
                and event.position().x() < self.visualItemRect(item).x()):
            item.setExpanded(not item.isExpanded())
            event.accept()
            return
        super().mousePressEvent(event)

    def drawBranches(self, painter, rect, index) -> None:
        item = self.itemFromIndex(index)
        if item is None or item.childCount() == 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(theme.TEXT_MUTED)))

        cx, cy = rect.center().x(), rect.center().y()
        size = self.PFEIL
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

class MainWindow(ServerbilderMixin, QMainWindow):
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
        # Eigener kleiner Pool fuer Server-Vorschauen in der Lupe
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._vorschau_signale = _VorschauSignale()
        self._vorschau_signale.fertig.connect(self._vorschau_da)
        self._vorschau_signale.client_da.connect(self._client_da)
        self._server_signale = _ServerSignale()
        self._server_signale.orte_da.connect(self._orte_da)
        self._server_signale.treffer_da.connect(self._treffer_da)
        self._server_signale.person_fertig.connect(self._person_fertig)
        self._server_signale.person_bild.connect(self._person_bild)
        # Personenkennung -> QIcon; verhindert, dass jeder Neuaufbau des
        # Baums alle Gesichter erneut von der Platte liest.
        self._gesicht_cache: dict = {}
        self._server_treffer: list[str] = []
        self._such_anlass = ""
        self._remote_wartet = ""
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        self._masse_thread: QThread | None = None
        self._masse_worker: MasseWorker | None = None
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
        self._zoom_stumm = False
        self._diashow_vollbild_vorher = False
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
        self._filter_hidden = not bool(self.config["filter_bar"])
        self._gesamt = 0        # Aufnahmen in der aktuellen Ansicht
        self._scan_worker: ScanWorker | None = None

        # Dicht an dicht ist ab 0.3.33 fest eingebaut. Wer den alten
        # Schalter einmal ausgeschaltet hatte, saehe sonst weiter das
        # alte Raster.
        self.config["grid_packed"] = True

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
        self._toggle_zoomregler(bool(self.config["zoom_slider"]))
        # Die Filterleiste entsteht sichtbar - erst hier greift die
        # Vorgabe aus der Konfiguration.
        self._filterleiste_zeigen()
        self.loader.ready.connect(self._image_arrived)
        self._melde("Ordner werden gelesen …")
        self._reload_folder_tree()
        self._update_status()
        self.grid.setFocus()
        self._apply_watch_settings()
        self._apply_sync_settings()
        # Bis der Server geantwortet hat, ist die Serversuche gesperrt -
        # sie kaeme sonst ins Leere.
        self._serversuche_freigeben(False)
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

        # Linke Spalte: Suchfeld ueber dem Baum
        links = QWidget()
        links_layout = QVBoxLayout(links)
        links_layout.setContentsMargins(6, 6, 6, 0)
        links_layout.setSpacing(6)

        self.server_suche = QLineEdit()
        self.server_suche.setPlaceholderText("Auf dem Server suchen …")
        self.server_suche.setClearButtonEnabled(True)
        self.server_suche.setToolTip(
            "Sucht auf Immich in normaler Sprache: roter Teppich, "
            "Mikrofon, Schnee.\nEnter startet die Suche.")
        self.server_suche.returnPressed.connect(self._server_suche_starten)
        links_layout.addWidget(self.server_suche)

        self.tree = FolderTree()
        self.tree.setHeaderHidden(True)
        # 20 statt 14: der groessere Pfeil braucht Platz, und die
        # Trefferflaeche zum Aufklappen ist genau diese Spalte.
        self.tree.setIndentation(20)
        self.tree.setIconSize(QSize(28, 28))
        self.tree.setAnimated(True)
        self.tree.setMinimumWidth(180)
        self.tree.itemSelectionChanged.connect(self._folder_selected)
        self.tree.itemExpanded.connect(self._expand_item)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        links_layout.addWidget(self.tree, 1)
        self.links_spalte = links
        splitter.addWidget(links)

        self.model = PhotoModel(
            exiftool=self.exiftool, thumb_edge=self.config["thumb_size"]
        )
        self.model.serverfehler.connect(self._zeige_serverfehler)
        self.model.masse_bekannt.connect(self._masse_bekannt)
        self.model.masse_berichtigt.connect(self._masse_berichtigt)
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
        self.grid.doubleClicked.connect(self._grid_doppelklick)
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

        self._diashow_timer = QTimer(self)
        self._diashow_timer.timeout.connect(self._diashow_schritt)

        self._masse_berichtigt_zahl = 0
        self._masse_speichern = QTimer(self)
        self._masse_speichern.setSingleShot(True)
        self._masse_speichern.setInterval(1500)
        self._masse_speichern.timeout.connect(self._masse_sichern)
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
        self.canvas.zoom_changed.connect(self._zoom_anzeigen)
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

        # Zuschnitt: bisher nur ueber die Taste R erreichbar - unsichtbar,
        # wenn man die Belegung nicht kennt.
        self.crop_button = QPushButton("⛶")
        self.crop_button.setCheckable(True)
        self.crop_button.setFixedWidth(38)
        self.crop_button.setToolTip("Zuschneiden (R)")
        self.crop_button.toggled.connect(self._crop_button_geschaltet)
        header_layout.addWidget(self.crop_button)

        # Zoomregler: einblendbar, damit er in der Lupe keinen Platz
        # kostet, wenn er nicht gebraucht wird.
        self.zoom_toggle = QPushButton("🔍")
        self.zoom_toggle.setCheckable(True)
        self.zoom_toggle.setChecked(bool(self.config["zoom_slider"]))
        self.zoom_toggle.setFixedWidth(38)
        self.zoom_toggle.setToolTip("Zoomregler ein-/ausblenden")
        self.zoom_toggle.toggled.connect(self._toggle_zoomregler)
        header_layout.addWidget(self.zoom_toggle)

        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setFixedWidth(140)
        self.zoom_slider.setToolTip("Vergrößerung (Strg + / Strg −)")
        # In Promille gerechnet: QSlider kann nur ganze Zahlen, und
        # 50…200 % in Prozentschritten waere zu grob zum Feinstellen.
        self.zoom_slider.setRange(int(self.canvas.MIN_ZOOM * 1000),
                                  int(self.canvas.MAX_ZOOM * 1000))
        self.zoom_slider.setValue(1000)
        self.zoom_slider.valueChanged.connect(self._zoom_geregelt)
        header_layout.addWidget(self.zoom_slider)

        self.zoom_label = QLabel("100 %")
        self.zoom_label.setFixedWidth(52)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.zoom_label)

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

        # Der Leerhinweis verweist seit jeher auf „Ordner hinzufügen" -
        # die Aktion selbst hing an keinem Menü und war nicht erreichbar
        # (QK 13.08.2026).
        ordner_action = QAction("Ordner hinzufügen …", self)
        ordner_action.setToolTip(
            "Einen Fotoordner in die Bibliothek aufnehmen. Wimmich liest "
            "ihn nur - es wird nichts verschoben oder umbenannt.")
        ordner_action.triggered.connect(self._add_library)

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
        menu_datei.addAction(ordner_action)
        menu_datei.addAction(scan_action)
        menu_datei.addSeparator()
        self.export_action = QAction("Auswahl exportieren …", self)
        self.export_action.setShortcut("Ctrl+Shift+E")
        self.export_action.triggered.connect(self._export_auswahl)
        menu_datei.addAction(self.export_action)
        self.addAction(self.export_action)
        menu_datei.addSeparator()
        masse_action = QAction("Bildmaße prüfen und richtigstellen …", self)
        masse_action.setToolTip(
            "Prüft den ganzen Index gegen die Dateien und dreht falsch "
            "herum eingetragene Maße um.")
        masse_action.triggered.connect(self._masse_pruefen)
        menu_datei.addAction(masse_action)
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

        self.diashow_action = QAction("Diashow", self)
        self.diashow_action.setShortcut("F5")
        self.diashow_action.triggered.connect(self._diashow_umschalten)
        menu_ansicht.addAction(self.diashow_action)
        self.addAction(self.diashow_action)

        self.filter_action = QAction("Filterleiste", self)
        self.filter_action.setCheckable(True)
        self.filter_action.setChecked(bool(self.config["filter_bar"]))
        self.filter_action.setShortcut("Ctrl+L")
        self.filter_action.toggled.connect(self._toggle_filterleiste)
        menu_ansicht.addAction(self.filter_action)
        self.addAction(self.filter_action)
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
        for name, text in (("filenames", "Dateinamen"),
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
            0, "Alle Ordner untereinander, wie in Picasa.\n"
               "Bilder, die nur auf dem Server liegen, stehen mit darin.")
        self.tree.addTopLevelItem(self._all_item)

        anzahl = len(self.config["sammlung"]) + len(self.config["sammlung_remote"])
        self._sammlung_item = QTreeWidgetItem(
            [f"Vorläufige Sammlung ({anzahl})" if anzahl
             else "Vorläufige Sammlung"])
        self._sammlung_item.setData(0, Qt.ItemDataRole.UserRole, ("sammlung", ""))
        self._sammlung_item.setToolTip(
            0, "Von Hand zusammengetragene Bilder — zum Sichten und\n"
               "Aussortieren. Kein Album: nichts davon geht auf den Server.")
        self.tree.addTopLevelItem(self._sammlung_item)

        self._folders_root = QTreeWidgetItem(["Lokale Ordner"])
        self._folders_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(self._folders_root)
        for root in self.config.libraries:
            item = QTreeWidgetItem([Path(root).name or root])
            item.setData(0, Qt.ItemDataRole.UserRole, ("folder", root))
            item.setToolTip(0, root)
            self._folders_root.addChild(item)
            self._add_children(item, root)
        # Eingeklappt starten: bei vielen Bibliotheken war die Spalte
        # sonst gleich voll und man musste erst zuklappen, um etwas zu
        # finden. Aufklappen geht mit einem Klick.
        self._folders_root.setExpanded(False)

        self._albums_root = QTreeWidgetItem(["Alben"])
        self._albums_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self.tree.addTopLevelItem(self._albums_root)

        # „Erkunden" fasst zusammen, was der SERVER weiss: Orte und
        # Personen. Beides kommt aus Immich, beides braucht Netz -
        # deshalb steht es beieinander und nicht bei den lokalen Ordnern.
        self._explore_root = QTreeWidgetItem(["Erkunden"])
        self._explore_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self._explore_root.setToolTip(
            0, "Orte und Personen kommen von Immich — dafür muss der "
               "Server erreichbar sein.")
        self.tree.addTopLevelItem(self._explore_root)

        self._places_root = QTreeWidgetItem(["Orte"])
        self._places_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self._explore_root.addChild(self._places_root)

        self._people_root = QTreeWidgetItem(["Personen"])
        self._people_root.setData(0, Qt.ItemDataRole.UserRole, None)
        self._explore_root.addChild(self._people_root)

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
                label = (f"{name}  ({self.db.album_count(row['id'])})"
                         if kind == "album" else name)
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, (kind, row["id"]))
                if kind == "album" and not row["immich_id"]:
                    # Noch nicht auf dem Server - beim Abgleich geht es hin
                    item.setToolTip(0, "Nur in Wimmich; wird beim nächsten "
                                       "Immich-Abgleich angelegt")
                    item.setText(0, label + "  \u2022")
                root.addChild(item)
            if kind == "person":
                # Unbenannte NICHT einzeln in den Baum - sie kaemen als
                # fuenfzig Zeilen „(ohne Namen)" und waeren ohne Bild
                # nicht auseinanderzuhalten. Ein Eintrag fuehrt ins
                # RASTER, dort haben sie ihr Gesicht dabei.
                ohne = [r for r in self.db.people(include_unnamed=True)
                        if not (r["name"] or "").strip()]
                if ohne:
                    eintrag = QTreeWidgetItem([f"Ohne Namen ({len(ohne)})"])
                    eintrag.setData(0, Qt.ItemDataRole.UserRole,
                                    ("unbenannt", ""))
                    eintrag.setToolTip(
                        0, "Erkannte Gesichter ohne Namen — im Raster zum "
                           "Benennen (Rechtsklick auf die Kachel)")
                    root.addChild(eintrag)
                self._gesichter_holen()
            if not rows:
                hint = QTreeWidgetItem(
                    ["— noch keins (Rechtsklick: Neues Album) —"]
                    if kind == "album" else ["— noch nicht abgeglichen —"])
                hint.setData(0, Qt.ItemDataRole.UserRole, None)
                hint.setFlags(Qt.ItemFlag.NoItemFlags)
                root.addChild(hint)
            root.setExpanded(False)
        self._auswahl_wiederherstellen(gewaehlt)

    def _auswahl_wiederherstellen(self, gewaehlt) -> None:
        """Nach dem Neuaufbau wieder auf denselben Eintrag stellen.

        _reload_immich_tree() wirft die Baumeintraege weg und legt sie neu
        an - damit verliert der Baum seine Auswahl und die Ansicht fiel
        auf „Alle Fotos" zurueck. Beim Zuordnen eines Bildes sprang
        Wimmich dadurch mitten in der Arbeit aus dem Album heraus.
        Gemessen: nach dem Entfernen standen 5 statt 3 Bildern da.
        """
        if not gewaehlt or gewaehlt[0] not in ("album", "person", "ort"):
            return
        wurzel = {"album": self._albums_root,
                  "person": self._people_root,
                  "ort": self._places_root}.get(gewaehlt[0])
        if wurzel is None:
            return
        for i in range(wurzel.childCount()):
            item = wurzel.child(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == gewaehlt:
                self._selection = gewaehlt
                gesperrt = self.tree.blockSignals(True)
                wurzel.setExpanded(True)   # sonst bliebe die Auswahl versteckt
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

        if data and data[0] == "person":
            person_id = data[1]
            # Ohne Verbindung ausgrauen statt weglassen: die Aenderung
            # geht IMMER zuerst an den Server (Regel aus 0.3.40).
            hat_server = self.model._immich_client is not None
            umbenennen = menu.addAction(
                "Umbenennen …", lambda: self._person_umbenennen(person_id))
            zusammen = menu.addAction(
                "Andere Person hier aufgehen lassen …",
                lambda: self._person_zusammenfuehren(person_id))
            for aktion in (umbenennen, zusammen):
                aktion.setEnabled(hat_server)
                if not hat_server:
                    aktion.setToolTip("Braucht eine Verbindung zu Immich")
            menu.addSeparator()

        if data and data[0] == "sammlung":
            anzahl = self._sammlung_zahl()
            leeren = menu.addAction(f"Sammlung leeren ({anzahl}) …",
                                    self._sammlung_leeren)
            leeren.setEnabled(bool(anzahl))
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

    # -- Vorläufige Sammlung -------------------------------------------

    def _sammlung_von_auswahl(self) -> None:
        """Taste S: Auswahl aufnehmen - in der Sammlung: wieder entfernen.

        NICHT B - das ist in der Lupe vorher/nachher.
        """
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        if not rows and self.in_loupe:
            rows = [self._loupe_row]
        if not rows:
            return
        if self._selection and self._selection[0] == "sammlung":
            self._sammlung_entfernen(rows)
        else:
            self._sammlung_aufnehmen(rows)

    def _sammlung_zahl(self) -> int:
        return (len(self.config["sammlung"])
                + len(self.config["sammlung_remote"]))

    def _sammlung_beschriften(self) -> None:
        """Zähler am Baumeintrag nachziehen."""
        anzahl = self._sammlung_zahl()
        self._sammlung_item.setText(
            0, f"Vorläufige Sammlung ({anzahl})" if anzahl
            else "Vorläufige Sammlung")

    def _sammlung_aufnehmen(self, rows: list[int]) -> None:
        """Ausgewählte Bilder in die vorläufige Sammlung legen.

        Bewusst KEIN Album: nichts davon geht auf den Server, und
        Doppelte werden stillschweigend übergangen.
        """
        pfade, kennungen = self._auswahl_schluessel(rows)
        sammlung = list(self.config["sammlung"])
        remote = list(self.config["sammlung_remote"])
        neu = 0
        for pfad in pfade:
            if pfad not in sammlung:
                sammlung.append(pfad)
                neu += 1
        for kennung in kennungen:
            if kennung not in remote:
                remote.append(kennung)
                neu += 1
        self.config["sammlung"] = sammlung
        self.config["sammlung_remote"] = remote
        self._sammlung_beschriften()
        schon = len(pfade) + len(kennungen) - neu
        text = f"{neu} Aufnahme(n) in die Sammlung übernommen"
        if schon:
            text += f", {schon} war(en) schon drin"
        self.statusBar().showMessage(text, 5000)
        if self._selection and self._selection[0] == "sammlung":
            self._refresh_view()

    def _sammlung_entfernen(self, rows: list[int]) -> None:
        pfade, kennungen = self._auswahl_schluessel(rows)
        self.config["sammlung"] = [p for p in self.config["sammlung"]
                                   if p not in pfade]
        self.config["sammlung_remote"] = [
            k for k in self.config["sammlung_remote"] if k not in kennungen]
        self._sammlung_beschriften()
        self.statusBar().showMessage(
            f"{len(pfade) + len(kennungen)} Aufnahme(n) aus der Sammlung "
            "genommen", 5000)
        if self._selection and self._selection[0] == "sammlung":
            self._refresh_view()

    def _sammlung_leeren(self) -> None:
        if not self._sammlung_zahl():
            return
        antwort = QMessageBox.question(
            self, "Sammlung leeren",
            f"Die {self._sammlung_zahl()} Aufnahmen aus der Sammlung nehmen?\n"
            "Die Dateien bleiben unangetastet.")
        if antwort != QMessageBox.StandardButton.Yes:
            return
        self.config["sammlung"] = []
        self.config["sammlung_remote"] = []
        self._sammlung_beschriften()
        if self._selection and self._selection[0] == "sammlung":
            self._refresh_view()

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
        if data[0] == "ort":
            self.server_suche.clear()
            self._serversuche_ausloesen("ort", data[1])
            return          # die Ansicht kommt, wenn der Server antwortet
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
            monate = self.db.remote_monate()
        elif kind == "all":
            filter_args = dict(
                min_rating=self._current_min_rating(),
                stacked=self.stack_button.isChecked(),
                show_rejects=self.filters.show_rejects(),
                labels=self._current_labels(),
                unlabeled=self.filters.include_unlabeled())
            jahre = self.db.jahre(self.config.libraries, **filter_args)
            monate = self.db.monate(self.config.libraries, **filter_args)
        else:
            jahre, monate = [], []
        if self.sort_desc_button.isChecked():
            jahre = list(reversed(jahre))
        self.jahresleiste.setze_jahre(jahre, monate)
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
                # Monatsgenau, damit auch der Spiegelstrich mitwandert
                self.jahresleiste.setze_aktuell(taken[:7] if len(taken) >= 7
                                                else taken[:4])
                return

    def _springe_zu_jahr(self, ziel: str) -> None:
        """Zum ersten Bild dieses Jahres oder Monats blättern.

        `ziel` ist entweder ein Jahr ('2026') oder ein Monat
        ('2026-08'); verglichen wird auf so vielen Stellen, wie das
        Ziel lang ist.

        Die Ansicht laedt stueckweise nach; das Ziel kann also noch gar
        nicht geholt sein. Deshalb wird nachgeschoben, bis es auftaucht
        oder nichts mehr kommt.
        """
        stellen = len(ziel)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            geprueft = 0
            while True:
                for row in range(geprueft, self.model.rowCount()):
                    item = self.model.row_data(row) or {}
                    if "_header" in item:
                        continue
                    if str(item.get("taken_at") or "")[:stellen] == ziel:
                        self._go_to_jahr(row)
                        return
                geprueft = self.model.rowCount()
                if not self.model.canFetchMore():
                    break
                self.model.fetchMore()
        finally:
            QApplication.restoreOverrideCursor()
        name = ziel if stellen <= 4 else monatstitel(ziel)
        self.statusBar().showMessage(f"{name}: nichts gefunden", 4000)

    def _go_to_jahr(self, row: int) -> None:
        # Die Kopfzeile ueber dem ersten Bild soll mit ins Bild kommen
        ziel = row - 1 if row > 0 and self.model.is_header(row - 1) else row
        index = self.model.index(ziel, 0)
        self.grid.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtTop)
        self.grid.setCurrentIndex(self.model.index(row, 0))
        taken = str((self.model.row_data(row) or {}).get("taken_at") or "")
        self.jahresleiste.setze_aktuell(taken[:7] if len(taken) >= 7
                                        else taken[:4])

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
            if kind in ("ort", "suche"):
                # Der SERVER hat geantwortet, hier werden die Kennungen
                # nur noch mit dem Lokalen zusammengefuehrt: lokale
                # Zeilen zuerst (die haben Bewertung und Bearbeitung),
                # reine Serverbilder hinterher.
                treffer = list(self._server_treffer)
                rows = list(self.db.photos_by_immich_ids(
                    treffer, order=self.sort_box.currentData(),
                    **richtung, **common))
                bekannt = {r["immich_id"] for r in rows if r["immich_id"]}
                rows += [_remote_zeile(r) for r in self.db.remote_by_ids(
                    [k for k in treffer if k not in bekannt])]
            elif kind == "sammlung":
                rows = list(self.db.photos_by_paths(
                    list(self.config["sammlung"]),
                    order=self.sort_box.currentData(), **richtung, **common))
                rows += [_remote_zeile(r) for r in
                         self.db.remote_by_ids(list(self.config["sammlung_remote"]))]
            elif kind == "all":
                rows = []          # wird weiter unten als Cursor geholt
            elif kind == "folder":
                rows = self.db.photos_in_folder(
                    key, recursive=self.recursive_button.isChecked(),
                    order=self.sort_box.currentData(), **richtung, **common,
                )
            elif kind == "unbenannt":
                rows = [_person_zeile(r) for r in
                        self.db.people(include_unnamed=True)
                        if not (r["name"] or "").strip()]
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
            # Bilder, die nur auf dem Server liegen, gehoeren seit 0.3.37
            # mit in „Alle Fotos" - ein eigener Eintrag dafuer war eine
            # kuenstliche Trennung, die niemand sucht.
            nur_server = [_remote_zeile(r) for r in self.db.remote_only(
                order=sortierung, desc=self.sort_desc_button.isChecked())]
            self.model.set_cursor(
                cursor, group_by=gruppierung, zusatz=nur_server,
                sortschluessel=_sortschluessel_fuer(sortierung),
                desc=self.sort_desc_button.isChecked())
            gezeigt = self.db.count_photos(
                self.config.libraries, min_rating=common["min_rating"],
                stacked=common["stacked"], show_rejects=common["show_rejects"],
                labels=common["labels"], unlabeled=common["unlabeled"]
            ) + len(nur_server)
        else:
            self.model.set_rows(rows, group_by=gruppierung)
            gezeigt = len([r for r in rows if "_header" not in r])

        self.model.set_edited(self.db.edited_steps())
        self._jahresleiste_fuellen()
        self._gesamt = gezeigt
        if self.in_loupe:
            # Die Liste hat sich unter der Lupe verändert - zurück ins Raster
            self._show_grid()
        self._update_status(gezeigt)

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

        gesichter = [r for r in rows
                     if (self.model.row_data(r) or {}).get("_person")]
        if gesichter:
            # Gesichtskacheln koennen nichts von dem, was das normale
            # Menue anbietet (keine Datei, keine Bewertung) - hier gibt
            # es genau die zwei sinnvollen Sachen.
            menu = QMenu(self)
            person_id = self.model.row_data(gesichter[0])["person_id"]
            benennen = menu.addAction(
                "Person benennen …",
                lambda: self._person_umbenennen(person_id))
            zusammen = menu.addAction(
                "In eine benannte Person schieben …",
                lambda: self._gesicht_einordnen(person_id))
            hat_server = self.model._immich_client is not None
            for aktion in (benennen, zusammen):
                aktion.setEnabled(hat_server and len(gesichter) == 1)
            menu.exec(self.grid.viewport().mapToGlobal(position))
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
            menu.addAction(f"In vorläufige Sammlung übernehmen ({len(rows)})\tS",
                           self._sammlung_von_auswahl)
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
        menu.addAction(f"{count} Aufnahme(n) exportieren …\tStrg+Umsch+E",
                       self._export_auswahl)
        menu.addAction(f"In vorläufige Sammlung übernehmen ({count})\tS",
                       self._sammlung_von_auswahl)
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
        self._crop_knopf_setzen(False)
        self._remote_wartet = ""
        self._vorschau_signale.gewuenscht = ""
        self._set_tool(TOOL_NONE)
        self._crop_mode = False
        self.pages.setCurrentIndex(0)
        self.panel_box.setVisible(False)
        self.loupe_header.setVisible(False)
        self.crop_bar.setVisible(False)
        self._filterleiste_zeigen()
        self.grid.setFocus()
        if 0 <= self._loupe_row < self.model.rowCount():
            index = self.model.index(self._loupe_row, 0)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self._update_status(self._gesamt)

    def _grid_doppelklick(self, index) -> None:
        """Doppelklick im Raster: Lupe - ausser bei einem Gesicht.

        Eine Gesichtskachel hat keine Datei; die Lupe zeigte sonst
        „keine Vorschau". Stattdessen fuehrt der Doppelklick geradewegs
        zum Benennen, denn genau dafuer ist die Ansicht da.
        """
        zeile = self.model.row_data(index.row()) or {}
        if zeile.get("_person"):
            if self.model._immich_client is not None:
                self._person_umbenennen(zeile["person_id"])
            return
        self._show_loupe(index.row())

    def _show_loupe(self, row: int) -> None:
        if row < 0 or row >= self.model.rowCount():
            return
        if (self.model.row_data(row) or {}).get("_person"):
            return          # Gesichtskachel: es gibt nichts zu vergroessern
        self.pages.setCurrentIndex(1)
        self.panel_box.setVisible(self._chrome_visible and not self._panel_hidden)
        self.loupe_header.setVisible(self._chrome_visible)
        self.filter_bar.setVisible(False)
        self._load_loupe(row)
        self.canvas.setFocus()

    def _filterleiste_zeigen(self) -> None:
        """Filterleiste nur zeigen, wenn sie eingeschaltet ist.

        Sie gehoert zum Raster; in der Lupe ist sie ohnehin weg, und
        ausgeblendete Leisten (Tab) haben Vorrang.
        """
        self.filter_bar.setVisible(
            self._chrome_visible and not self.in_loupe
            and not self._filter_hidden)

    def _toggle_filterleiste(self, checked: bool) -> None:
        self._filter_hidden = not checked
        self.config["filter_bar"] = bool(checked)
        self._filterleiste_zeigen()

    def _toggle_zoomregler(self, checked: bool) -> None:
        """Zoomregler ein- oder ausblenden und den Zustand merken."""
        self.config["zoom_slider"] = bool(checked)
        self.zoom_slider.setVisible(checked)
        self.zoom_label.setVisible(checked)

    def _zoom_geregelt(self, wert: int) -> None:
        """Regler bewegt: Vergroesserung setzen.

        Der Wert ist in Promille - QSlider kann nur ganze Zahlen, und
        Prozentschritte waeren zum Feinstellen zu grob.
        """
        if self._zoom_stumm:
            return
        self.canvas.set_zoom(wert / 1000.0)

    def _zoom_anzeigen(self, faktor: float) -> None:
        """Regler und Beschriftung dem Bild nachfuehren.

        Stumm geschaltet, sonst schickte der Regler die gerade
        empfangene Vergroesserung sofort zurueck ans Bild.
        """
        self._zoom_stumm = True
        try:
            self.zoom_slider.setValue(round(faktor * 1000))
        finally:
            self._zoom_stumm = False
        self.zoom_label.setText(f"{faktor * 100:.0f} %")

    def _crop_button_geschaltet(self, an: bool) -> None:
        """Knopf und Taste R sollen dasselbe tun."""
        if an != self._crop_mode:
            self._toggle_crop_mode(an)

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
        # Von Hand geblaettert heisst: die Diashow ist nicht mehr gewollt
        if self._diashow_timer.isActive() and not self._diashow_eigener_schritt:
            self._diashow_beenden()
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
        self._filterleiste_zeigen()
        # Die GANZE linke Spalte, nicht nur der Baum: das Suchfeld stand
        # sonst mit Tab weiter da (0.3.45).
        self.links_spalte.setVisible(visible)
        self.statusBar().setVisible(visible)
        self.loupe_header.setVisible(visible and self.in_loupe)
        self.panel_box.setVisible(visible and self.in_loupe and not self._panel_hidden)
        self.crop_bar.setVisible(visible and self._crop_mode)

    def _toggle_chrome(self) -> None:
        self._set_chrome(not self._chrome_visible)

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

    # -- Diashow -------------------------------------------------------

    def _diashow_umschalten(self) -> None:
        if self._diashow_timer.isActive():
            self._diashow_beenden()
        else:
            self._diashow_starten()

    def _diashow_starten(self) -> None:
        """Vollbild, Bild fuer Bild, im eingestellten Takt.

        Losgelaufen wird bei dem Bild, das gerade dran ist - in der Lupe
        das gezeigte, im Raster das ausgewaehlte, sonst das erste.
        """
        if not self.in_loupe:
            self._loupe_from_selection()
        if not self.in_loupe:
            self.statusBar().showMessage(
                "Für die Diashow wird mindestens ein Bild gebraucht.", 4000)
            return
        self._diashow_vollbild_vorher = self.isFullScreen()
        self._set_chrome(False)
        self.showFullScreen()
        takt = max(1, int(self.config["diashow_sekunden"]))
        self._diashow_timer.start(takt * 1000)
        self.statusBar().showMessage(
            f"Diashow: {takt} s je Bild — F5 oder Esc beendet sie", 5000)

    def _diashow_beenden(self) -> None:
        if not self._diashow_timer.isActive():
            return
        self._diashow_timer.stop()
        # Das Vollbild nur zuruecknehmen, wenn die Diashow es angemacht
        # hat - sonst risse sie einen laufenden Vollbildmodus mit.
        if not getattr(self, "_diashow_vollbild_vorher", False):
            self.showNormal()
            self._set_chrome(True)
        self.statusBar().showMessage("Diashow beendet.", 3000)

    _diashow_eigener_schritt = False

    def _diashow_schritt(self) -> None:
        """Ein Bild weiter; am Ende von vorn.

        Am Ende der Liste wird erst nachgeladen - bei „Alle Fotos" haengt
        immer noch etwas im Cursor.
        """
        naechste = self._loupe_row + 1
        while naechste >= self.model.rowCount() and self.model.canFetchMore():
            self.model.fetchMore()
        if naechste >= self.model.rowCount():
            naechste = 0
            zeilen = self.model.photo_rows()
            if not zeilen:
                self._diashow_beenden()
                return
            naechste = zeilen[0]
        self._diashow_eigener_schritt = True
        try:
            self._show_loupe(naechste)
        finally:
            self._diashow_eigener_schritt = False

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

    def _masse_berichtigt(self, pfad: str, breite: int, hoehe: int) -> None:
        """Falsche Bildmasse im Index richtigstellen.

        Passiert waehrend des Blaetterns, sobald die Vorschau zeigt, dass
        der Eintrag quer steht, obwohl das Bild hochkant ist. Ohne den
        Eintrag in der Datenbank waere es beim naechsten Start wieder
        falsch, und die Metadatenanzeige zeigte weiter Querformat.
        """
        if not pfad:
            return
        try:
            self.db.masse_berichtigen(pfad, breite, hoehe)
            self._masse_berichtigt_zahl = getattr(
                self, "_masse_berichtigt_zahl", 0) + 1
            self._masse_speichern.start()
        except Exception:
            crashlog.protokolliere("Bildmaße berichtigen")

    def _masse_pruefen(self) -> None:
        """Den ganzen Index gegen die Dateien halten.

        Nötig, weil das Nachbessern beim Blättern nur heilt, was man
        auch ansieht. Wer die falsche Größe in den Metadaten stört,
        will nicht erst durch die halbe Bibliothek scrollen.
        """
        if self._masse_thread is not None:
            self.statusBar().showMessage("Die Prüfung läuft bereits.", 4000)
            return
        zeilen = self.db.masse_pruefliste()
        if not zeilen:
            QMessageBox.information(self, "Bildmaße", "Der Index ist leer.")
            return
        antwort = QMessageBox.question(
            self, "Bildmaße prüfen",
            f"{len(zeilen)} Einträge gegen die Dateien halten?\n\n"
            "Falsch herum eingetragene Maße werden umgedreht. An den "
            "Dateien selbst ändert sich nichts, Bewertungen und Marken "
            "bleiben unberührt.")
        if antwort != QMessageBox.StandardButton.Yes:
            return

        self.sync_progress_bar.setRange(0, len(zeilen))
        self.sync_progress_bar.setValue(0)
        self.sync_progress_bar.setVisible(True)
        self.sync_label.setText("Bildmaße werden geprüft …")
        self.sync_label.setVisible(True)

        self._masse_thread = QThread(self)
        self._masse_worker = MasseWorker([tuple(z) for z in zeilen],
                                         str(DB_PATH))
        self._masse_worker.moveToThread(self._masse_thread)
        self._masse_thread.started.connect(self._masse_worker.run)
        self._masse_worker.fortschritt.connect(self._masse_fortschritt)
        self._masse_worker.fertig.connect(self._masse_fertig)
        self._masse_thread.start()

    def _masse_fortschritt(self, fertig: int, gesamt: int) -> None:
        self.sync_progress_bar.setValue(fertig)
        self.sync_label.setText(f"Bildmaße: {fertig}/{gesamt} geprüft")

    def _masse_fertig(self, berichtigt: int, uebersprungen: int,
                      gesamt: int) -> None:
        if self._masse_thread is not None:
            self._masse_thread.quit()
            self._masse_thread.wait(5000)
        self._masse_thread = None
        self._masse_worker = None
        self.sync_progress_bar.setVisible(False)
        self.sync_label.setText(
            f"{berichtigt} von {gesamt} Bildmaßen richtiggestellt"
            + (f", {uebersprungen} übersprungen" if uebersprungen else ""))
        self.sync_label.setVisible(True)
        if berichtigt:
            self._refresh_view()

    def _masse_sichern(self) -> None:
        """Gesammelte Berichtigungen in einem Zug festschreiben.

        Nicht nach jedem Bild: beim Blaettern durch einen Ordner mit
        vielen falschen Eintraegen waere das ein Schreibvorgang je
        Kachel.
        """
        anzahl = getattr(self, "_masse_berichtigt_zahl", 0)
        if not anzahl:
            return
        self._masse_berichtigt_zahl = 0
        self.db.commit()
        self.statusBar().showMessage(
            f"{anzahl} Bildgröße(n) richtiggestellt (Hochformat war quer "
            "eingetragen)", 6000)

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

    def _clear_crop(self) -> None:
        if self._remote_aktiv():
            return
        self._stack_edits.remove_kind(CROP)
        self.canvas.set_crop(None)
        self._render_loupe()
        self._save_edits()

    def _crop_knopf_setzen(self, an: bool) -> None:
        """Knopf nachziehen, ohne seinen eigenen Schalter auszuloesen."""
        if self.crop_button.isChecked() != an:
            gesperrt = self.crop_button.blockSignals(True)
            self.crop_button.setChecked(an)
            self.crop_button.blockSignals(gesperrt)

    def _toggle_crop_mode(self, on: bool | None = None) -> None:
        if self._remote_aktiv():
            return
        self._crop_mode = (not self._crop_mode) if on is None else bool(on)
        self._crop_knopf_setzen(self._crop_mode)
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
        self.panel.fade_slider.setValue(round(value * 100))
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
        self.model.set_edited(self.db.edited_steps())

    def _export_auswahl(self) -> None:
        """Stapel-Export: die ausgewaehlten Bilder in einen Ordner rechnen.

        Reine Serverbilder haben keine Datei und fallen heraus - gerechnet
        wird immer aus dem Original auf der Platte.
        """
        if self._export_thread is not None:
            self.statusBar().showMessage("Es läuft bereits ein Export.", 4000)
            return
        rows = sorted({idx.row() for idx in self.grid.selectedIndexes()})
        if not rows and self.in_loupe and self._loupe_path:
            rows = [self._loupe_row]

        auftraege: list[tuple[str, str | None]] = []
        ohne_datei = 0
        for row in rows:
            item = self.model.row_data(row) or {}
            pfad = str(item.get("path") or "")
            if not pfad:
                ohne_datei += 1
                continue
            auftraege.append((pfad, self.db.load_edits(pfad)))

        if not auftraege:
            QMessageBox.information(
                self, "Nichts zu exportieren",
                "Es ist kein Bild mit lokaler Datei ausgewählt."
                + (" Reine Serverbilder müssen erst geholt werden."
                   if ohne_datei else ""))
            return

        dialog = ExportDialog(len(auftraege), self.config["export_dir"], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        optionen = dialog.optionen()
        if optionen is None:
            self.statusBar().showMessage("Kein Zielordner gewählt.", 4000)
            return
        self.config["export_dir"] = str(optionen.ziel)

        self._starte_export(auftraege, optionen, ohne_datei)

    def _starte_export(self, auftraege, optionen, ohne_datei: int) -> None:
        """Den Stapel in einem eigenen Faden rechnen lassen.

        Im Fensterfaden stuende das Programm: ein 45-MP-RAW mit
        Retuschen braucht Sekunden, und der Balken soll laufen.
        """
        self._export_ohne_datei = ohne_datei
        self.sync_progress_bar.setRange(0, len(auftraege))
        self.sync_progress_bar.setValue(0)
        self.sync_progress_bar.setVisible(True)
        self.sync_label.setText("Export läuft …")
        self.sync_label.setVisible(True)

        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(auftraege, optionen)
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.fortschritt.connect(self._export_fortschritt)
        self._export_worker.fertig.connect(self._export_fertig)
        self._export_thread.start()

    def _export_fortschritt(self, name: str, fertig: int, gesamt: int) -> None:
        self.sync_progress_bar.setValue(fertig)
        self.sync_label.setText(
            f"Export {fertig}/{gesamt}" + (f": {name}" if name else ""))

    def _export_fertig(self, geschrieben: int, uebersprungen: int,
                       fehler: list) -> None:
        if self._export_thread is not None:
            self._export_thread.quit()
            self._export_thread.wait(5000)
        self._export_thread = None
        self._export_worker = None
        self.sync_progress_bar.setVisible(False)

        teile = [f"{geschrieben} Bild(er) exportiert"]
        if uebersprungen:
            teile.append(f"{uebersprungen} ohne Bearbeitung übersprungen")
        if getattr(self, "_export_ohne_datei", 0):
            teile.append(f"{self._export_ohne_datei} ohne lokale Datei")
        if fehler:
            teile.append(f"{len(fehler)} Fehler")
        self.sync_label.setText(", ".join(teile))
        self.sync_label.setVisible(True)

        if fehler:
            QMessageBox.warning(
                self, "Export mit Fehlern",
                "Diese Bilder konnten nicht geschrieben werden:\n\n"
                + "\n".join(fehler[:12])
                + (f"\n… und {len(fehler) - 12} weitere" if len(fehler) > 12 else ""))

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

    def eventFilter(self, obj, event):
        """Tasten aus Raster, Lupe und Baum zentral behandeln.

        Bis 0.3.29 stand diese Methode zwar da, war aber NIRGENDS
        angemeldet - installEventFilter fehlte. Folge (nachgemessen):
        die Pfeiltasten kamen nie im Fenster an. In der Lupe hat der
        QGraphicsView sie zum Scrollen verbraucht, im Raster hat die
        Liste sie fuer ihre eigene Auswahl genommen und aus Ziffern eine
        Namenssuche gemacht - „3" setzte also keine Bewertung.
        """
        if (event.type() == QEvent.Type.KeyPress and self._tasten_hier(obj)
                and self._handle_key(event)):
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
        return not (isinstance(fokus, QWidget) and fokus.window() is not self)

    def keyPressEvent(self, event) -> None:
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

        # Escape beendet ZUERST die Diashow. Sonst faengt sie der
        # Zuschnitt ab, wenn der noch an war - gemessen im Testlauf.
        if key == Qt.Key.Key_Escape and self._diashow_timer.isActive():
            self._diashow_beenden()
            return True

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

        if key == Qt.Key.Key_S and not ctrl:
            # NICHT B: das ist in der Lupe seit jeher „vorher/nachher"
            self._sammlung_von_auswahl()
            return True

        if ctrl and key in (Qt.Key.Key_0, Qt.Key.Key_9):
            if self.in_loupe:
                self.canvas.fit()
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

    def _show_diagnose(self) -> None:
        """Sagt, woran es hängt, statt raten zu lassen.

        Die drei häufigsten Ursachen für Zähigkeit lassen sich hier in
        Sekunden auseinanderhalten: fehlendes rawpy, fehlendes exiftool,
        oder schlicht ein noch nicht durchgelaufener Metadatenlauf.
        """
        import time
        from . import retouch as _r

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

    def closeEvent(self, event) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()
        self._sync_timer.stop()
        self._diashow_timer.stop()
        self._watch_delay.stop()
        if self._sync_worker is not None:
            self._sync_worker.cancel()
        # Ein laufender Stapel-Export muss enden, bevor das Fenster
        # abgeraeumt wird - sonst meldet sich der Faden ins Leere.
        if self._masse_worker is not None:
            self._masse_worker.abbrechen()
        if self._masse_thread is not None:
            self._masse_thread.quit()
            self._masse_thread.wait(5000)
            self._masse_thread = None
        if self._export_worker is not None:
            self._export_worker.abbrechen()
        if self._export_thread is not None:
            self._export_thread.quit()
            self._export_thread.wait(5000)
            self._export_thread = None
        self._teardown_scan()
        self._teardown_sync()
        # Laufende Kachelaufgaben abwarten: sie melden sich ueber
        # Signale zurueck: kommt die Meldung, nachdem das Fenster schon
        # abgeraeumt ist, wirft Qt „wrapped C/C++ object has been
        # deleted". Beim Beenden im Testlauf nachgestellt.
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(3000)
        # Auch der eigene Pool fuer Server-Vorschauen - sonst meldet Qt
        # beim Beenden „wrapped C/C++ object ... has been deleted".
        self._remote_wartet = ""
        self._pool.waitForDone(3000)
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


def jahr_von(item: dict | None) -> str:
    """Jahr einer Modellzeile, leer wenn ohne Aufnahmedatum."""
    return str((item or {}).get("taken_at") or "")[:4]


def _sortschluessel_fuer(sortierung: str):
    """Womit wird verglichen, wenn Serverbilder eingefaedelt werden?

    Muss zu der Sortierung passen, die die Datenbank benutzt - sonst
    landen die Serverbilder an der falschen Stelle. Fuer Sortierungen,
    die ein Serverbild gar nicht kennt (Ordner, Bewertung, Datei-
    aenderung), wird das Aufnahmedatum genommen; das ist das Einzige,
    was beide Seiten sicher haben.
    """
    if sortierung == "filename":
        return lambda item: str(item.get("filename") or "").lower()
    return lambda item: str(item.get("taken_at") or "")


def _person_zeile(row) -> dict:
    """Ein unbenanntes Gesicht als Modellzeile fuers Raster.

    Dieselben Schluessel wie eine Bildzeile, damit Raster und Delegate
    nichts Besonderes koennen muessen - erkennbar allein an _person.
    path bleibt leer: es GIBT keine Datei, und die Lupe haelt sich
    deshalb heraus (_ist_person).
    """
    return {
        "id": -1,
        "_person": True,
        "person_id": row["id"],
        "immich_id": "",
        "path": "",
        "filename": "(ohne Namen)",
        "folder": "Gesicht auf dem Server",
        "taken_at": None,
        "width": 1, "height": 1,      # Gesichter sind quadratisch
        "rating": 0,
        "label": None,
        "stack_count": 1,
        "is_raw": 0,
    }


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
    """Die Einblendung in der Lupe als Rich-Text.

    Kamera, Objektiv und Dateiname kommen aus der Datei bzw. vom Server
    und koennen alles enthalten - ein „<" darin zerlegte bisher die
    Anzeige. Maskiert wird wie in loupe_title. (Gemessen 0.3.43: ein
    QLabel holt bei <img src=http://…> NICHTS aus dem Netz, es ging also
    nur um die Darstellung.)
    """
    rows = []
    if item.get("taken_at"):
        rows.append(_html_escape(str(item["taken_at"])))
    if item.get("camera"):
        rows.append(_html_escape(str(item["camera"])))
    if item.get("lens"):
        rows.append(_html_escape(str(item["lens"])))
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
    rows.append(_html_escape(str(item["filename"])))
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
<tr><td>Strg+L</td><td>Filterleiste</td></tr>
<tr><td>S</td><td>in die vorläufige Sammlung (dort: heraus)</td></tr>
<tr><td>Strg + / Strg −</td><td>vergrößern / verkleinern</td></tr>
<tr><td>Strg+0</td><td>einpassen</td></tr>
<tr><td>F5</td><td>Diashow starten und beenden</td></tr>
<tr><td>I</td><td>Bildangaben</td></tr>
<tr><td>Strg+A / Strg+D</td><td>alles wählen / Auswahl aufheben</td></tr>
<tr><td>Strg+Z</td><td>Bearbeitungsschritt zurück</td></tr>
<tr><td>Strg+F</td><td>in die Suchleiste; Enter führt zurück</td></tr>
<tr><td>F5 / F6</td><td>neu einlesen / Immich abgleichen</td></tr>
<tr><td>Strg+,</td><td>Einstellungen</td></tr>
</table>
<p style="color:#8e8e9c">Wimmich {version} — freie Software unter der
GNU GPL v3 oder später. Ohne jede Gewährleistung.</p>"""
