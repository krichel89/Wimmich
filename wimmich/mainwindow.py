"""Hauptfenster.

Alles in einem Fenster: links der Baum, rechts entweder das Raster oder
die Lupe. In der Lupe sitzt die Bearbeitungsleiste rechts daneben.
Es gibt keine eigenen Fenster mehr für Ansicht und Bearbeitung.

Die Tastenbelegung ist die von Cammello - siehe _handle_key().
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import (
    QEvent, QFileSystemWatcher, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QColor, QIcon, QKeySequence, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListView, QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter,
    QStackedWidget, QStatusBar,
    QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__, marks, retouch, theme, thumbs
from .config import Config, DB_PATH, ensure_dirs, find_exiftool
from .db import Database
from .exif import ExifTool, ExifToolError
from .models import PhotoDelegate, PhotoModel, ROLE_ID, ROLE_PATH
from .previews import PreviewLoader
from .canvas import CanvasView, NONE as TOOL_NONE, PIPETTE
from .edit_panel import EditPanel
from .edits import (
    CROP, EditStack, FADED, RED_EYE, SPOT, STROKE, Step, TONE,
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

SORT_OPTIONS = [
    ("Aufnahmedatum", "taken_at"),
    ("Dateiname", "filename"),
    ("Bewertung", "rating"),
    ("Zuletzt geändert", "mtime"),
]


class MainWindow(QMainWindow):
    start_scan = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        self.config = Config()
        self.db = Database(DB_PATH)

        exe = find_exiftool(self.config["exiftool"])
        self.exiftool = ExifTool(exe)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
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
        self._scan_worker: ScanWorker | None = None

        self._build_ui()
        self._build_actions()
        self.panel.setVisible(False)
        self.loader.ready.connect(self._image_arrived)
        self._reload_folder_tree()
        self._update_status()
        self.grid.setFocus()
        self._apply_watch_settings()
        self._apply_sync_settings()

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

        self.rating_filter = QComboBox()
        self.rating_filter.addItem("Alle Bewertungen", 0)
        for stars in range(1, 6):
            self.rating_filter.addItem("\u2605" * stars + " und besser", stars)
        self.rating_filter.currentIndexChanged.connect(self._refresh_view)
        bar_layout.addWidget(self.rating_filter)

        self.sort_box = QComboBox()
        for label, key in SORT_OPTIONS:
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._refresh_view)
        bar_layout.addWidget(self.sort_box)

        self.recursive_button = QPushButton("Unterordner")
        self.recursive_button.setCheckable(True)
        self.recursive_button.setToolTip("Bilder aus allen Unterordnern mitzeigen")
        self.recursive_button.toggled.connect(self._refresh_view)
        bar_layout.addWidget(self.recursive_button)

        self.reject_button = QPushButton("Abgelehnte")
        self.reject_button.setCheckable(True)
        self.reject_button.setChecked(True)
        self.reject_button.setToolTip(
            "Abgelehnte Bilder (Taste X) mitzeigen.\n"
            "Bei aktivem Sternfilter fallen sie immer heraus."
        )
        self.reject_button.toggled.connect(self._refresh_view)
        bar_layout.addWidget(self.reject_button)

        self.label_filter = QComboBox()
        self.label_filter.addItem("Alle Farben", None)
        for i, colour in enumerate(marks.LABEL_COLORS):
            self.label_filter.addItem(marks.label_text(i, self._label_set()), i)
        self.label_filter.currentIndexChanged.connect(self._refresh_view)
        bar_layout.addWidget(self.label_filter)

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

        self.tree = QTreeWidget()
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
        self.grid = QListView()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(PhotoDelegate(self.config["grid_size"]))
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(6)
        self.grid.setMouseTracking(True)   # damit die Kachel beim Ueberfahren reagiert
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.doubleClicked.connect(lambda idx: self._show_loupe(idx.row()))
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        # Rechts: Raster ODER Lupe, im selben Fenster
        self.pages = QStackedWidget()
        self.pages.addWidget(self.grid)          # Seite 0

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
        self.panel.fade_changed.connect(self._fade_changed)
        self.panel.suggest_fade.connect(self._suggest_fade)
        self.panel.pipette_toggled.connect(self._set_pipette)
        self.panel.crop_cleared.connect(self._clear_crop)
        self.panel.reset_all.connect(self._reset_edits)
        self.panel.undo.connect(self._undo_edit)
        self.panel.export.connect(self._export_edited)
        loupe_layout.addWidget(self.panel)

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

        loupe_column = QWidget()
        column_layout = QVBoxLayout(loupe_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(4)
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
        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)

    def _build_actions(self) -> None:
        toolbar = QToolBar("Hauptleiste")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.toolbar = toolbar



        scan_action = QAction("Neu einlesen", self)
        scan_action.setShortcut(QKeySequence("F5"))
        scan_action.triggered.connect(self._rescan)
        toolbar.addAction(scan_action)

        toolbar.addSeparator()

        immich_action = QAction("Immich abgleichen", self)
        immich_action.setShortcut(QKeySequence("F6"))
        immich_action.triggered.connect(self._start_sync)
        toolbar.addAction(immich_action)

        self.loupe_action = QAction("Lupe / Raster", self)
        self.loupe_action.setToolTip("Zwischen Raster und Lupe wechseln (E oder G)")
        self.loupe_action.triggered.connect(self._toggle_loupe)
        toolbar.addAction(self.loupe_action)

        toolbar.addSeparator()

        keys_action = QAction("Tastenkürzel", self)
        keys_action.setShortcut(QKeySequence("F1"))
        keys_action.triggered.connect(self._show_keys)
        toolbar.addAction(keys_action)

        settings_action = QAction("Einstellungen", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

        toolbar.addSeparator()

        clear_action = QAction("Vorschau-Cache leeren", self)
        clear_action.triggered.connect(self._clear_cache)
        toolbar.addAction(clear_action)

        # Die Tasten laufen zentral über _handle_key(), damit Raster und
        # Lupe dieselbe Belegung haben - siehe dort.
        self.grid.installEventFilter(self)
        self.canvas.installEventFilter(self)
        self.tree.installEventFilter(self)

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
        for root, rows, kind in (
            (self._albums_root, self.db.albums(), "album"),
            (self._people_root, self.db.people(), "person"),
        ):
            root.takeChildren()
            for row in rows:
                name = row["name"] or "(ohne Namen)"
                label = (f"{name}  ({row['asset_count']})"
                         if kind == "album" else name)
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, (kind, row["id"]))
                root.addChild(item)
            if not rows:
                hint = QTreeWidgetItem(["— noch nicht abgeglichen —"])
                hint.setData(0, Qt.ItemDataRole.UserRole, None)
                hint.setFlags(Qt.ItemFlag.NoItemFlags)
                root.addChild(hint)
            root.setExpanded(bool(rows))

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
            # Platzhalter, damit der Aufklapppfeil erscheint
            child.addChild(QTreeWidgetItem(["..."]))
            parent.addChild(child)

    def _expand_item(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "folder":
            return
        if item.childCount() == 1 and item.child(0).text(0) == "...":
            self._add_children(item, data[1])

    def _tree_menu(self, position) -> None:
        """Rechtsklick im Baum: Ordner ausschließen oder wieder aufnehmen."""
        item = self.tree.itemAt(position)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "folder":
            return
        folder = data[1]

        menu = QMenu(self)
        if self.config.is_excluded(folder):
            menu.addAction("Wieder aufnehmen",
                           lambda: self._include_folder(folder))
        else:
            menu.addAction("Ordner ausschließen",
                           lambda: self._exclude_folder(folder))
        menu.exec(self.tree.viewport().mapToGlobal(position))

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

    # -- Anzeige -------------------------------------------------------

    def _current_min_rating(self) -> int:
        return int(self.rating_filter.currentData() or 0)

    def _label_set(self) -> str:
        return str(self.config["label_set"] or "de")

    def _current_labels(self) -> list[str] | None:
        """Gefilterte Markierungstexte - ALLE Sätze, nicht nur der aktive.

        Sonst findet ein Filter auf "Rot" die Bilder nicht, die Lightroom
        auf Englisch als "Red" markiert hat.
        """
        index = self.label_filter.currentData()
        if index is None:
            return None
        return [texts[index] for texts in marks.LABEL_SETS.values()
                if index < len(texts)]

    def _refresh_view(self) -> None:
        query = self.search_box.text().strip()
        common = dict(
            min_rating=self._current_min_rating(),
            stacked=self.stack_button.isChecked(),
            prefer_raw=bool(self.config["prefer_raw"]),
            show_rejects=self.reject_button.isChecked(),
            labels=self._current_labels(),
        )

        if query:
            rows = self.db.search(query, **common)
        elif self._selection is None:
            rows = []
        else:
            kind, key = self._selection
            if kind == "all":
                rows = self.db.all_photos(self.config.libraries, **common)
            elif kind == "folder":
                rows = self.db.photos_in_folder(
                    key, recursive=self.recursive_button.isChecked(),
                    order=self.sort_box.currentData(), **common,
                )
            elif kind == "album":
                rows = self.db.photos_by_immich(
                    "album_assets", "album_id", key,
                    order=self.sort_box.currentData(), **common,
                )
            else:
                rows = self.db.photos_by_immich(
                    "person_assets", "person_id", key,
                    order=self.sort_box.currentData(), **common,
                )

        picasa = bool(self._selection and self._selection[0] == "all"
                      and not self.search_box.text().strip())
        self.grid.setUniformItemSizes(not picasa)
        self.model.set_rows(rows, group_by_folder=picasa)
        self.model.set_edited(self.db.edited_paths())
        if self.in_loupe:
            # Die Liste hat sich unter der Lupe verändert - zurück ins Raster
            self._show_grid()
        self._update_status(len(rows))

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
        menu = self._build_grid_menu(len(rows))
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

        for row in rows:
            item = self.model.row_data(row)
            if item is None:
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

        extra = f" ({files} Dateien)" if files != len(rows) else ""
        wording = ("als abgelehnt markiert" if marks.is_reject(rating)
                   else f"mit {rating} Stern(en) bewertet")
        self.statusBar().showMessage(
            f"{len(rows)} Aufnahme(n) {wording}{extra}", 4000
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
        self.panel.setVisible(False)
        self.crop_bar.setVisible(False)
        self.grid.setFocus()
        if 0 <= self._loupe_row < self.model.rowCount():
            index = self.model.index(self._loupe_row, 0)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self._update_status(self.model.rowCount())

    def _show_loupe(self, row: int) -> None:
        if row < 0 or row >= self.model.rowCount():
            return
        self.pages.setCurrentIndex(1)
        self.panel.setVisible(self._chrome_visible)
        self._load_loupe(row)
        self.canvas.setFocus()

    def _load_loupe(self, row: int) -> None:
        item = self.model.row_data(row)
        if item is None:
            return
        # Beim Stapel wird das JPEG gezeigt und bearbeitet - eine
        # RAW-Datei lässt sich nicht pixelweise verändern.
        self._loupe_row = row
        self._loupe_path = item.get("thumb_path") or item["path"]
        self._want_full = False
        self._show_before = False
        self._stroke = []

        self._stack_edits = EditStack.from_json(
            self.db.load_edits(self._loupe_path))
        self.panel.load(self._stack_edits.single(TONE),
                        self._stack_edits.single(FADED),
                        self._crop_text(),
                        base_kelvin=item.get("color_temp"))

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
        nicht das Foto ist, verschwindet.
        """
        self._chrome_visible = visible
        self.toolbar.setVisible(visible)
        self.filter_bar.setVisible(visible)
        self.tree.setVisible(visible)
        self.statusBar().setVisible(visible)
        self.panel.setVisible(visible and self.in_loupe)
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
        if not size:
            return 0.0, 0.0
        rx, ry = x / size[0], y / size[1]

        crop_step = self._stack_edits.single(CROP)
        if crop_step is not None and not self._crop_mode:
            rx = crop_step.x + rx * crop_step.width
            ry = crop_step.y + ry * crop_step.height
        return rx, ry

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
        self._stack_edits.remove_kind(CROP)
        self.canvas.set_crop(None)
        self._render_loupe()
        self._save_edits()

    def _toggle_crop_mode(self, on: bool | None = None) -> None:
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
        if self._crop_aspect_key:
            self._set_aspect(self._crop_aspect_key)

    def _set_aspect(self, digit: int) -> None:
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
        self.statusBar().showMessage(
            f"Format: {ASPECT_NAMES[digit]}"
            + (" hoch" if self._crop_portrait and ratio else ""), 3000)

    def _tone_changed(self, step) -> None:
        self._stack_edits.remove_kind(TONE)
        if step is not None:
            self._stack_edits.add(step)
        self._render_soon()

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
                            base_kelvin=item.get("color_temp"))
        self._render_loupe()
        self._save_edits()

    def _reset_edits(self) -> None:
        self._stack_edits.clear()
        self.canvas.set_crop(None)
        item = self.model.row_data(self._loupe_row) or {}
        self.panel.load(None, None, self._crop_text(),
                        base_kelvin=item.get("color_temp"))
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
        """Tasten aus Raster, Lupe und Baum zentral behandeln."""
        if event.type() == QEvent.Type.KeyPress:
            if self._handle_key(event):
                return True
        return super().eventFilter(obj, event)

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
        if row < 0 or row >= self.model.rowCount():
            return
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

        for key, value in values.items():
            self.config[key] = value

        # Entfernte Ordner aus dem Index werfen. Die Dateien bleiben liegen.
        entfernt = 0
        for folder in removed:
            entfernt += self.db.delete_under(folder)

        if old_grid != values["grid_size"]:
            self.grid.setItemDelegate(PhotoDelegate(values["grid_size"]))

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
        self.statusBar().showMessage("Abgleich mit Immich läuft …")

    def _sync_progress(self, message: str, done: int, total: int) -> None:
        self.statusBar().showMessage(message)

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
        if result.failed:
            parts.append(f"{result.failed} fehlgeschlagen")
        self.statusBar().showMessage("Abgleich fertig: " + ", ".join(parts), 8000)

        if result.errors and not self._sync_quiet:
            QMessageBox.warning(
                self, "Abgleich mit Fehlern",
                "Diese Dateien konnten nicht übertragen werden:\n\n"
                + "\n".join(result.errors[:10])
                + ("\n…" if len(result.errors) > 10 else ""),
            )

    def _sync_failed(self, message: str) -> None:
        quiet = self._sync_quiet
        self._teardown_sync()
        if quiet:
            # Still weiterlaufen lassen: beim nächsten Durchlauf wird
            # erneut versucht. Nur die Statuszeile sagt Bescheid.
            self.statusBar().showMessage(f"Abgleich nicht möglich: {message}", 8000)
        else:
            QMessageBox.critical(self, "Abgleich fehlgeschlagen", message)
            self.statusBar().clearMessage()

    def _teardown_sync(self) -> None:
        if self._sync_thread is not None:
            self._sync_thread.quit()
            self._sync_thread.wait(5000)
        self._sync_thread = None
        self._sync_worker = None

    def _show_keys(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Tastenkürzel")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(KEY_HELP)
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
        self.exiftool.stop()
        self.db.close()
        super().closeEvent(event)


def _info_html(item: dict) -> str:
    rows = []
    if item.get("taken_at"):
        rows.append(item["taken_at"])
    if item.get("camera"):
        rows.append(item["camera"])
    if item.get("lens"):
        rows.append(item["lens"])
    if item.get("width") and item.get("height"):
        rows.append(f"{item['width']} × {item['height']} px")
    if int(item.get("stack_count") or 1) > 1:
        rows.append(f"Stapel aus {item['stack_count']} Dateien")
    rows.append(item["filename"])
    return "<br>".join(rows)


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
<tr><td>+ / −</td><td>Belichtung</td></tr>
<tr><td>W</td><td>Pipette (Weißabgleich)</td></tr>
<tr><td>C</td><td>Zuschnitt an/aus</td></tr>
<tr><td>&nbsp;&nbsp;darin 1–6</td><td>frei, 3:2, 4:3, 1:1, 16:9, 5:4<br>
gleiche Ziffer nochmal: hoch ⇄ quer</td></tr>
<tr><td>&nbsp;&nbsp;Enter / Esc</td><td>übernehmen / abbrechen</td></tr>
<tr><td>&nbsp;&nbsp;Shift+C</td><td>Zuschnitt aufheben</td></tr>
<tr><td>B</td><td>Vorher / Nachher</td></tr>
<tr><td>F</td><td>Vollbild — nur das Bild</td></tr>
<tr><td>Tab</td><td>Leisten und Baum ein/aus</td></tr>
<tr><td>I</td><td>Bildangaben</td></tr>
<tr><td>Strg+A / Strg+D</td><td>alles wählen / Auswahl aufheben</td></tr>
<tr><td>Strg+Z</td><td>Bearbeitungsschritt zurück</td></tr>
<tr><td>Strg+F</td><td>in die Suchleiste; Enter führt zurück</td></tr>
<tr><td>F5 / F6</td><td>neu einlesen / Immich abgleichen</td></tr>
<tr><td>Strg+,</td><td>Einstellungen</td></tr>
</table>"""
