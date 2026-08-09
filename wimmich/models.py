"""Modell und Zeichenroutine für die Rasteransicht."""

from __future__ import annotations

from PyQt6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QRect, QRectF, QRunnable, QSize,
    Qt, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from pathlib import Path

from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from . import marks, theme, thumbs

ROLE_PATH = Qt.ItemDataRole.UserRole + 1
ROLE_ID = Qt.ItemDataRole.UserRole + 2
ROLE_RATING = Qt.ItemDataRole.UserRole + 3
ROLE_ROW = Qt.ItemDataRole.UserRole + 4
ROLE_STACK = Qt.ItemDataRole.UserRole + 5
ROLE_LABEL = Qt.ItemDataRole.UserRole + 6
ROLE_EDITED = Qt.ItemDataRole.UserRole + 7
ROLE_HEADER = Qt.ItemDataRole.UserRole + 8


class _ThumbSignals(QObject):
    done = pyqtSignal(int, str)   # Zeilennummer, Cache-Pfad
    fail = pyqtSignal(int)


class _ThumbTask(QRunnable):
    def __init__(self, row: int, path: str, mtime: float, filesize: int,
                 edge: int, exiftool, signals: _ThumbSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._args = (row, path, mtime, filesize, edge, exiftool)
        self._signals = signals

    def run(self) -> None:
        row, path, mtime, filesize, edge, exiftool = self._args
        result = thumbs.get_thumbnail(path, mtime, filesize, edge, exiftool)
        if result is None:
            self._signals.fail.emit(row)
        else:
            self._signals.done.emit(row, str(result))


class PhotoModel(QAbstractListModel):
    """Haelt die Ergebnisliste einer Abfrage und lädt Vorschauen nach."""

    def __init__(self, exiftool=None, thumb_edge: int = 256, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._pixmaps: dict[int, QPixmap] = {}
        self._requested: set[int] = set()
        self._thumb_edge = thumb_edge
        self._exiftool = exiftool

        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.done.connect(self._thumb_ready)
        self._signals.fail.connect(self._thumb_failed)

        self._placeholder = QPixmap()   # leer = Delegate zeichnet Platzhalter
        self._edited: set[str] = set()

    # -- Daten ---------------------------------------------------------

    def set_rows(self, rows, group_by_folder: bool = False) -> None:
        """Bilder anzeigen.

        group_by_folder=True fügt vor jedem Ordnerwechsel eine Kopfzeile
        ein - die Picasa-Ansicht, in der alle Ordner untereinander
        stehen. Kopfzeilen sind ganz normale Zeilen mit dem Schlüssel
        "_header"; die Zeichenroutine erkennt sie daran.
        """
        prepared: list[dict] = []
        letzter = None
        for row in rows:
            item = dict(row)
            if group_by_folder and item.get("folder") != letzter:
                letzter = item.get("folder")
                prepared.append({"_header": letzter, "filename": "",
                                 "path": "", "id": -1})
            prepared.append(item)

        self.beginResetModel()
        self._rows = prepared
        self._pixmaps.clear()
        self._requested.clear()
        self.endResetModel()

    def photo_rows(self) -> list[int]:
        """Zeilennummern, die wirklich Bilder sind (ohne Kopfzeilen)."""
        return [i for i, r in enumerate(self._rows) if "_header" not in r]

    def is_header(self, row: int) -> bool:
        item = self.row_data(row)
        return bool(item and "_header" in item)

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt-Namen)
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._rows):
            return None
        item = self._rows[row]

        if "_header" in item:
            if role == ROLE_HEADER:
                return item["_header"]
            if role == Qt.ItemDataRole.DisplayRole:
                return item["_header"]
            return None

        if role == ROLE_HEADER:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return item["filename"]
        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmap_for(row, item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _tooltip(item)
        if role == ROLE_PATH:
            return item["path"]
        if role == ROLE_ID:
            return item["id"]
        if role == ROLE_RATING:
            return item.get("rating") or 0
        if role == ROLE_STACK:
            return _stack_label(item)
        if role == ROLE_LABEL:
            return marks.label_color(item.get("label"))
        if role == ROLE_EDITED:
            return (item.get("thumb_path") or item["path"]) in self._edited
        if role == ROLE_ROW:
            return item
        return None

    def flags(self, index):  # noqa: N802
        """Kopfzeilen sind nicht auswählbar."""
        base = super().flags(index)
        if index.isValid() and self.is_header(index.row()):
            return Qt.ItemFlag.NoItemFlags
        return base

    def update_rating(self, row: int, rating: int) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row]["rating"] = rating
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [ROLE_RATING, Qt.ItemDataRole.DisplayRole])

    def set_edited(self, paths: set[str]) -> None:
        """Pfade mit Retusche - für das Abzeichen auf der Kachel."""
        if paths != self._edited:
            self._edited = set(paths)
            if self._rows:
                self.dataChanged.emit(
                    self.index(0, 0), self.index(len(self._rows) - 1, 0),
                    [ROLE_EDITED],
                )

    def update_label(self, row: int, label: str) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row]["label"] = label
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [ROLE_LABEL, Qt.ItemDataRole.DisplayRole])

    # -- Vorschauen ----------------------------------------------------

    def _pixmap_for(self, row: int, item: dict) -> QPixmap:
        pixmap = self._pixmaps.get(row)
        if pixmap is not None:
            return pixmap
        if row not in self._requested:
            self._requested.add(row)
            # thumb_path zeigt bei Stapeln auf das JPEG - das spart das
            # Entwickeln der RAW-Datei für die Kachel
            self._pool.start(_ThumbTask(
                row,
                item.get("thumb_path") or item["path"],
                item.get("thumb_mtime") or item.get("mtime") or 0.0,
                item.get("thumb_size") or item.get("filesize") or 0,
                self._thumb_edge, self._exiftool, self._signals,
            ))
        return self._placeholder

    def _thumb_ready(self, row: int, cache_file: str) -> None:
        self._pixmaps[row] = QPixmap(cache_file)
        self._touch(row)

    def _thumb_failed(self, row: int) -> None:
        self._pixmaps[row] = self._placeholder
        self._touch(row)

    def _touch(self, row: int) -> None:
        if row < len(self._rows):
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


class PhotoDelegate(QStyledItemDelegate):
    """Zeichnet Kachel, Dateiname, Sterne und Stapelabzeichen."""

    def __init__(self, tile: int = 180, parent=None) -> None:
        super().__init__(parent)
        self.tile = tile
        self.label_height = 40
        self.pad = 8

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        if index is not None and index.isValid() and index.data(ROLE_HEADER):
            # Volle Breite, damit die Kopfzeile eine eigene Reihe bekommt
            width = self._viewport_width(option)
            return QSize(width, 34)
        return QSize(self.tile + self.pad * 2,
                     self.tile + self.label_height + self.pad)

    @staticmethod
    def _viewport_width(option) -> int:
        widget = getattr(option, "widget", None)
        if widget is not None and widget.viewport() is not None:
            return max(200, widget.viewport().width() - 8)
        return 900

    def paint(self, painter: QPainter, option, index) -> None:
        header = index.data(ROLE_HEADER)
        if header:
            self._paint_header(painter, option, header)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        card = QRectF(rect.adjusted(3, 3, -3, -3))
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(card, theme.RADIUS + 2, theme.RADIUS + 2)
            painter.fillPath(
                path, QBrush(QColor(theme.ACCENT_DIM if selected else theme.HOVER))
            )

        image_rect = QRectF(card.x() + 5, card.y() + 5, self.tile, self.tile)
        rejected = marks.is_reject(int(index.data(ROLE_RATING) or 0))
        if rejected:
            painter.setOpacity(0.38)
        self._draw_image(painter, image_rect, index)
        painter.setOpacity(1.0)

        painter.setPen(QPen(QColor("#ffffff" if selected else theme.TEXT_MUTED)))
        font = QFont(option.font)
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
        painter.setFont(font)

        name_rect = QRect(int(card.x()) + 4, int(image_rect.bottom()) + 5,
                          int(card.width()) - 8, 15)
        elided = painter.fontMetrics().elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
            Qt.TextElideMode.ElideMiddle, name_rect.width(),
        )
        painter.drawText(name_rect, int(Qt.AlignmentFlag.AlignHCenter), elided)

        rating = int(index.data(ROLE_RATING) or 0)
        mark_rect = QRect(name_rect.x(), name_rect.bottom() + 1,
                          name_rect.width(), 14)
        if marks.is_reject(rating):
            painter.setPen(QPen(QColor("#ff6b6b")))
            painter.drawText(
                mark_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                "\u2715",
            )
        elif rating > 0:
            painter.setPen(QPen(QColor(theme.STAR)))
            painter.drawText(
                mark_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                "\u2605" * rating,
            )
        painter.restore()

    def _paint_header(self, painter: QPainter, option, text: str) -> None:
        """Ordnername als Trennzeile zwischen den Kacheln."""
        painter.save()
        rect = option.rect
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)

        painter.setPen(QPen(QColor(theme.TEXT)))
        name = Path(text).name or text
        painter.drawText(
            rect.adjusted(8, 8, -8, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )

        metrics = painter.fontMetrics()
        start = rect.x() + 16 + metrics.horizontalAdvance(name)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        painter.drawText(
            QRect(start, rect.y() + 8, max(0, rect.width() - start), rect.height() - 8),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            str(text),
        )

        pen = QPen(QColor(theme.BORDER))
        painter.setPen(pen)
        line_y = rect.bottom() - 3
        painter.drawLine(rect.x() + 8, line_y, rect.right() - 8, line_y)
        painter.restore()

    def _draw_image(self, painter: QPainter, area: QRectF, index) -> None:
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)

        if not isinstance(pixmap, QPixmap) or pixmap.isNull():
            # Ruhiger Platzhalter, solange die Vorschau noch entsteht
            path = QPainterPath()
            path.addRoundedRect(area, theme.RADIUS, theme.RADIUS)
            painter.fillPath(path, QBrush(QColor(theme.ELEVATED)))
            return

        scaled = pixmap.scaled(
            area.size().toSize(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        target = QRectF(0, 0, scaled.width(), scaled.height())
        target.moveCenter(area.center())

        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(target, theme.RADIUS, theme.RADIUS)
        painter.setClipPath(clip)
        painter.drawPixmap(target.toRect(), scaled)
        painter.restore()

        badge = index.data(ROLE_STACK)
        if badge:
            self._draw_badge(painter, target, badge)

        colour = index.data(ROLE_LABEL)
        if colour:
            self._draw_dot(painter, target, colour)

        if index.data(ROLE_EDITED):
            self._draw_edited(painter, target)

    def _draw_badge(self, painter: QPainter, image_rect: QRectF, text: str) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)

        width = painter.fontMetrics().horizontalAdvance(text) + 12
        badge = QRectF(image_rect.right() - width - 5, image_rect.top() + 5,
                       width, 16)
        path = QPainterPath()
        path.addRoundedRect(badge, 5, 5)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 170)))
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()


    def _draw_dot(self, painter: QPainter, image_rect: QRectF, colour: str) -> None:
        """Farbmarkierung oben links.

        Deutlich größer als ein Punkt: bei einem Raster voller Bilder
        muss die Farbe im Vorbeischauen erkennbar sein, nicht erst beim
        Hinsehen. Heller Ring darum, damit sie auch auf farbigem Grund
        steht.
        """
        painter.save()
        size = 22.0
        dot = QRectF(image_rect.left() + 7, image_rect.top() + 7, size, size)
        painter.setPen(QPen(QColor(255, 255, 255, 210), 2.0))
        painter.setBrush(QBrush(QColor(colour)))
        painter.drawEllipse(dot)
        painter.restore()


    def _draw_edited(self, painter: QPainter, image_rect: QRectF) -> None:
        """Zeigt an, dass für dieses Bild Retuschen gespeichert sind."""
        painter.save()
        size = 16.0
        badge = QRectF(image_rect.left() + 5, image_rect.bottom() - size - 5,
                       size, size)
        path = QPainterPath()
        path.addRoundedRect(badge, 5, 5)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 170)))
        font = QFont(painter.font())
        font.setPointSizeF(8.0)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "\u270e")
        painter.restore()


def _stack_label(item: dict) -> str:
    """Abzeichen oben rechts auf der Kachel."""
    count = int(item.get("stack_count") or 1)
    if count > 1:
        return "RAW+JPG" if count == 2 else f"RAW+{count - 1}"
    return "RAW" if item.get("is_raw") else ""


def _tooltip(item: dict) -> str:
    lines = [item["path"]]
    count = int(item.get("stack_count") or 1)
    if count > 1:
        lines.append(f"Stapel aus {count} Dateien")
    if item.get("taken_at"):
        lines.append(f"Aufnahme: {item['taken_at']}")
    if item.get("camera"):
        lines.append(f"Kamera: {item['camera']}")
    if item.get("lens"):
        lines.append(f"Objektiv: {item['lens']}")
    if item.get("width") and item.get("height"):
        lines.append(f"{item['width']} x {item['height']} px")
    if item.get("keywords"):
        lines.append(f"Stichwörter: {item['keywords']}")
    if item.get("label"):
        lines.append(f"Markierung: {item['label']}")
    return "\n".join(lines)
