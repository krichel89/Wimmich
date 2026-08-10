"""Fehleraufzeichnung.

Warum es das gibt: PyQt6 beendet das Programm HART, sobald in einem Slot
eine unbehandelte Python-Ausnahme auftritt - ohne Meldung, ohne Spur.
Genau das erzeugt „Abstürze, die nicht reproduzierbar sind": der Fehler
war da, aber niemand hat ihn je gesehen.

Ein eigener sys.excepthook fängt das ab. Er schreibt den vollständigen
Rückverfolgungspfad in eine Datei, zeigt ihn einmal an - und kehrt dann
zurück, statt abzubrechen. Dadurch bleibt das Programm am Leben, und der
Fehler ist beim nächsten Mal belegbar.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

from .config import CONFIG_DIR

LOG_PATH = CONFIG_DIR / "fehler.log"
MAX_BYTES = 512 * 1024        # darüber wird die Datei einmal umgebrochen

_gemeldet: set[str] = set()
_dialog_offen = False


def log_path() -> Path:
    return LOG_PATH


def _kuerzen() -> None:
    """Datei nicht endlos wachsen lassen."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            alt = LOG_PATH.with_suffix(".log.alt")
            alt.unlink(missing_ok=True)
            LOG_PATH.replace(alt)
    except OSError:
        pass


def schreibe(text: str, kopf: str = "") -> None:
    """Einen Eintrag anhängen. Schlägt das fehl, ist das kein Drama."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _kuerzen()
        with open(LOG_PATH, "a", encoding="utf-8") as datei:
            datei.write(f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')}"
                        f"{'  ' + kopf if kopf else ''}\n{text}\n")
    except OSError:
        pass


def _signatur(typ, wert, spur) -> str:
    """Kurzkennung, damit derselbe Fehler nicht zehnmal ein Fenster öffnet."""
    letzte = traceback.extract_tb(spur)[-1] if spur else None
    ort = f"{letzte.filename}:{letzte.lineno}" if letzte else "?"
    return f"{typ.__name__}|{ort}"


def install(app_version: str = "", parent_getter=None) -> None:
    """Eigenen Fehlerhaken setzen.

    parent_getter liefert - wenn vorhanden - das Fenster für die Meldung.
    Es wird bewusst als Funktion übergeben: beim Setzen des Hakens gibt
    es das Hauptfenster noch nicht.
    """
    vorher = sys.excepthook

    def haken(typ, wert, spur):
        if issubclass(typ, KeyboardInterrupt):
            vorher(typ, wert, spur)
            return

        text = "".join(traceback.format_exception(typ, wert, spur))
        schreibe(text, kopf=f"Wimmich {app_version}")
        sys.stderr.write(text)

        kennung = _signatur(typ, wert, spur)
        if kennung in _gemeldet:
            return                      # schon gezeigt - nur noch protokollieren
        _gemeldet.add(kennung)
        _zeige_meldung(typ, wert, parent_getter)

    sys.excepthook = haken


def _zeige_meldung(typ, wert, parent_getter) -> None:
    """Meldung anzeigen - ohne dabei selbst einen Fehler auszulösen."""
    global _dialog_offen
    if _dialog_offen:
        return
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        eltern = None
        if parent_getter is not None:
            try:
                eltern = parent_getter()
            except Exception:
                eltern = None

        _dialog_offen = True
        box = QMessageBox(eltern)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Da ist etwas schiefgegangen")
        box.setText(
            "Wimmich läuft weiter, aber eine Aktion ist fehlgeschlagen:\n\n"
            f"{typ.__name__}: {wert}"
        )
        box.setInformativeText(
            f"Der vollständige Bericht steht in:\n{LOG_PATH}\n\n"
            "Diese Meldung erscheint je Fehlerart nur einmal je Sitzung."
        )
        box.exec()
    except Exception:
        pass                            # Melden darf nie selbst abstürzen
    finally:
        _dialog_offen = False
