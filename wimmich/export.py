"""Stapel-Export: mehrere Bilder in einem Zug in einen Ordner schreiben.

Bisher gab es nur „Als Datei speichern" für das EINE Bild in der Lupe.
Hier wird eine ganze Auswahl gerechnet - mit den gesetzten Bearbeitungs-
schritten, wahlweise verkleinert und umbenannt.

Zwei Dinge sind bewusst so gebaut:

* Gerechnet wird in einem eigenen Faden. Ein 45-MP-RAW mit Retuschen
  braucht Sekunden; im Fensterfaden stünde das Programm.
* Es wird NIE eine vorhandene Datei überschrieben - dieselbe Regel wie
  beim Holen von Originalen. Wimmich löscht und überschreibt nichts.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout,
)

from . import crashlog
from .edits import EditStack, array_to_qimage, qimage_to_array
from .previews import decode as decode_image

# Längste Kante, wenn verkleinert wird. Die Werte decken die üblichen
# Fälle ab: Netzgalerie, Bildschirm, Druck.
GROESSEN = [("Originalgröße", 0), ("4000 px", 4000), ("2560 px", 2560),
            ("1920 px", 1920), ("1200 px", 1200), ("800 px", 800)]

FORMATE = [("JPEG (*.jpg)", "jpg"), ("PNG (*.png)", "png"),
           ("TIFF (*.tif)", "tif")]


class ExportFehler(Exception):
    """Ein Bild ging nicht - erwartbar, kein Programmfehler.

    Solche Fälle (Datei weg, unlesbar, Ziel nicht beschreibbar) gehören
    in die Fehlerliste am Ende, NICHT ins fehler.log: das Protokoll ist
    für Abstürze da und verlöre seinen Wert, wenn eine kaputte JPEG-
    Datei darin landet.
    """


def freier_name(pfad: Path) -> Path:
    """Nie eine vorhandene Datei überschreiben - hängt (1), (2) … an."""
    if not pfad.exists():
        return pfad
    stamm, endung = pfad.stem, pfad.suffix
    nummer = 1
    while True:
        kandidat = pfad.with_name(f"{stamm} ({nummer}){endung}")
        if not kandidat.exists():
            return kandidat
        nummer += 1


def zielname(quelle: Path, endung: str, vorsatz: str = "",
             nummer: int | None = None, stellen: int = 3) -> str:
    """Dateiname im Ziel: Vorsatz + Originalname oder Vorsatz + Nummer."""
    stamm = quelle.stem if nummer is None else str(nummer).zfill(stellen)
    return f"{vorsatz}{stamm}.{endung}"


class ExportOptionen:
    """Was der Nutzer im Fenster eingestellt hat."""

    def __init__(self, ziel: Path, endung: str = "jpg", qualitaet: int = 92,
                 max_kante: int = 0, vorsatz: str = "",
                 nummerieren: bool = False, nur_bearbeitete: bool = False) -> None:
        self.ziel = Path(ziel)
        self.endung = endung
        self.qualitaet = int(qualitaet)
        self.max_kante = int(max_kante)
        self.vorsatz = vorsatz
        self.nummerieren = bool(nummerieren)
        self.nur_bearbeitete = bool(nur_bearbeitete)


class ExportDialog(QDialog):
    """Fragt Zielordner und Ausgabeform ab."""

    def __init__(self, anzahl: int, vorgabe_ordner: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auswahl exportieren")
        self.setMinimumWidth(460)

        aussen = QVBoxLayout(self)
        aussen.addWidget(QLabel(f"<b>{anzahl} Bilder</b> werden geschrieben. "
                                "Die Originale bleiben unangetastet."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        zeile = QHBoxLayout()
        self.ordner_feld = QLineEdit(vorgabe_ordner)
        self.ordner_feld.setPlaceholderText("Zielordner wählen …")
        waehlen = QPushButton("…")
        waehlen.setFixedWidth(34)
        waehlen.clicked.connect(self._ordner_waehlen)
        zeile.addWidget(self.ordner_feld)
        zeile.addWidget(waehlen)
        form.addRow("Zielordner", zeile)

        self.format_wahl = QComboBox()
        for text, _endung in FORMATE:
            self.format_wahl.addItem(text)
        self.format_wahl.currentIndexChanged.connect(self._format_gewechselt)
        form.addRow("Format", self.format_wahl)

        self.qualitaet = QSpinBox()
        self.qualitaet.setRange(50, 100)
        self.qualitaet.setValue(92)
        self.qualitaet.setSuffix(" %")
        form.addRow("Qualität", self.qualitaet)

        self.groesse = QComboBox()
        for text, _px in GROESSEN:
            self.groesse.addItem(text)
        form.addRow("Längste Kante", self.groesse)

        self.vorsatz = QLineEdit()
        self.vorsatz.setPlaceholderText("z. B. Berlinale_ (bleibt leer: Originalname)")
        form.addRow("Namensvorsatz", self.vorsatz)

        self.nummerieren = QCheckBox("Durchnummerieren statt Originalname")
        form.addRow("", self.nummerieren)

        self.nur_bearbeitete = QCheckBox("Nur Bilder mit Bearbeitungsschritten")
        form.addRow("", self.nur_bearbeitete)

        aussen.addLayout(form)

        knoepfe = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        knoepfe.button(QDialogButtonBox.StandardButton.Ok).setText("Exportieren")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        aussen.addWidget(knoepfe)

    def _ordner_waehlen(self) -> None:
        ordner = QFileDialog.getExistingDirectory(
            self, "Zielordner", self.ordner_feld.text() or str(Path.home()))
        if ordner:
            self.ordner_feld.setText(ordner)

    def _format_gewechselt(self, index: int) -> None:
        # Qualität ist nur bei JPEG eine Stellschraube
        self.qualitaet.setEnabled(FORMATE[index][1] == "jpg")

    def optionen(self) -> ExportOptionen | None:
        ordner = self.ordner_feld.text().strip()
        if not ordner:
            return None
        return ExportOptionen(
            ziel=Path(ordner),
            endung=FORMATE[self.format_wahl.currentIndex()][1],
            qualitaet=self.qualitaet.value(),
            max_kante=GROESSEN[self.groesse.currentIndex()][1],
            vorsatz=self.vorsatz.text(),
            nummerieren=self.nummerieren.isChecked(),
            nur_bearbeitete=self.nur_bearbeitete.isChecked())


class ExportWorker(QObject):
    """Rechnet den Stapel in einem eigenen Faden.

    Gibt nach jedem Bild Bescheid, damit der Balken läuft und der
    Abbruch greift. Ein Fehler bei EINEM Bild beendet den Lauf nicht -
    er wird gesammelt und am Ende genannt.
    """

    fortschritt = pyqtSignal(str, int, int)     # Name, fertig, gesamt
    fertig = pyqtSignal(int, int, list)         # geschrieben, übersprungen, Fehler

    def __init__(self, auftraege: list[tuple[str, str | None]],
                 optionen: ExportOptionen) -> None:
        super().__init__()
        self._auftraege = auftraege
        self._optionen = optionen
        self._abbruch = False

    def abbrechen(self) -> None:
        self._abbruch = True

    def run(self) -> None:
        opt = self._optionen
        geschrieben = uebersprungen = 0
        fehler: list[str] = []
        gesamt = len(self._auftraege)
        try:
            opt.ziel.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.fertig.emit(0, 0, [f"Zielordner nicht nutzbar: {exc}"])
            return

        for i, (pfad, schritte) in enumerate(self._auftraege, start=1):
            if self._abbruch:
                break
            quelle = Path(pfad)
            self.fortschritt.emit(quelle.name, i - 1, gesamt)
            if opt.nur_bearbeitete and not schritte:
                uebersprungen += 1
                continue
            try:
                geschrieben += self._eines(quelle, schritte, i)
            except ExportFehler as exc:
                fehler.append(f"{quelle.name}: {exc}")
            except Exception as exc:               # eine Datei darf nicht alles reissen
                crashlog.protokolliere("Stapel-Export")
                fehler.append(f"{quelle.name}: {exc}")

        self.fortschritt.emit("", gesamt, gesamt)
        self.fertig.emit(geschrieben, uebersprungen, fehler)

    def _eines(self, quelle: Path, schritte: str | None, nummer: int) -> int:
        opt = self._optionen
        bild = decode_image(str(quelle), None)
        if bild is None or bild.isNull():
            raise ExportFehler("Bild nicht lesbar")

        stapel = EditStack.from_json(schritte)
        if stapel.steps:
            bild = array_to_qimage(stapel.apply(qimage_to_array(bild)))

        if opt.max_kante and max(bild.width(), bild.height()) > opt.max_kante:
            bild = bild.scaled(
                opt.max_kante, opt.max_kante,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)

        name = zielname(quelle, opt.endung, opt.vorsatz,
                        nummer if opt.nummerieren else None)
        ziel = freier_name(opt.ziel / name)
        qualitaet = opt.qualitaet if opt.endung == "jpg" else -1
        if not bild.save(str(ziel), None, qualitaet):
            raise ExportFehler("konnte nicht geschrieben werden")
        return 1
