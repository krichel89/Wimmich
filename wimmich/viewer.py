"""Großansicht: Fenster um die ImageView herum.

Zwei Ebenen: beim Blättern kommt zuerst die verkleinerte Ansicht
(schnell), erst beim Zoomen wird das unverkleinerte Bild nachgeladen.
Nachbarbilder werden im Voraus geholt, damit Blättern nicht wartet.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeySequence, QShortcut
from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

from . import marks, theme
from .imageview import ImageView
from .previews import PreviewLoader

PREFETCH_RADIUS = 2      # so viele Bilder in jede Richtung vorbereiten


class ViewerDialog(QDialog):
    """Großansicht mit Zoom, Vollbild, Sternen und Farbmarkierungen."""

    rating_changed = pyqtSignal(int, int)   # Zeile im Modell, Bewertung
    label_changed = pyqtSignal(int, int)    # Zeile im Modell, Farbindex

    def __init__(self, model, start_row: int, loader: PreviewLoader | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ansicht")
        self.setModal(False)
        self._model = model
        self._row = start_row
        self._current_path = ""
        self._want_full = False
        self._info_visible = False

        self._loader = loader or PreviewLoader(self)
        self._loader.ready.connect(self._image_ready)
        self._loader.failed.connect(self._image_failed)

        self.setStyleSheet(f"background: {theme.VIEWER_BG};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = ImageView(self)
        self.view.zoom_requested.connect(self._need_full)
        self.view.zoom_changed.connect(self._zoom_changed)
        self.view.fullscreen_requested.connect(self.toggle_fullscreen)
        layout.addWidget(self.view)

        self._status = QLabel("Wird geladen …", self)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 13px; background: transparent;"
        )
        layout.addWidget(self._status)

        self._build_shortcuts()

        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(int(area.width() * 0.9), int(area.height() * 0.9))

        self._load()

    def _build_shortcuts(self) -> None:
        def add(sequence, slot):
            QShortcut(QKeySequence(sequence), self, slot)

        add(Qt.Key.Key_Escape, self._escape)
        add(Qt.Key.Key_Right, lambda: self.step(1))
        add(Qt.Key.Key_Left, lambda: self.step(-1))
        add(Qt.Key.Key_Space, lambda: self.step(1))
        add(Qt.Key.Key_F, self.toggle_fullscreen)
        add(Qt.Key.Key_Z, lambda: self.view.toggle_zoom())
        add(Qt.Key.Key_I, self._toggle_info)
        add(Qt.Key.Key_X, lambda: self._set_rating(marks.REJECT))
        add("Ctrl++", lambda: self.view.zoom_step(1))
        add("Ctrl+-", lambda: self.view.zoom_step(-1))
        add("Ctrl+0", self.view.fit)

        for digit in range(6):
            add(str(digit), lambda value=digit: self._set_rating(value))
        # Lightroom-Belegung: 6 = Rot, 7 = Gelb, 8 = Grün, 9 = Blau
        for offset, key in enumerate(marks.LABEL_KEYS):
            add(key, lambda index=offset: self._set_label(index))

    # -- Navigation ----------------------------------------------------

    def step(self, delta: int) -> None:
        new_row = self._row + delta
        if 0 <= new_row < self._model.rowCount():
            self._row = new_row
            self._load()

    def _load(self) -> None:
        item = self._model.row_data(self._row)
        if item is None:
            return
        # Bei einem Stapel wird das JPEG gezeigt - das ist bereits ein
        # fertiges Vollbild und damit billiger als jedes RAW.
        self._current_path = item.get("thumb_path") or item["path"]
        self._want_full = False
        self.setWindowTitle(
            f"{item['filename']}  ·  {self._row + 1}/{self._model.rowCount()}"
        )

        image = self._loader.request(self._current_path, "screen")
        if image is not None:
            self._show(image)
        else:
            self.view.clear_image()
            self._status.setText("Wird geladen …")

        self._update_overlay()
        self._prefetch_neighbours()

    def _prefetch_neighbours(self) -> None:
        paths = []
        for offset in range(1, PREFETCH_RADIUS + 1):
            for row in (self._row + offset, self._row - offset):
                item = self._model.row_data(row)
                if item:
                    paths.append(item.get("thumb_path") or item["path"])
        self._loader.prefetch(paths, "screen")

    # -- Bild ----------------------------------------------------------

    def _show(self, image: QImage, keep_view: bool = False) -> None:
        self.view.set_image(image, keep_view=keep_view)
        self._status.setText("")

    def _image_ready(self, path: str, level: str, image: QImage) -> None:
        if path != self._current_path:
            return          # zwischenzeitlich weitergeblättert
        if level == "full":
            # Kommt an, während schon gezoomt ist: Pixel tauschen, Ansicht behalten
            self._show(image, keep_view=True)
        else:
            self._show(image)

    def _image_failed(self, path: str, level: str) -> None:
        if path == self._current_path and level == "screen":
            self._status.setText("Bild konnte nicht geladen werden.")

    def _need_full(self) -> None:
        """Beim Zoomen die unverkleinerte Ebene nachladen."""
        if self._want_full or not self._current_path:
            return
        self._want_full = True
        image = self._loader.request(self._current_path, "full")
        if image is not None:
            self._show(image, keep_view=True)

    def _zoom_changed(self, factor: float) -> None:
        self._update_overlay(zoom=factor)

    # -- Bewertung und Markierung --------------------------------------

    def _set_rating(self, rating: int) -> None:
        self.rating_changed.emit(self._row, rating)
        self._update_overlay()

    def _set_label(self, index: int) -> None:
        self.label_changed.emit(self._row, index)
        self._update_overlay()

    def _update_overlay(self, zoom: float | None = None) -> None:
        item = self._model.row_data(self._row)
        if item is None:
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
            parts.append(f'<span style="color:{colour}">●</span>')

        factor = zoom if zoom is not None else self.view.zoom_factor()
        if not self.view.is_fit:
            parts.append(f'<span style="color:#aaa">{factor * 100:.0f}\u2009%</span>')

        self.view.set_overlay("&nbsp;&nbsp;".join(parts))
        self.view.show_overlay(True)

        if self._info_visible:
            self.view.set_info_overlay(_info_html(item))

    def _toggle_info(self) -> None:
        self._info_visible = not self._info_visible
        item = self._model.row_data(self._row)
        if self._info_visible and item:
            self.view.set_info_overlay(_info_html(item))
        self.view.show_info_overlay(self._info_visible)

    def refresh_current(self) -> None:
        """Nach einer Änderung von außen die Anzeige nachziehen."""
        self._update_overlay()

    # -- Vollbild ------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._status.show()
        else:
            self._status.hide()
            self.showFullScreen()

    def _escape(self) -> None:
        """Esc verlässt erst das Vollbild, dann den Zoom, dann das Fenster."""
        if self.isFullScreen():
            self.toggle_fullscreen()
        elif not self.view.is_fit:
            self.view.fit()
        else:
            self.close()


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
