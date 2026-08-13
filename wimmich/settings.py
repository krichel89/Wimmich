"""Einstellungsfenster.

Drei Reiter: Bibliothek (Ordner verwalten), Ansicht, Immich.
Der Dialog ändert nichts von selbst - er liefert am Ende ein Wörterbuch
mit den neuen Werten, plus die Liste der Ordner, die entfernt werden
sollen. Das Aufräumen macht das Hauptfenster, weil dazu die Datenbank
gehört.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from . import marks, theme
from .immich import ImmichClient, ImmichError, normalise_base_url

# Die Rechte, die Wimmich am Immich-Schlüssel braucht - in Immichs eigener
# Gruppierung und Reihenfolge, damit sich die Liste dort von oben nach
# unten abhaken lässt. Immich bietet nur Kreuzchen, kein Einfügefeld; ein
# Kopierblock wäre also nutzlos.
#
# asset.view fehlte lange und war die Ursache der 403 bei Server-
# Vorschauen (Diagnose 0.3.29): es ist von asset.read und asset.download
# GETRENNT.
IMMICH_RECHTE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("asset", (
        ("read", "Bilder und ihre Angaben lesen (Abgleich, Suche)"),
        ("view", "Vorschauen vom Server anzeigen - ohne dies: 403"),
        ("download", "Originale vom Server holen"),
        ("upload", "fehlende Bilder hochladen"),
        ("delete", "Bilder in Immichs Papierkorb legen"),
    )),
    ("album", (
        ("read", "Alben und ihren Inhalt lesen"),
        ("create", "neues Album anlegen"),
        ("update", "Album umbenennen"),
        ("delete", "Album löschen"),
    )),
    ("albumAsset", (
        ("create", "Bilder in ein Album legen"),
        ("delete", "Bilder aus einem Album nehmen"),
    )),
    ("person", (
        ("read", "Personen aus Immichs Gesichtserkennung holen"),
        ("update", "Person umbenennen (Erkunden → Personen)"),
        ("merge", "zwei Personen zusammenführen"),
    )),
    ("user", (
        ("read", "eigener Anmeldename bei „Verbindung prüfen“"),
    )),
    ("server", (
        ("about", "Serverversion bei „Verbindung prüfen“"),
    )),
)

RECHTE_ANZAHL = sum(len(rechte) for _, rechte in IMMICH_RECHTE)


class SettingsDialog(QDialog):
    """Alles, was man einstellen kann, an einer Stelle."""

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.removed_folders: list[str] = []

        self.setWindowTitle("Einstellungen")
        self.setMinimumSize(620, 480)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._library_tab(), "Bibliothek")
        self.tabs.addTab(self._view_tab(), "Ansicht")
        self.tabs.addTab(self._immich_tab(), "Immich")
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        # Qt beschriftet die Knöpfe je nach Systemsprache; hier fest auf Deutsch
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Speichern")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- Reiter Bibliothek ---------------------------------------------

    def _library_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(_hint(
            "Wimmich liest diese Ordner. Es wird nichts verschoben, "
            "umbenannt oder gelöscht."
        ))

        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        for path in self.config.libraries:
            item = QListWidgetItem(path)
            item.setToolTip(path)
            self.folder_list.addItem(item)
        self.folder_list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.folder_list, 1)

        row = QHBoxLayout()
        add_button = QPushButton("Ordner hinzufügen")
        add_button.clicked.connect(self._add_folder)
        row.addWidget(add_button)

        self.remove_button = QPushButton("Aus Wimmich entfernen")
        self.remove_button.clicked.connect(self._remove_folder)
        self.remove_button.setEnabled(False)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.library_note = QLabel("")
        self.library_note.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 14px;"
        )
        self.library_note.setWordWrap(True)
        layout.addWidget(self.library_note)
        return page

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(bool(self.folder_list.selectedItems()))

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Fotoordner wählen")
        if not folder:
            return
        folder = str(Path(folder).resolve())
        existing = [self.folder_list.item(i).text()
                    for i in range(self.folder_list.count())]
        if folder in existing:
            return
        self.folder_list.addItem(QListWidgetItem(folder))
        if folder in self.removed_folders:
            self.removed_folders.remove(folder)

    def _remove_folder(self) -> None:
        items = self.folder_list.selectedItems()
        if not items:
            return
        names = "\n".join(item.text() for item in items)
        answer = QMessageBox.question(
            self, "Ordner entfernen",
            f"Diese Ordner aus Wimmich entfernen?\n\n{names}\n\n"
            "Die Bilder auf der Platte bleiben unangetastet — es "
            "verschwinden nur die Einträge aus dem Index.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for item in items:
            self.removed_folders.append(item.text())
            self.folder_list.takeItem(self.folder_list.row(item))
        self.library_note.setText(
            f"{len(self.removed_folders)} Ordner werden beim Speichern entfernt."
        )
        self._update_buttons()

    # -- Reiter Ansicht ------------------------------------------------

    def _view_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)

        self.theme_box = QComboBox()
        self.theme_box.addItem("Dunkel", "dunkel")
        self.theme_box.addItem("Hell", "hell")
        index = self.theme_box.findData(self.config["theme"] or "dunkel")
        self.theme_box.setCurrentIndex(max(0, index))
        form.addRow("Erscheinungsbild", self.theme_box)

        self.grid_size = QSpinBox()
        self.grid_size.setRange(90, 400)
        self.grid_size.setSingleStep(10)
        self.grid_size.setValue(int(self.config["grid_size"]))
        self.grid_size.setSuffix(" px")
        form.addRow("Kachelgröße", self.grid_size)

        self.thumb_size = QSpinBox()
        self.thumb_size.setRange(128, 512)
        self.thumb_size.setSingleStep(32)
        self.thumb_size.setValue(int(self.config["thumb_size"]))
        self.thumb_size.setSuffix(" px")
        form.addRow("Vorschau im Cache", self.thumb_size)
        form.addRow("", _hint(
            "Eine geänderte Vorschaugröße wirkt erst nach dem Leeren "
            "des Vorschau-Caches."
        ))

        self.prefer_raw = QCheckBox("RAW vertritt den Stapel (sonst JPEG)")
        self.prefer_raw.setChecked(bool(self.config["prefer_raw"]))
        form.addRow("Stapel", self.prefer_raw)

        self.label_set = QComboBox()
        for code, labels in marks.LABEL_SETS.items():
            self.label_set.addItem(f"{code}  ({', '.join(labels[:3])} …)", code)
        index = self.label_set.findData(self.config["label_set"] or "de")
        self.label_set.setCurrentIndex(max(0, index))
        form.addRow("Farbmarkierungen", self.label_set)
        form.addRow("", _hint(
            "Muss zur Sprache des Farbmarkierungssatzes in Lightroom passen, "
            "sonst zeigt Lightroom die Markierung weiß an. Gelesen wird "
            "immer in allen Sprachen."
        ))

        self.write_xmp = QCheckBox(
            "Bewertungen und Farben in die Datei schreiben (XMP)"
        )
        self.write_xmp.setChecked(bool(self.config["write_xmp"]))
        form.addRow("Metadaten", self.write_xmp)
        return page

    # -- Reiter Immich -------------------------------------------------

    def _immich_tab(self) -> QWidget:
        """Der Reiter steckt in einem Rollbereich.

        Mit der Rechteliste ist er höher als das Fenster; ohne
        Rollbereich zöge er die Mindestgröße des Dialogs mit hoch
        (gemessen 0.3.41: 489 → 704 px).
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        form.setSpacing(10)

        self.url_edit = QLineEdit(self.config["immich_url"] or "")
        self.url_edit.setPlaceholderText("https://fotos.example.de")
        form.addRow("Serveradresse", self.url_edit)

        self.key_edit = QLineEdit(self.config["immich_key"] or "")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("API-Schlüssel aus den Kontoeinstellungen")
        form.addRow("Schlüssel", self.key_edit)

        self.upload_box = QCheckBox("Fehlende Bilder hochladen")
        self.upload_box.setChecked(bool(self.config["immich_upload"]))
        form.addRow("", self.upload_box)

        self.people_box = QCheckBox("Personen holen (Gesichtserkennung des Servers)")
        self.people_box.setChecked(bool(self.config["immich_people"]))
        form.addRow("", self.people_box)

        self.auto_box = QCheckBox("Laufend abgleichen, solange die Verbindung steht")
        self.auto_box.setChecked(bool(self.config["immich_auto"]))
        form.addRow("", self.auto_box)

        self.interval = QSpinBox()
        self.interval.setRange(1, 720)
        self.interval.setValue(int(self.config["immich_interval_min"]))
        self.interval.setSuffix(" Minuten")
        form.addRow("Abstand", self.interval)

        self.watch_box = QCheckBox("Auf Änderungen in den Ordnern reagieren")
        self.watch_box.setChecked(bool(self.config["watch_folders"]))
        form.addRow("", self.watch_box)
        layout.addLayout(form)

        layout.addWidget(_hint(
            "Der Schlüssel steht in Immich unter Kontoeinstellungen → "
            "API-Schlüssel. Am besten einen eigenen nur für Wimmich, mit "
            "genau diesen Rechten:"
        ))

        layout.addWidget(self._rechte_block())
        layout.addWidget(_trennlinie())

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        test_button = QPushButton("Verbindung prüfen")
        test_button.clicked.connect(self._test)
        layout.addWidget(test_button)

        layout.addWidget(_hint(
            "Die Ordner auf der Platte bleiben führend, Immich ist der "
            "Spiegel. Vom Server geholt werden Vorschauen; Originale nur "
            "auf ausdrücklichen Wunsch. Der Schlüssel liegt im Klartext in "
            "der config.json im Benutzerprofil."
        ))
        layout.addStretch(1)

        rollbereich = QScrollArea()
        rollbereich.setWidget(page)
        rollbereich.setWidgetResizable(True)
        rollbereich.setFrameShape(QFrame.Shape.NoFrame)
        return rollbereich

    # -- Rechteliste ---------------------------------------------------

    def _rechte_block(self) -> QWidget:
        """Die nötigen Rechte zum Abhaken, in Immichs Gruppierung.

        Die Kreuzchen ändern nichts - Wimmich kann in Immich keine Rechte
        setzen. Sie merken sich nur, was schon erledigt ist, und stehen
        beim nächsten Öffnen wieder da.
        """
        block = QWidget()
        spalte = QVBoxLayout(block)
        spalte.setContentsMargins(0, 0, 0, 0)
        spalte.setSpacing(6)

        titel = QLabel(f"Nötige Rechte am Schlüssel ({RECHTE_ANZAHL})")
        titel.setStyleSheet("font-weight: 600;")
        spalte.addWidget(titel)
        spalte.addWidget(_hint(
            "Immich hat dort nur Kreuzchen — diese Liste ist zum Abhaken. "
            "Fehlt asset.view, bleiben Server-Vorschauen leer (403)."
        ))

        erledigt = self.config["immich_rechte_ok"]
        if not isinstance(erledigt, list):
            erledigt = []
        self.rechte_boxen: dict[str, QCheckBox] = {}

        for gruppe, rechte in IMMICH_RECHTE:
            zeile = QHBoxLayout()
            zeile.setSpacing(12)
            name = QLabel(gruppe)
            name.setMinimumWidth(120)
            name.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            zeile.addWidget(name)
            for recht, wozu in rechte:
                voll = f"{gruppe}.{recht}"
                box = QCheckBox(recht)
                box.setToolTip(f"{voll} — {wozu}")
                box.setChecked(voll in erledigt)
                self.rechte_boxen[voll] = box
                zeile.addWidget(box)
            zeile.addStretch(1)
            spalte.addLayout(zeile)
        return block

    def _test(self) -> None:
        url = normalise_base_url(self.url_edit.text())
        if not url or not self.key_edit.text().strip():
            self._say("Adresse und Schlüssel werden beide gebraucht.", False)
            return
        self.result_label.setText("Wird geprüft …")
        self.result_label.repaint()

        try:
            info = ImmichClient(url, self.key_edit.text()).connect()
        except ImmichError as exc:
            self._say(str(exc), False)
            return

        parts = [f"Verbunden mit Immich {info.version or '(Version unbekannt)'}"]
        if info.user:
            parts.append(f"als {info.user}")
        if not info.plural_paths:
            parts.append("— älterer Server, Endpunkte in Einzahl erkannt")
        self._say(" ".join(parts), True)

    def _say(self, text: str, good: bool) -> None:
        colour = "#5ac37a" if good else "#ff6b6b"
        self.result_label.setStyleSheet(f"color: {colour}; font-size: 14px;")
        self.result_label.setText(text)

    # -- Ergebnis ------------------------------------------------------

    def values(self) -> dict:
        return {
            "libraries": [self.folder_list.item(i).text()
                          for i in range(self.folder_list.count())],
            "theme": self.theme_box.currentData(),
            "grid_size": self.grid_size.value(),
            "thumb_size": self.thumb_size.value(),
            "prefer_raw": self.prefer_raw.isChecked(),
            "label_set": self.label_set.currentData(),
            "write_xmp": self.write_xmp.isChecked(),
            "immich_url": normalise_base_url(self.url_edit.text()),
            "immich_key": self.key_edit.text().strip(),
            "immich_upload": self.upload_box.isChecked(),
            "immich_people": self.people_box.isChecked(),
            "immich_auto": self.auto_box.isChecked(),
            "immich_interval_min": self.interval.value(),
            "watch_folders": self.watch_box.isChecked(),
            "immich_rechte_ok": [name for name, box in self.rechte_boxen.items()
                                 if box.isChecked()],
        }


def _trennlinie() -> QFrame:
    linie = QFrame()
    linie.setFrameShape(QFrame.Shape.HLine)
    linie.setStyleSheet(f"color: {theme.BORDER};")
    return linie


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 14px;")
    label.setWordWrap(True)
    return label
