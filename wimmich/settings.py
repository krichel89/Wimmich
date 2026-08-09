"""Einstellungsfenster.

Drei Reiter: Bibliothek (Ordner verwalten), Ansicht, Immich.
Der Dialog ändert nichts von selbst - er liefert am Ende ein Wörterbuch
mit den neuen Werten, plus die Liste der Ordner, die entfernt werden
sollen. Das Aufräumen macht das Hauptfenster, weil dazu die Datenbank
gehört.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from . import marks, theme
from .immich import ImmichClient, ImmichError, normalise_base_url


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
            "API-Schlüssel. Nötige Rechte: asset.read, asset.upload, "
            "album.read, person.read.\n\n"
            "Wimmich lädt nichts vom Server herunter — die Ordner auf der "
            "Platte bleiben führend, Immich ist der Spiegel.\n\n"
            "Der Schlüssel liegt im Klartext in der config.json im "
            "Benutzerprofil. Am besten einen eigenen Schlüssel nur für "
            "Wimmich anlegen, mit genau diesen vier Rechten."
        ))

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        test_button = QPushButton("Verbindung prüfen")
        test_button.clicked.connect(self._test)
        layout.addWidget(test_button)
        layout.addStretch(1)
        return page

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
        }


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 14px;")
    label.setWordWrap(True)
    return label
