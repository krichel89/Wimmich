"""Startbild, das die lange Anlaufzeit überbrückt.

Der Aufbau des Hauptfensters dauert: Datenbank öffnen, ggf. migrieren,
Ordnerbaum aufbauen, exiftool starten. Bis dahin passierte auf dem
Bildschirm nichts - das sieht aus, als wäre nichts angekommen.

Bewusst OHNE mitgelieferte Bilddatei: das Logo steckt als Base64 in
icon.py. Eine Datei daneben müsste im gebauten Programm über _MEIPASS
gesucht werden, und genau daran ist 0.3.17 beim Start gescheitert.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from . import theme
from .icon import app_icon

BREITE, HOEHE = 460, 260
LOGO = 128


def _hintergrund() -> QPixmap:
    """Fläche mit Logo und Beschriftung zeichnen."""
    pixmap = QPixmap(BREITE, HOEHE)
    pixmap.fill(QColor(theme.PANEL))

    maler = QPainter(pixmap)
    maler.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    maler.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    maler.setPen(QColor(theme.BORDER))
    maler.drawRect(0, 0, BREITE - 1, HOEHE - 1)

    logo = app_icon().pixmap(LOGO, LOGO)
    if not logo.isNull():
        maler.drawPixmap((BREITE - LOGO) // 2, 24, logo)

    from . import __version__, APP_NAME

    schrift = QFont()
    schrift.setPixelSize(26)
    schrift.setBold(True)
    maler.setFont(schrift)
    maler.setPen(QColor(theme.TEXT))
    maler.drawText(QRect(0, 24 + LOGO + 10, BREITE, 34),
                   int(Qt.AlignmentFlag.AlignCenter),
                   f"{APP_NAME} {__version__}")

    schrift.setPixelSize(15)
    schrift.setBold(False)
    maler.setFont(schrift)
    maler.setPen(QColor(theme.TEXT_MUTED))
    maler.drawText(QRect(0, 24 + LOGO + 46, BREITE, 24),
                   int(Qt.AlignmentFlag.AlignCenter),
                   "by Harald Krichel")
    maler.end()
    return pixmap


class Startbild(QSplashScreen):
    """Startbild mit Sanduhr und einer Zeile für den Stand."""

    def __init__(self) -> None:
        super().__init__(_hintergrund())
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._sanduhr = False

    def melde(self, text: str) -> None:
        """Eine Zeile unten anzeigen und sofort zeichnen lassen."""
        self.showMessage(
            text,
            int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter),
            QColor(theme.TEXT_MUTED))
        QApplication.processEvents()

    def starte(self, text: str = "wird gestartet …") -> None:
        self.show()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._sanduhr = True
        self.melde(text)

    def fertig(self, fenster) -> None:
        """Sanduhr zurücknehmen und das Startbild schließen.

        Die Sanduhr wird GENAU EINMAL zurückgenommen - ein zweiter Aufruf
        von restoreOverrideCursor() würde einen Cursor abräumen, den
        jemand anders gesetzt hat.
        """
        if self._sanduhr:
            QApplication.restoreOverrideCursor()
            self._sanduhr = False
        self.finish(fenster)
