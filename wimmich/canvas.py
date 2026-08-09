"""Bildfläche der Lupenansicht.

Baut auf ImageView auf und ergänzt, was zum Bearbeiten gebraucht wird:
Klicks und Striche in Bildkoordinaten, den Zuschnittrahmen und die
Pipette. Früher steckte das im Bearbeitungsfenster; seit alles in einem
Fenster läuft, ist es ein eigenes Bauteil.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen

from .edits import CROP, RED_EYE, SPOT, STROKE
from .imageview import ImageView

PIPETTE = "pipette"
NONE = "none"


class CanvasView(ImageView):
    """Bildansicht, die Klicks, Striche, Zuschnitt und Pipette meldet."""

    clicked = pyqtSignal(float, float)
    stroke_started = pyqtSignal(float, float)
    stroke_moved = pyqtSignal(float, float)
    stroke_ended = pyqtSignal()
    crop_changed = pyqtSignal(float, float, float, float)
    picked = pyqtSignal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mode = NONE
        self._drawing = False
        self._crop: tuple[float, float, float, float] | None = None
        self._crop_drag: tuple[float, float] | None = None
        self._crop_now: QRectF | None = None
        self._aspect: float | None = None

    # -- Zustand -------------------------------------------------------

    def set_mode(self, kind: str) -> None:
        self._mode = kind
        self._crop_now = None
        self.setCursor(
            Qt.CursorShape.ArrowCursor if kind == NONE
            else Qt.CursorShape.CrossCursor
        )
        self.viewport().update()

    def mode(self) -> str:
        return self._mode

    def set_crop(self, rect: tuple[float, float, float, float] | None) -> None:
        self._crop = rect
        self.viewport().update()

    def set_aspect(self, ratio: float | None) -> None:
        """Seitenverhältnis für den Zuschnitt; None heißt frei."""
        self._aspect = ratio

    def image_size(self):
        pixmap = self._item.pixmap()
        if pixmap.isNull():
            return None
        return pixmap.width(), pixmap.height()

    # -- Rahmen zeichnen -----------------------------------------------

    def drawForeground(self, painter: QPainter, rect) -> None:  # noqa: N802
        """Zuschnitt als Rahmen über dem ganzen Bild.

        Das Bild bleibt ungeschnitten sichtbar - sonst würden Retuschen,
        die danach gesetzt werden, an falscher Stelle landen.
        """
        size = self.image_size()
        if not size:
            return
        width, height = size

        area = self._crop_now
        if area is None and self._crop:
            x, y, w, h = self._crop
            area = QRectF(x * width, y * height, w * width, h * height)
        if area is None:
            return

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
        for part in _outside(QRectF(0, 0, width, height), area):
            painter.drawRect(part)

        pen = QPen(QColor(255, 255, 255, 220))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(area)

        pen.setColor(QColor(255, 255, 255, 90))
        painter.setPen(pen)
        for i in (1, 2):
            x = area.left() + area.width() * i / 3
            y = area.top() + area.height() * i / 3
            painter.drawLine(int(x), int(area.top()), int(x), int(area.bottom()))
            painter.drawLine(int(area.left()), int(y), int(area.right()), int(y))
        painter.restore()

    # -- Maus ----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.has_image():
            return
        point = self.mapToScene(event.position().toPoint())

        if self._mode == CROP:
            self._crop_drag = (point.x(), point.y())
            self._crop_now = QRectF(point, point)
        elif self._mode == PIPETTE:
            self.picked.emit(point.x(), point.y())
        elif self._mode == STROKE:
            self._drawing = True
            self.stroke_started.emit(point.x(), point.y())
        elif self._mode in (SPOT, RED_EYE):
            self.clicked.emit(point.x(), point.y())
        else:
            # Kein Werkzeug aktiv: die Zoom-Bedienung der Elternklasse
            super().mousePressEvent(event)
            return
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        point = self.mapToScene(event.position().toPoint())
        if self._crop_drag is not None:
            self._crop_now = self._rect_from(point)
            self.viewport().update()
            event.accept()
            return
        if self._drawing:
            self.stroke_moved.emit(point.x(), point.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _rect_from(self, point) -> QRectF:
        x0, y0 = self._crop_drag
        width = abs(point.x() - x0)
        height = abs(point.y() - y0)
        if self._aspect:
            # Am längeren Zug ausrichten, damit das Ziehen nicht springt
            if width / max(height, 1e-6) > self._aspect:
                height = width / self._aspect
            else:
                width = height * self._aspect
        left = x0 if point.x() >= x0 else x0 - width
        top = y0 if point.y() >= y0 else y0 - height
        return QRectF(left, top, width, height)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._crop_drag is not None:
            self._crop_drag = None
            size = self.image_size()
            area, self._crop_now = self._crop_now, None
            if area is not None and size:
                width, height = size
                x = max(0.0, area.left() / width)
                y = max(0.0, area.top() / height)
                w = min(1.0 - x, area.width() / width)
                h = min(1.0 - y, area.height() / height)
                self.crop_changed.emit(x, y, w, h)
            event.accept()
            return
        if self._drawing:
            self._drawing = False
            self.stroke_ended.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._mode != NONE:
            event.accept()      # beim Bearbeiten kein Vollbild
            return
        super().mouseDoubleClickEvent(event)


def _outside(full: QRectF, inner: QRectF) -> list[QRectF]:
    """Die vier Streifen rings um den Zuschnitt - zum Abdunkeln."""
    inner = inner.intersected(full)
    return [
        QRectF(full.left(), full.top(), full.width(), inner.top() - full.top()),
        QRectF(full.left(), inner.bottom(),
               full.width(), full.bottom() - inner.bottom()),
        QRectF(full.left(), inner.top(),
               inner.left() - full.left(), inner.height()),
        QRectF(inner.right(), inner.top(),
               full.right() - inner.right(), inner.height()),
    ]
