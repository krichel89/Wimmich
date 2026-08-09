"""Einstellungen für die Immich-Verbindung."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from . import theme
from .immich import ImmichClient, ImmichError, normalise_base_url


class ImmichSettingsDialog(QDialog):
    """Serveradresse, Schlüssel, Verbindungstest."""

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Immich-Verbindung")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.url_edit = QLineEdit(config["immich_url"] or "")
        self.url_edit.setPlaceholderText("https://fotos.example.de")
        form.addRow("Serveradresse", self.url_edit)

        self.key_edit = QLineEdit(config["immich_key"] or "")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("API-Schlüssel aus den Kontoeinstellungen")
        form.addRow("Schlüssel", self.key_edit)

        self.upload_box = QCheckBox("Fehlende Bilder hochladen")
        self.upload_box.setChecked(bool(config["immich_upload"]))
        form.addRow("", self.upload_box)

        self.people_box = QCheckBox("Personen holen (Gesichtserkennung des Servers)")
        self.people_box.setChecked(bool(config["immich_people"]))
        form.addRow("", self.people_box)

        layout.addLayout(form)

        hint = QLabel(
            "Der Schlüssel steht in Immich unter Kontoeinstellungen → API-Schlüssel.\n"
            "Nötige Rechte: asset.read, asset.upload, album.read, person.read.\n\n"
            "Wimmich lädt nichts vom Server herunter — die Ordner auf der Platte\n"
            "bleiben führend, Immich ist der Spiegel."
        )
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        test_button = QPushButton("Verbindung prüfen")
        test_button.clicked.connect(self._test)
        layout.addWidget(test_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _test(self) -> None:
        url = normalise_base_url(self.url_edit.text())
        if not url or not self.key_edit.text().strip():
            self._say("Adresse und Schlüssel werden beide gebraucht.", False)
            return
        self.result_label.setText("Wird geprüft …")
        self.result_label.repaint()

        client = ImmichClient(url, self.key_edit.text())
        try:
            info = client.connect()
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
        self.result_label.setStyleSheet(f"color: {colour}; font-size: 12px;")
        self.result_label.setText(text)

    def values(self) -> dict:
        return {
            "immich_url": normalise_base_url(self.url_edit.text()),
            "immich_key": self.key_edit.text().strip(),
            "immich_upload": self.upload_box.isChecked(),
            "immich_people": self.people_box.isChecked(),
        }
