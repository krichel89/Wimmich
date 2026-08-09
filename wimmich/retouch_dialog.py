"""Retuschefenster.

Vier Werkzeuge, bewusst wenige:

  Fleck        klicken - Pickel, Staub, Flecken auf Scans
  Riss         ziehen  - Kratzer und Risse entlangfahren
  Rote Augen   klicken
  Auffrischen  Schieberegler für ausgeblichene Farben

Nichts davon verändert die Originaldatei. Die Schritte landen in der
Datenbank; wer ein fertiges Bild braucht, gibt es über „Als Datei
speichern" aus.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from . import edits as edits_mod
from . import previews, retouch, theme
from .edits import EditStack, Step, FADED, RED_EYE, SPOT, STROKE
from .imageview import ImageView

TOOLS = [
    ("spot", "Fleck", SPOT, "Pickel, Staub, Flecken - anklicken"),
    ("crack", "Riss", STROKE, "Kratzer und Risse - entlangziehen"),
    ("eye", "Rote Augen", RED_EYE, "Pupille anklicken"),
]


class RetouchDialog(QDialog):
    """Retusche eines einzelnen Bildes."""

    saved = pyqtSignal(str)      # Pfad, dessen Retusche sich geändert hat

    def __init__(self, path: str, stack: EditStack, parent=None) -> None:
        super().__init__(parent)
        self.path = path
        self.stack = stack
        self.setWindowTitle(f"Retusche — {Path(path).name}")
        self.resize(1100, 780)
        self.setStyleSheet(f"background: {theme.BG};")

        self._original: np.ndarray | None = None
        self._tool = SPOT
        self._brush = 26          # Pinselgröße in Bildschirmpunkten
        self._stroke: list[tuple[int, int]] = []
        self._show_before = False

        self._build_ui()
        self._load_image()

        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)
        QShortcut(QKeySequence("["), self, lambda: self._resize_brush(-4))
        QShortcut(QKeySequence("]"), self, lambda: self._resize_brush(4))

    # -- Aufbau --------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Werkzeugleiste
        tools = QHBoxLayout()
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, (_key, label, kind, tip) in enumerate(TOOLS):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setChecked(index == 0)
            button.clicked.connect(lambda _c, k=kind: self._set_tool(k))
            self._group.addButton(button)
            tools.addWidget(button)

        tools.addSpacing(16)
        tools.addWidget(QLabel("Pinsel"))
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(6, 120)
        self.brush_slider.setValue(self._brush)
        self.brush_slider.setFixedWidth(140)
        self.brush_slider.valueChanged.connect(self._brush_changed)
        tools.addWidget(self.brush_slider)
        self.brush_label = QLabel(f"{self._brush} px")
        self.brush_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        tools.addWidget(self.brush_label)

        tools.addStretch(1)
        self.undo_button = QPushButton("Rückgängig")
        self.undo_button.clicked.connect(self._undo)
        tools.addWidget(self.undo_button)
        reset = QPushButton("Alles zurücknehmen")
        reset.clicked.connect(self._reset)
        tools.addWidget(reset)
        layout.addLayout(tools)

        # Bild
        self.view = _CanvasView(self)
        self.view.clicked.connect(self._on_click)
        self.view.stroke_started.connect(self._stroke_start)
        self.view.stroke_moved.connect(self._stroke_move)
        self.view.stroke_ended.connect(self._stroke_end)
        layout.addWidget(self.view, 1)

        # Auffrischen
        fade = QHBoxLayout()
        fade.addWidget(QLabel("Farben auffrischen"))
        self.fade_slider = QSlider(Qt.Orientation.Horizontal)
        self.fade_slider.setRange(0, 100)
        self.fade_slider.setValue(int(self._existing_fade() * 100))
        self.fade_slider.valueChanged.connect(self._fade_changed)
        fade.addWidget(self.fade_slider, 1)
        self.fade_label = QLabel("")
        self.fade_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.fade_label.setMinimumWidth(180)
        fade.addWidget(self.fade_label)

        self.neutral_box = QCheckBox("Farbstich entfernen")
        self.neutral_box.setChecked(True)
        self.neutral_box.toggled.connect(lambda _v: self._fade_changed(
            self.fade_slider.value()))
        fade.addWidget(self.neutral_box)

        self.auto_button = QPushButton("Vorschlagen")
        self.auto_button.setToolTip(
            "Schätzt anhand des Tonwertumfangs, wie stark das Bild verblasst ist"
        )
        self.auto_button.clicked.connect(self._suggest_fade)
        fade.addWidget(self.auto_button)
        layout.addLayout(fade)

        # Fußzeile
        footer = QHBoxLayout()
        self.status = QLabel("Wird geladen …")
        self.status.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        footer.addWidget(self.status, 1)

        self.compare_button = QPushButton("Vorher zeigen")
        self.compare_button.setCheckable(True)
        self.compare_button.toggled.connect(self._toggle_before)
        footer.addWidget(self.compare_button)

        export = QPushButton("Als Datei speichern …")
        export.clicked.connect(self._export)
        footer.addWidget(export)

        close = QPushButton("Fertig")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    # -- Bild ----------------------------------------------------------

    def _load_image(self) -> None:
        image = previews.decode(self.path, previews.SCREEN_EDGE)
        if image is None or image.isNull():
            self.status.setText("Bild konnte nicht geladen werden.")
            return
        self._original = edits_mod.qimage_to_array(image)
        self._render()

    def _render(self) -> None:
        if self._original is None:
            return
        if self._show_before:
            result = self._original
        else:
            result = self.stack.apply(self._original)
        self.view.set_image(edits_mod.array_to_qimage(result))
        self._update_status()

    def _update_status(self) -> None:
        engine = "OpenCV (Telea)" if retouch.HAVE_CV2 else "eigenes Ausbreiten"
        parts = [f"{len(self.stack)} Schritt(e)"]
        if self.stack:
            parts.append(self.stack.steps[-1].label())
        parts.append(f"Risse: {engine}")
        self.status.setText("  ·  ".join(parts))
        self.undo_button.setEnabled(bool(self.stack))

    # -- Werkzeuge -----------------------------------------------------

    def _set_tool(self, kind: str) -> None:
        self._tool = kind
        self.view.set_stroke_mode(kind == STROKE)

    def _brush_changed(self, value: int) -> None:
        self._brush = value
        self.brush_label.setText(f"{value} px")
        self.view.set_brush(value)

    def _resize_brush(self, delta: int) -> None:
        self.brush_slider.setValue(self.brush_slider.value() + delta)

    def _relative(self, x: float, y: float) -> tuple[float, float]:
        image = self.view.image_size()
        if not image:
            return 0.0, 0.0
        return x / image[0], y / image[1]

    def _relative_radius(self) -> float:
        image = self.view.image_size()
        if not image:
            return 0.02
        # Der Pinsel ist in Bildschirmpunkten gedacht; hier auf die
        # kürzere Bildkante umgerechnet, damit er beim Ausgeben in
        # voller Größe gleich groß bleibt.
        return (self._brush / 2.0) / min(image)

    def _on_click(self, x: float, y: float) -> None:
        if self._original is None or self._tool == STROKE:
            return
        rx, ry = self._relative(x, y)
        self.stack.add(Step(kind=self._tool, x=rx, y=ry,
                            radius=self._relative_radius()))
        self._render()

    def _stroke_start(self, x: float, y: float) -> None:
        self._stroke = [(x, y)]

    def _stroke_move(self, x: float, y: float) -> None:
        if self._stroke:
            self._stroke.append((x, y))

    def _stroke_end(self) -> None:
        if len(self._stroke) < 2 or self._original is None:
            self._stroke = []
            return
        points = [list(self._relative(x, y)) for x, y in self._stroke]
        self._stroke = []
        self.stack.add(Step(kind=STROKE, points=points,
                            radius=self._relative_radius()))
        self._render()

    # -- Auffrischen ---------------------------------------------------

    def _existing_fade(self) -> float:
        for step in self.stack.steps:
            if step.kind == FADED:
                return step.strength
        return 0.0

    def _fade_changed(self, value: int) -> None:
        strength = value / 100.0
        self.stack.steps = [s for s in self.stack.steps if s.kind != FADED]
        if strength > 0:
            self.stack.add(Step(kind=FADED, strength=strength,
                                neutralise=self.neutral_box.isChecked()))
        self.fade_label.setText(
            f"{value} %" if value else "aus"
        )
        self._render()

    def _suggest_fade(self) -> None:
        if self._original is None:
            return
        value = int(round(retouch.auto_faded_strength(self._original) * 100))
        self.fade_slider.setValue(value)
        self.fade_label.setText(
            f"{value} % (vorgeschlagen)" if value else "nicht nötig"
        )

    # -- Sonstiges -----------------------------------------------------

    def _undo(self) -> None:
        removed = self.stack.undo()
        if removed and removed.kind == FADED:
            self.fade_slider.blockSignals(True)
            self.fade_slider.setValue(0)
            self.fade_slider.blockSignals(False)
            self.fade_label.setText("aus")
        self._render()

    def _reset(self) -> None:
        if not self.stack:
            return
        self.stack.clear()
        self.fade_slider.blockSignals(True)
        self.fade_slider.setValue(0)
        self.fade_slider.blockSignals(False)
        self.fade_label.setText("aus")
        self._render()

    def _toggle_before(self, on: bool) -> None:
        self._show_before = on
        self.compare_button.setText("Nachher zeigen" if on else "Vorher zeigen")
        self._render()

    def _export(self) -> None:
        """Fertiges Bild ausgeben - in voller Auflösung, neu gerechnet."""
        if not self.stack:
            QMessageBox.information(self, "Nichts zu speichern",
                                    "Es sind keine Schritte gesetzt.")
            return
        source = Path(self.path)
        suggestion = str(source.with_name(source.stem + "_retuschiert.jpg"))
        target, _ = QFileDialog.getSaveFileName(
            self, "Bild speichern", suggestion, "JPEG (*.jpg);;PNG (*.png)")
        if not target:
            return

        self.status.setText("Wird in voller Auflösung gerechnet …")
        self.status.repaint()

        full = previews.decode(self.path, None)
        if full is None or full.isNull():
            QMessageBox.warning(self, "Fehler",
                                "Das Bild konnte nicht geladen werden.")
            return
        result = self.stack.apply(edits_mod.qimage_to_array(full))
        image = edits_mod.array_to_qimage(result)
        if image.save(target, quality=95):
            self.status.setText(f"Gespeichert: {Path(target).name}")
        else:
            QMessageBox.warning(self, "Fehler",
                                "Die Datei konnte nicht geschrieben werden.")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.saved.emit(self.path)
        super().closeEvent(event)


class _CanvasView(ImageView):
    """Bildansicht, die Klicks und Striche in Bildkoordinaten meldet."""

    clicked = pyqtSignal(float, float)
    stroke_started = pyqtSignal(float, float)
    stroke_moved = pyqtSignal(float, float)
    stroke_ended = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stroke_mode = False
        self._drawing = False
        self._brush = 26
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_stroke_mode(self, on: bool) -> None:
        self._stroke_mode = on

    def set_brush(self, size: int) -> None:
        self._brush = size

    def image_size(self):
        pixmap = self._item.pixmap()
        if pixmap.isNull():
            return None
        return pixmap.width(), pixmap.height()

    # Die Zoom-Bedienung der Elternklasse würde hier stören: ein Klick
    # soll retuschieren, nicht auf 100 % springen.
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.has_image():
            return
        point = self.mapToScene(event.position().toPoint())
        if self._stroke_mode:
            self._drawing = True
            self.stroke_started.emit(point.x(), point.y())
        else:
            self.clicked.emit(point.x(), point.y())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drawing:
            point = self.mapToScene(event.position().toPoint())
            self.stroke_moved.emit(point.x(), point.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drawing:
            self._drawing = False
            self.stroke_ended.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        event.accept()      # kein Vollbild im Retuschefenster
