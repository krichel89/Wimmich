"""Bildansicht mit Einpassung, Zoom und Verschieben.

Portiert aus Cammellos culling_view.CullImageView (PyQt5 → PyQt6). Das
Bedienmuster ist bewusst identisch, damit die Handgriffe aus Cammello
hier weiter sitzen:

  Klick        einpassen ⇄ 100 %, verankert an der Mausposition
  Ziehen       verschieben, solange gezoomt ist
  Doppelklick  Vollbild an/aus
  Mausrad      stufenlos zoomen
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QLabel,
)

from . import theme


class ImageView(QGraphicsView):
    """Zeigt ein Bild einpassend oder gezoomt."""

    zoom_requested = pyqtSignal()        # Klick: die volle Ebene wird gebraucht
    zoom_changed = pyqtSignal(float)     # aktueller Faktor, 1.0 = 100 %
    fullscreen_requested = pyqtSignal()

    MIN_ZOOM, MAX_ZOOM = 0.05, 8.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setScene(self._scene)

        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing
        )
        self.setBackgroundBrush(QColor(theme.VIEWER_BG))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # Tasten behandelt das Fenster

        self._fit = True
        self._source: QImage | None = None
        self._press_pos = None

        # Statusanzeige unten rechts (Sterne, Farbe) - im Vollbild ist
        # die Statuszeile des Fensters nicht sichtbar.
        self.overlay = _overlay_label(self, 16)
        # Bildangaben oben links, mit i umschaltbar.
        self.info_overlay = _overlay_label(self, 14)

    # -- Inhalt --------------------------------------------------------

    def set_image(self, image: QImage, keep_view: bool = False) -> None:
        """Bild anzeigen.

        keep_view=True tauscht nur die Pixel aus, ohne Zoom und Position
        zurückzusetzen - das passiert, wenn eine andere Auflösung
        nachträglich eintrifft.

        Wichtig dabei: kommt das Bild in ANDERER Pixelgröße (beim
        Reglerziehen wird auf einer verkleinerten Fassung gerechnet),
        muss die Ansicht nachgezogen werden. Sonst springt das Bild
        sichtbar in der Größe, weil die Vergrößerung dieselbe bleibt,
        die Pixelzahl aber nicht.
        """
        before = self._item.pixmap()
        old_width = before.width()

        self._source = image
        pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
        self._item.setPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))

        if not keep_view:
            self.fit()
            return
        if not old_width or not pixmap.width() or old_width == pixmap.width():
            return

        if self._fit:
            # Eingepasst: schlicht neu einpassen, das Ergebnis sieht gleich aus
            self.fit()
        else:
            # Gezoomt: die Vergrößerung um das Größenverhältnis nachziehen,
            # damit derselbe Bildausschnitt gleich groß stehen bleibt.
            centre = self.mapToScene(self.viewport().rect().center())
            ratio = old_width / pixmap.width()
            scale = pixmap.width() / old_width
            self.set_zoom(self.zoom_factor() * ratio,
                          anchor=QPointF(centre.x() * scale,
                                         centre.y() * scale))

    def has_image(self) -> bool:
        return self._source is not None and not self._item.pixmap().isNull()

    def clear_image(self) -> None:
        self._source = None
        self._item.setPixmap(QPixmap())
        self._fit = True

    # -- Zoom ----------------------------------------------------------

    @property
    def is_fit(self) -> bool:
        return self._fit

    def zoom_factor(self) -> float:
        return self.transform().m11()

    def _ratios(self):
        pixmap = self._item.pixmap()
        if pixmap.isNull() or not pixmap.width() or not pixmap.height():
            return None
        viewport = self.viewport()
        return (viewport.width() / pixmap.width(),
                viewport.height() / pixmap.height())

    def fit_factor(self) -> float:
        ratios = self._ratios()
        return min(ratios) if ratios else 1.0

    def fit(self) -> None:
        self._fit = True
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.zoom_factor())

    def set_zoom(self, factor: float, anchor=None) -> None:
        """Stufenloser Zoom. 1.0 bedeutet 1:1 in Pixeln."""
        factor = max(self.MIN_ZOOM, min(self.MAX_ZOOM, factor))
        self._fit = False
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        center = (anchor if anchor is not None
                  else self.mapToScene(self.viewport().rect().center()))
        self.resetTransform()
        self.scale(factor, factor)
        self.centerOn(center)
        self.zoom_changed.emit(factor)

    def zoom_step(self, direction: int) -> None:
        """Ein Schritt wie in Lightroom (Faktor 1,25)."""
        self.set_zoom(self.zoom_factor() * (1.25 ** direction))

    def zoom_100(self, anchor=None) -> None:
        self.set_zoom(1.0, anchor)

    def toggle_zoom(self, anchor=None) -> None:
        if self._fit:
            self.zoom_100(anchor)
        else:
            self.fit()

    # -- Overlays ------------------------------------------------------

    def set_overlay(self, html: str) -> None:
        self.overlay.setText(html)
        self.overlay.adjustSize()
        self._place_overlay()

    def show_overlay(self, on: bool) -> None:
        self.overlay.setVisible(on)
        if on:
            self._place_overlay()

    def _place_overlay(self) -> None:
        margin = 14
        self.overlay.move(self.width() - self.overlay.width() - margin,
                          self.height() - self.overlay.height() - margin)

    def set_info_overlay(self, html: str) -> None:
        self.info_overlay.setText(html)
        self.info_overlay.adjustSize()
        self.info_overlay.move(14, 14)

    def show_info_overlay(self, on: bool) -> None:
        self.info_overlay.setVisible(on)
        if on:
            self.info_overlay.move(14, 14)

    # -- Ereignisse ----------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit:
            self.fit()
        self._place_overlay()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.has_image():
            return
        delta = event.angleDelta().y()
        if not delta:
            return
        anchor = self.mapToScene(event.position().toPoint())
        if self._fit:
            self.zoom_requested.emit()
            self.set_zoom(self.fit_factor() * (1.25 if delta > 0 else 0.8), anchor)
        else:
            self.set_zoom(self.zoom_factor() * (1.25 if delta > 0 else 0.8), anchor)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton and self._fit
                and self.has_image()):
            # Klick im eingepassten Zustand: auf 100 % an der Klickstelle.
            # Das Fenster schiebt gleich darauf die volle Ebene nach.
            self.zoom_requested.emit()
            self.toggle_zoom(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and not self._fit:
            # Im Zoom: Klick ohne Ziehen schaltet zurück (siehe Release).
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton and not self._fit
                and self._press_pos is not None
                and (event.position().toPoint()
                     - self._press_pos).manhattanLength() < 4):
            self.fit()
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # Qt liefert den zweiten Druck als dieses Ereignis, der erste
            # hat aber schon auf 100 % gezoomt. Das nehmen wir zurück,
            # damit das Vollbild eingepasst beginnt.
            if not self._fit:
                self.fit()
            self.fullscreen_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _overlay_label(parent: QGraphicsView, size: int) -> QLabel:
    label = QLabel(parent)
    label.setStyleSheet(
        "background: rgba(0, 0, 0, 165); color: white;"
        f"padding: 6px 12px; border-radius: 6px; font-size: {size}px;"
    )
    label.setTextFormat(Qt.TextFormat.RichText)
    label.hide()
    return label
