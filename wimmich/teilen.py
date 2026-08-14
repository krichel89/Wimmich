"""Teilen: öffentliche Links und Alben für andere Immich-Konten.

Alle Dialoge hier sind DUMM: sie fragen nichts beim Server nach und
liefern nur, was Harald eingestellt hat. Geholt und geschickt wird
ausschließlich in den Hintergrundaufgaben des Fensters — sonst steht
das Fenster still, solange Immich braucht (Lehre aus 0.3.37).

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from . import theme

# Auswahl für das Ablaufdatum: Text -> Tage (0 = kein Ablauf)
ABLAUF = (
    ("kein Ablauf", 0),
    ("nach 1 Tag", 1),
    ("nach 1 Woche", 7),
    ("nach 30 Tagen", 30),
    ("nach 1 Jahr", 365),
)


def _knopfleiste(ok_text: str = "OK"):
    """OK/Abbrechen auf Deutsch.

    Qt beschriftet die Standardknoepfe sonst englisch („Cancel"), weil
    keine deutsche Uebersetzung geladen ist - im Einstellungsfenster
    wird es genauso von Hand gesetzt.
    """
    kasten = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
    kasten.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    kasten.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
    return kasten


def _hinweis(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
    return label


def ablauf_zeitpunkt(tage: int) -> str:
    """Tage ab jetzt als Zeitstempel, wie Immich ihn erwartet.

    Immich verlangt ISO 8601 mit Zeitzone; ohne Zeitzone lehnt der
    Server den Wert ab. Bei 0 bleibt das Feld leer und wird gar nicht
    erst mitgeschickt.
    """
    if tage <= 0:
        return ""
    ziel = datetime.now(timezone.utc) + timedelta(days=tage)
    return ziel.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class LinkDialog(QDialog):
    """Was soll der öffentliche Link können?"""

    def __init__(self, album_name: str, eltern=None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Öffentlichen Link erstellen")
        self.setMinimumWidth(460)
        spalte = QVBoxLayout(self)

        spalte.addWidget(_hinweis(
            f"Für das Album „{album_name}“. Wer den Link hat, sieht die "
            "Bilder — ohne Konto auf deinem Server."))

        form = QFormLayout()
        self.beschreibung = QLineEdit()
        self.beschreibung.setPlaceholderText("wofür der Link gedacht ist")
        form.addRow("Beschreibung", self.beschreibung)

        self.passwort = QLineEdit()
        self.passwort.setEchoMode(QLineEdit.EchoMode.Password)
        self.passwort.setPlaceholderText("leer = ohne Passwort")
        form.addRow("Passwort", self.passwort)

        self.ablauf = QComboBox()
        for text, tage in ABLAUF:
            self.ablauf.addItem(text, tage)
        form.addRow("Läuft ab", self.ablauf)

        self.wunsch_url = QLineEdit()
        self.wunsch_url.setPlaceholderText("leer = zufällige, lange Adresse")
        self.wunsch_url.setToolTip(
            "Eine eigene, kurze Adresse ist bequemer — aber auch zu "
            "erraten. Ohne Wunsch-URL vergibt Immich einen langen "
            "Zufallsschlüssel.")
        form.addRow("Wunsch-URL", self.wunsch_url)
        spalte.addLayout(form)

        self.download = QCheckBox("Herunterladen erlauben")
        self.download.setChecked(True)
        self.upload = QCheckBox("Hochladen erlauben (Gäste dürfen Bilder beisteuern)")
        self.metadaten = QCheckBox("Aufnahmedaten zeigen (Zeit, Ort, Kamera)")
        self.metadaten.setChecked(True)
        for kasten in (self.download, self.upload, self.metadaten):
            spalte.addWidget(kasten)

        knoepfe = _knopfleiste("Link erstellen")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        spalte.addWidget(knoepfe)

    def werte(self) -> dict:
        return {
            "beschreibung": self.beschreibung.text().strip(),
            "passwort": self.passwort.text(),
            "laeuft_ab": ablauf_zeitpunkt(int(self.ablauf.currentData() or 0)),
            "download": self.download.isChecked(),
            "upload": self.upload.isChecked(),
            "metadaten": self.metadaten.isChecked(),
            "wunsch_url": self.wunsch_url.text().strip(),
        }


class LinkFertigDialog(QDialog):
    """Der fertige Link zum Kopieren."""

    def __init__(self, url: str, mit_passwort: bool, eltern=None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Link steht")
        self.setMinimumWidth(520)
        spalte = QVBoxLayout(self)

        self.feld = QLineEdit(url)
        self.feld.setReadOnly(True)
        self.feld.selectAll()
        spalte.addWidget(self.feld)

        if mit_passwort:
            spalte.addWidget(_hinweis(
                "Der Link ist mit einem Passwort geschützt — das musst du "
                "getrennt weitergeben."))

        zeile = QHBoxLayout()
        kopieren = QPushButton("In die Zwischenablage")
        kopieren.clicked.connect(self._kopieren)
        zeile.addWidget(kopieren)
        zeile.addStretch(1)
        fertig = QPushButton("Fertig")
        fertig.clicked.connect(self.accept)
        zeile.addWidget(fertig)
        spalte.addLayout(zeile)

    def _kopieren(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.feld.text())


class NutzerDialog(QDialog):
    """Album einem anderen Konto auf demselben Server geben."""

    def __init__(self, album_name: str, nutzer: list, eltern=None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Album freigeben")
        self.setMinimumWidth(420)
        self._nutzer = list(nutzer)
        spalte = QVBoxLayout(self)
        spalte.addWidget(_hinweis(
            f"„{album_name}“ für ein Konto auf demselben Immich-Server. "
            "Kein öffentlicher Link — die Person meldet sich normal an."))

        self.liste = QListWidget()
        for eintrag in self._nutzer:
            beschriftung = eintrag.name or eintrag.mail or eintrag.id
            if eintrag.name and eintrag.mail:
                beschriftung = f"{eintrag.name} ({eintrag.mail})"
            zeile = QListWidgetItem(beschriftung)
            zeile.setData(Qt.ItemDataRole.UserRole, eintrag.id)
            self.liste.addItem(zeile)
        self.liste.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        if self._nutzer:
            self.liste.setCurrentRow(0)
        spalte.addWidget(self.liste, 1)

        form = QFormLayout()
        self.rolle = QComboBox()
        self.rolle.addItem("nur ansehen", "viewer")
        self.rolle.addItem("darf auch Bilder hineinlegen", "editor")
        form.addRow("Rechte", self.rolle)
        spalte.addLayout(form)

        knoepfe = _knopfleiste("Freigeben")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        spalte.addWidget(knoepfe)

    def auswahl(self) -> tuple[list[str], str]:
        ids = [i.data(Qt.ItemDataRole.UserRole)
               for i in self.liste.selectedItems()]
        return ids, str(self.rolle.currentData() or "viewer")


class FreigabenDialog(QDialog):
    """Alle Links auf einen Blick: kopieren und zurücknehmen.

    Gelöscht wird erst beim Schließen mit OK - bis dahin ist jeder
    Strich durchgestrichen und laesst sich mit „Doch behalten"
    zuruecknehmen. Ein Fehlklick kostet so keinen Link.
    """

    def __init__(self, eintraege: list, url_fuer, eltern=None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Freigaben")
        self.setMinimumSize(620, 380)
        self._url_fuer = url_fuer
        self._weg: set[str] = set()
        spalte = QVBoxLayout(self)

        if not eintraege:
            spalte.addWidget(_hinweis("Es gibt noch keine Freigaben."))

        self.liste = QListWidget()
        for eintrag in eintraege:
            zeile = QListWidgetItem(self._text(eintrag))
            zeile.setData(Qt.ItemDataRole.UserRole, eintrag.id)
            zeile.setToolTip(url_fuer(eintrag))
            self.liste.addItem(zeile)
        spalte.addWidget(self.liste, 1)
        self._eintraege = {e.id: e for e in eintraege}

        zeile = QHBoxLayout()
        kopieren = QPushButton("Link kopieren")
        kopieren.clicked.connect(self._kopieren)
        zeile.addWidget(kopieren)
        self.loeschen = QPushButton("Zurücknehmen")
        self.loeschen.clicked.connect(self._umschalten)
        zeile.addWidget(self.loeschen)
        zeile.addStretch(1)
        spalte.addLayout(zeile)

        spalte.addWidget(_hinweis(
            "Zurückgenommene Links verschwinden erst mit „OK“ — und dann "
            "endgültig, auch auf dem Server."))

        knoepfe = _knopfleiste("Übernehmen")
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        spalte.addWidget(knoepfe)

    @staticmethod
    def _text(eintrag) -> str:
        teile = [eintrag.beschreibung or eintrag.album
                 or ("Einzelne Bilder" if eintrag.typ == "INDIVIDUAL"
                     else "Album")]
        if eintrag.typ == "INDIVIDUAL" and eintrag.anzahl:
            teile.append(f"{eintrag.anzahl} Bild(er)")
        if eintrag.mit_passwort:
            teile.append("mit Passwort")
        if eintrag.laeuft_ab:
            teile.append(f"läuft ab {eintrag.laeuft_ab[:10]}")
        if not eintrag.download:
            teile.append("kein Download")
        if eintrag.upload:
            teile.append("Gäste dürfen hochladen")
        return "  •  ".join(teile)

    def _aktuelle_id(self) -> str:
        eintrag = self.liste.currentItem()
        return str(eintrag.data(Qt.ItemDataRole.UserRole)) if eintrag else ""

    def _kopieren(self) -> None:
        from PyQt6.QtWidgets import QApplication
        kennung = self._aktuelle_id()
        if kennung:
            QApplication.clipboard().setText(
                self._url_fuer(self._eintraege[kennung]))

    def _umschalten(self) -> None:
        eintrag = self.liste.currentItem()
        if eintrag is None:
            return
        kennung = str(eintrag.data(Qt.ItemDataRole.UserRole))
        schrift = eintrag.font()
        if kennung in self._weg:
            self._weg.discard(kennung)
            schrift.setStrikeOut(False)
        else:
            self._weg.add(kennung)
            schrift.setStrikeOut(True)
        eintrag.setFont(schrift)
        self.loeschen.setText(
            "Doch behalten" if kennung in self._weg else "Zurücknehmen")

    def zu_loeschen(self) -> list[str]:
        return sorted(self._weg)
