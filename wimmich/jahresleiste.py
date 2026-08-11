"""Schmale Jahresleiste rechts neben dem Raster.

Bei mehreren zehntausend Aufnahmen ist die gewoehnliche Bildlaufleiste
kein Orientierungspunkt mehr: man weiss beim Ziehen nicht, wo im Jahr
man landet. Diese Leiste zeigt die Jahre, die in der aktuellen Ansicht
ueberhaupt vorkommen, und springt auf Klick dorthin.

Seit 0.3.33 sind auch die MONATE anfahrbar. Sie stehen als Spiegel-
striche zwischen den Jahreszahlen - ohne Beschriftung, sonst waere die
Leiste bei zwanzig Jahren eine Bleiwueste. Was ein Strich bedeutet,
sagt der Tooltip beim Ueberfahren.

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
MONAT_HOEHE = 9         # Hoehe eines Monatsstrichs
STRICH_LAENGE = 12      # Laenge des Spiegelstrichs

MONATSNAMEN = ["Januar", "Februar", "März", "April", "Mai", "Juni",
               "Juli", "August", "September", "Oktober", "November",
               "Dezember"]


def monatstitel(schluessel: str) -> str:
    """'2026-08' -> 'August 2026'. Unbekanntes bleibt, wie es ist."""
    teile = str(schluessel).split("-")
    if len(teile) != 2 or not teile[1].isdigit():
        return str(schluessel)
    nummer = int(teile[1])
    if not 1 <= nummer <= 12:
        return str(schluessel)
    return f"{MONATSNAMEN[nummer - 1]} {teile[0]}"


class Jahresleiste(QWidget):
    """Zeigt Jahre und Monate der Ansicht; ein Klick springt dorthin."""

    # Der Wert ist entweder ein Jahr ('2026') oder ein Monat ('2026-08').
    jahr_gewaehlt = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(BREITE)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._jahre: list[tuple[str, int]] = []
        self._monate: dict[str, list[tuple[str, int]]] = {}
        self._aktuell = ""
        self._unter_maus = -1

    # -- Inhalt --------------------------------------------------------

    def setze_jahre(self, jahre: list[tuple[str, int]],
                    monate: list[tuple[str, int]] | None = None) -> None:
        """jahre ist (Jahr, Anzahl), monate ist ('JJJJ-MM', Anzahl).

        Beides schon in Anzeigereihenfolge. Monate ohne passendes Jahr
        fallen weg - sie haetten in der Leiste keinen Platz.
        """
        jahre = [(str(j), int(n)) for j, n in jahre if j]
        gruppen: dict[str, list[tuple[str, int]]] = {j: [] for j, _ in jahre}
        for schluessel, anzahl in (monate or []):
            jahr = str(schluessel)[:4]
            if jahr in gruppen:
                gruppen[jahr].append((str(schluessel), int(anzahl)))
        # Die Monate folgen der Richtung der Jahre (auf- oder absteigend)
        absteigend = len(jahre) > 1 and jahre[0][0] > jahre[-1][0]
        for jahr in gruppen:
            gruppen[jahr].sort(key=lambda m: m[0], reverse=absteigend)

        if jahre != self._jahre or gruppen != self._monate:
            self._jahre = jahre
            self._monate = gruppen
            self._unter_maus = -1
            self.update()
        self.setVisible(len(self._jahre) > 1)

    def jahre(self) -> list[str]:
        return [j for j, _ in self._jahre]

    def monate(self, jahr: str) -> list[str]:
        return [m for m, _ in self._monate.get(str(jahr), [])]

    def setze_aktuell(self, schluessel: str) -> None:
        """Hervorhebung setzen. Ein Monat hebt auch sein Jahr hervor."""
        if schluessel != self._aktuell:
            self._aktuell = schluessel or ""
            self.update()

    # -- Aufteilung ----------------------------------------------------

    def _eintraege(self) -> list[tuple[str, bool, int]]:
        """Alle Zeilen der Reihe nach: (Schluessel, ist_Jahr, Anzahl)."""
        zeilen: list[tuple[str, bool, int]] = []
        for jahr, anzahl in self._jahre:
            zeilen.append((jahr, True, anzahl))
            for monat, manzahl in self._monate.get(jahr, []):
                zeilen.append((monat, False, manzahl))
        return zeilen

    def _felder(self) -> list[tuple[str, bool, QRect]]:
        """Felder von oben nach unten.

        Die Jahre bekommen die volle Zeilenhoehe, die Monate den
        schmalen Streifen. Passt nicht alles hin, fallen die MONATE
        weg - die Jahre muessen immer erreichbar bleiben.
        """
        zeilen = self._eintraege()
        if not zeilen:
            return []
        jahre = sum(1 for _s, ist_jahr, _a in zeilen if ist_jahr)
        monate = len(zeilen) - jahre

        if jahre * MIN_HOEHE + monate * MONAT_HOEHE > self.height():
            zeilen = [z for z in zeilen if z[1]]
            monate = 0

        # Restplatz gleichmaessig auf die Jahresfelder verteilen
        frei = max(0, self.height() - (jahre * MIN_HOEHE + monate * MONAT_HOEHE))
        jahr_hoehe = MIN_HOEHE + frei // max(1, jahre)

        felder: list[tuple[str, bool, QRect]] = []
        y = 0
        for schluessel, ist_jahr, _anzahl in zeilen:
            hoehe = jahr_hoehe if ist_jahr else MONAT_HOEHE
            felder.append((schluessel, ist_jahr,
                           QRect(0, y, self.width(), hoehe)))
            y += hoehe
        return felder

    # -- Zeichnen ------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 2.0))
        painter.setFont(font)

        aktuelles_jahr = self._aktuell[:4]
        for i, (schluessel, ist_jahr, feld) in enumerate(self._felder()):
            gewaehlt = (schluessel == self._aktuell
                        or (ist_jahr and schluessel == aktuelles_jahr))
            if gewaehlt:
                painter.fillRect(feld.adjusted(3, 1, -3, -1),
                                 QColor(theme.ACCENT_DIM))
            elif i == self._unter_maus:
                painter.fillRect(feld.adjusted(3, 1, -3, -1),
                                 QColor(theme.HOVER))

            if ist_jahr:
                painter.setPen(QPen(QColor(
                    theme.TEXT if gewaehlt else theme.TEXT_MUTED)))
                painter.drawText(feld, int(Qt.AlignmentFlag.AlignCenter),
                                 schluessel)
            else:
                # Monat: nur ein Spiegelstrich, keine Beschriftung
                farbe = QColor(theme.TEXT if gewaehlt else theme.TEXT_MUTED)
                farbe.setAlpha(255 if gewaehlt else 150)
                painter.setPen(QPen(farbe, 1.6))
                mitte = feld.center().y()
                links = (feld.width() - STRICH_LAENGE) // 2
                painter.drawLine(links, mitte, links + STRICH_LAENGE, mitte)
        painter.end()

    # -- Maus ----------------------------------------------------------

    def _treffer(self, y: int) -> int:
        for i, (_s, _ist_jahr, feld) in enumerate(self._felder()):
            if feld.top() <= y <= feld.bottom():
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        treffer = self._treffer(int(event.position().y()))
        if treffer == self._unter_maus:
            return
        self._unter_maus = treffer
        felder = self._felder()
        if 0 <= treffer < len(felder):
            schluessel, ist_jahr, _feld = felder[treffer]
            anzahlen = {s: a for s, _i, a in self._eintraege()}
            name = schluessel if ist_jahr else monatstitel(schluessel)
            self.setToolTip(f"{name}: {anzahlen.get(schluessel, 0)} Aufnahmen")
        self.update()

    def leaveEvent(self, event) -> None:
        self._unter_maus = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        felder = self._felder()
        treffer = self._treffer(int(event.position().y()))
        if 0 <= treffer < len(felder):
            self.jahr_gewaehlt.emit(felder[treffer][0])
