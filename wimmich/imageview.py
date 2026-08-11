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

    # Zoombereich: nach unten die Einpassung bzw. 50 % - je nachdem, was
    # kleiner ist -, nach oben 200 %. Frueher waren 5 % bis 800 % erlaubt;
    # damit liess sich das Bild zu einem Punkt schrumpfen oder so weit
    # vergroessern, dass man die Orientierung verlor.
    MIN_ZOOM, MAX_ZOOM = 0.5, 2.0
    # Ein Mausrad-Rastpunkt (120 Einheiten) aendert die Vergroesserung um
    # diesen Faktor. Vorher waren es 1,25 bzw. 0,8 - bei Maeusen, die
    # mehrere Rastpunkte je Bewegung melden, sprang das Bild dadurch.
    WHEEL_STEP = 1.10

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

    def min_zoom(self) -> float:
        """Kleinste erlaubte Vergroesserung.

        Die Einpassung muss immer erreichbar bleiben - bei einem grossen
        Bild in einem kleinen Fenster liegt sie unter 50 %. Bei einem
        kleinen Bild ist 50 % die Grenze.
        """
        return min(self.MIN_ZOOM, self.fit_factor())

    def set_zoom(self, factor: float, anchor=None) -> None:
        """Stufenloser Zoom. 1.0 bedeutet 1:1 in Pixeln."""
        untergrenze = self.min_zoom()
        einpassung = self.fit_factor()
        # Bei einem Bild, das GROESSER ist als das Fenster (jedes Foto),
        # gibt es unterhalb der Einpassung nichts mehr zu sehen - dort
        # wird eingepasst statt weiter zu verkleinern. Ein kleines Bild
        # wird dagegen zum Einpassen vergroessert; dann bleiben die
        # 50 % die Untergrenze, sonst liesse es sich nie verkleinern.
        if einpassung <= 1.0 and factor <= einpassung * 1.001:
            self.fit()
            return
        factor = max(untergrenze, min(self.MAX_ZOOM, factor))
        self._fit = False
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        center = (anchor if anchor is not None
                  else self.mapToScene(self.viewport().rect().center()))
        self.resetTransform()
        self.scale(factor, factor)
        self.centerOn(self._gehalten(center, factor))
        self.zoom_changed.emit(factor)

    def _gehalten(self, center: QPointF, factor: float) -> QPointF:
        """Mittelpunkt so beschneiden, dass das Bild im Fenster bleibt.

        Ohne das wandert das Bild beim Zoomen an der Mausposition aus dem
        Sichtfenster heraus und man sucht es mit gedruecktem Mausknopf
        wieder zusammen. Passt eine Achse ganz ins Fenster, wird auf
        dieser Achse mittig gestellt.
        """
        pixmap = self._item.pixmap()
        if pixmap.isNull() or not factor:
            return center
        breite, hoehe = pixmap.width(), pixmap.height()
        sicht_b = self.viewport().width() / factor
        sicht_h = self.viewport().height() / factor

        if sicht_b >= breite:
            x = breite / 2
        else:
            x = min(max(center.x(), sicht_b / 2), breite - sicht_b / 2)
        if sicht_h >= hoehe:
            y = hoehe / 2
        else:
            y = min(max(center.y(), sicht_h / 2), hoehe - sicht_h / 2)
        return QPointF(x, y)

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
        elif self.has_image():
            # Wird das Fenster groesser, waere sonst plaetzlich Rand
            # neben dem Bild sichtbar.
            self.set_zoom(self.zoom_factor())
        self._place_overlay()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.has_image():
            return
        delta = event.angleDelta().y()
        if not delta:
            return
        anchor = self.mapToScene(event.position().toPoint())
        # Stufenlos ueber die tatsaechlich gemeldete Radbewegung: ein
        # Rastpunkt sind 120 Einheiten, ein Trackpad meldet weniger.
        schritt = self.WHEEL_STEP ** (delta / 120.0)
        if self._fit:
            self.zoom_requested.emit()
            self.set_zoom(self.fit_factor() * schritt, anchor)
        else:
            self.set_zoom(self.zoom_factor() * schritt, anchor)
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
