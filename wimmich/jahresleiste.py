"""Schmale Jahresleiste rechts neben dem Raster.

Bei mehreren zehntausend Aufnahmen ist die gewoehnliche Bildlaufleiste
kein Orientierungspunkt mehr: man weiss beim Ziehen nicht, wo im Jahr
man landet. Diese Leiste zeigt die Jahre, die in der aktuellen Ansicht
ueberhaupt vorkommen, und springt auf Klick dorthin.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from . import theme

BREITE = 46
MIN_HOEHE = 22          # kleinste Hoehe eines Jahresfeldes


class Jahresleiste(QWidget):
    """Zeigt die Jahre der Ansicht; ein Klick springt zum ersten Bild."""

    jahr_gewaehlt = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(BREITE)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._jahre: list[tuple[str, int]] = []
        self._aktuell = ""
        self._unter_maus = -1

    # -- Inhalt --------------------------------------------------------

    def setze_jahre(self, jahre: list[tuple[str, int]]) -> None:
        """jahre ist eine Liste (Jahr, Anzahl) - schon in Anzeigereihenfolge."""
        jahre = [(str(j), int(n)) for j, n in jahre if j]
        if jahre != self._jahre:
            self._jahre = jahre
            self._unter_maus = -1
            self.update()
        self.setVisible(len(self._jahre) > 1)

    def jahre(self) -> list[str]:
        return [j for j, _ in self._jahre]

    def setze_aktuell(self, jahr: str) -> None:
        if jahr != self._aktuell:
            self._aktuell = jahr or ""
            self.update()

    # -- Zeichnen ------------------------------------------------------

    def _felder(self) -> list[tuple[str, QRect]]:
        """Gleich hohe Felder, eines je Jahr - passt notfalls zusammen."""
        if not self._jahre:
            return []
        hoehe = max(MIN_HOEHE, self.height() // max(1, len(self._jahre)))
        felder = []
        for i, (jahr, _anzahl) in enumerate(self._jahre):
            felder.append((jahr, QRect(0, i * hoehe, self.width(), hoehe)))
        return felder

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 2.0))
        painter.setFont(font)

        for i, (jahr, feld) in enumerate(self._felder()):
            if jahr == self._aktuell:
                painter.fillRect(feld.adjusted(3, 1, -3, -1),
                                 QColor(theme.ACCENT_DIM))
            elif i == self._unter_maus:
                painter.fillRect(feld.adjusted(3, 1, -3, -1), QColor(theme.HOVER))
            painter.setPen(QPen(QColor(
                theme.TEXT if jahr == self._aktuell else theme.TEXT_MUTED)))
            painter.drawText(feld, int(Qt.AlignmentFlag.AlignCenter), jahr)
        painter.end()

    # -- Maus ----------------------------------------------------------

    def _treffer(self, y: int) -> int:
        for i, (_jahr, feld) in enumerate(self._felder()):
            if feld.top() <= y <= feld.bottom():
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        treffer = self._treffer(int(event.position().y()))
        if treffer != self._unter_maus:
            self._unter_maus = treffer
            if 0 <= treffer < len(self._jahre):
                jahr, anzahl = self._jahre[treffer]
                self.setToolTip(f"{jahr}: {anzahl} Aufnahmen")
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._unter_maus = -1
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        treffer = self._treffer(int(event.position().y()))
        if 0 <= treffer < len(self._jahre):
            self.jahr_gewaehlt.emit(self._jahre[treffer][0])
