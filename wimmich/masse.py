"""Reparaturlauf: falsch herum eingetragene Bildmaße richtigstellen.

Eine ältere Fassung von Wimmich hat beim Einlesen die Exif-Ausrichtung
nicht beachtet. Bei einer Aufnahme, die quer gespeichert und per
Drehmarke hochkant angezeigt wird, stand deshalb die Querformat-Größe
im Index. Sichtbar wurde das doppelt: die Kachel bekam im dichten
Raster eine viel zu breite Fläche (Balken links und rechts), und die
Metadaten zeigten die falsche Auflösung.

Ein einmal gelesener Eintrag wird nie wieder gelesen, solange sich die
Datei nicht ändert - deshalb blieb der Fehler stehen, und weil er nur
Dateien mit Drehmarke traf, sah er zufällig verteilt aus.

Dieser Lauf hält jeden Eintrag gegen die Datei und dreht nur die
Maße um. Bewertung, Marke, Aufnahmedatum bleiben unberührt.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import crashlog
from .previews import read_orientation

# Exif-Ausrichtungen, die eine Vierteldrehung bedeuten
GEDREHT = (5, 6, 7, 8)


def masse_der_datei(pfad: str) -> tuple[int, int] | None:
    """Angezeigte Größe einer Datei - Drehmarke schon eingerechnet.

    Ohne Pillow-Ausnahmen nach außen: eine unlesbare Datei liefert
    None und wird übersprungen.
    """
    try:
        from PIL import Image
        with Image.open(pfad) as bild:
            breite, hoehe = bild.size
    except Exception:
        return None
    if read_orientation(pfad) in GEDREHT:
        breite, hoehe = hoehe, breite
    return breite, hoehe


class MasseWorker(QObject):
    """Prüft den ganzen Index in einem eigenen Faden."""

    fortschritt = pyqtSignal(int, int)          # geprüft, gesamt
    fertig = pyqtSignal(int, int, int)          # berichtigt, übersprungen, gesamt

    def __init__(self, zeilen: list, db_pfad: str) -> None:
        super().__init__()
        self._zeilen = zeilen
        self._db_pfad = db_pfad
        self._abbruch = False

    def abbrechen(self) -> None:
        self._abbruch = True

    def run(self) -> None:
        # Eigene Verbindung: die des Fensters gehört einem anderen Faden.
        import sqlite3
        conn = sqlite3.connect(self._db_pfad)
        berichtigt = uebersprungen = 0
        gesamt = len(self._zeilen)
        try:
            for i, zeile in enumerate(self._zeilen, start=1):
                if self._abbruch:
                    break
                if i % 200 == 0:
                    self.fortschritt.emit(i, gesamt)
                pfad = zeile["path"] if hasattr(zeile, "keys") else zeile[0]
                roh = zeile["is_raw"] if hasattr(zeile, "keys") else zeile[1]
                breite = zeile["width"] if hasattr(zeile, "keys") else zeile[2]
                hoehe = zeile["height"] if hasattr(zeile, "keys") else zeile[3]
                if roh:
                    # RAW kann Pillow nicht öffnen - solche Einträge
                    # kommen aus exiftool und werden hier nicht angefasst.
                    uebersprungen += 1
                    continue
                if not breite or not hoehe or not Path(pfad).exists():
                    uebersprungen += 1
                    continue
                echt = masse_der_datei(pfad)
                if echt is None:
                    uebersprungen += 1
                    continue
                # Nur die AUSRICHTUNG entscheidet, ob eingegriffen wird -
                # die Kantenlängen müssen nicht auf den Pixel stimmen.
                if echt[0] == echt[1]:
                    continue
                # Gegen den STAND IN DER DATENBANK vergleichen, nicht
                # gegen die uebergebene Liste: die kann veraltet sein,
                # und dann zaehlt der Lauf Berichtigungen, die laengst
                # erledigt sind (im Testlauf genau so passiert).
                jetzt = conn.execute(
                    "SELECT width, height FROM photos WHERE path=?",
                    (pfad,)).fetchone()
                if jetzt and jetzt[0] and jetzt[1]:
                    breite, hoehe = jetzt[0], jetzt[1]
                if (hoehe > breite) == (echt[1] > echt[0]):
                    continue
                # Geschrieben wird, was die DATEI sagt - nicht die
                # vertauschten alten Werte. Sonst dreht ein zweiter Lauf
                # mit einer veralteten Liste alles wieder um; im
                # Testlauf hat er das prompt getan.
                try:
                    conn.execute(
                        "UPDATE photos SET width=?, height=? WHERE path=?",
                        (echt[0], echt[1], pfad))
                    berichtigt += 1
                except Exception:
                    crashlog.protokolliere("Maße berichtigen")
                    uebersprungen += 1
            conn.commit()
        finally:
            conn.close()
        self.fortschritt.emit(gesamt, gesamt)
        self.fertig.emit(berichtigt, uebersprungen, gesamt)
