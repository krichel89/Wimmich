"""Alles, was in der Lupe und im Raster mit dem Server zu tun hat.

Herausgeloest aus mainwindow.py (0.3.35): Originale holen, Serverbilder
bearbeitbar machen oder loeschen, die Vorschau eines reinen Serverbildes,
und der Abgleich mit Immich. Es ist eine MISCHKLASSE - die Methoden
laufen unveraendert am MainWindow und greifen weiter auf self.db,
self.config und die Bauteile des Fensters zu. Der Schnitt ist eine
Ordnungsfrage, keine Umstellung: mainwindow.py war mit 149 KB die
unuebersichtlichste Datei im Programm.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QMessageBox, QTreeWidgetItem,
)

from . import APP_NAME, __version__, crashlog, previews, remote_thumbs, theme
from .canvas import NONE as TOOL_NONE
from .config import is_raw
from .edits import EditStack
from .exif import read_fast
from .immich import PING_TIMEOUT, ImmichClient, ImmichError
from .sync import SyncWorker


class _VorschauSignale(QObject):
    """Traegt das Ergebnis aus dem Hintergrundfaden zurueck ins Fenster.

    `gewuenscht` ist die Kennung des Bildes, das GERADE in der Lupe
    steht. Beim schnellen Blaettern mit totem Server stapeln sich sonst
    Auftraege, die je bis zu 8 Sekunden warten - jeder fuer ein Bild,
    das laengst niemand mehr sieht.
    """

    fertig = pyqtSignal(str, object)
    client_da = pyqtSignal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self.gewuenscht = ""


class _ClientTask(QRunnable):
    """Prueft und verbindet den Server, ohne das Fenster anzuhalten."""

    def __init__(self, url: str, key: str, signale: _VorschauSignale) -> None:
        super().__init__()
        self.url = url
        self.key = key
        self.signale = signale

    def run(self) -> None:
        client = ImmichClient(self.url, self.key)
        grund = ""
        try:
            erreichbar, grund = client.erreichbar()
            if erreichbar:
                client.connect(timeout=PING_TIMEOUT * 3)
            else:
                client = None
        except Exception as exc:
            client, grund = None, str(exc)
        try:
            self.signale.client_da.emit(client, grund)
        except RuntimeError:
            pass          # Fenster ist inzwischen zu


class _ServerSignale(QObject):
    """Antworten der Serversuche zurueck ins Fenster."""

    orte_da = pyqtSignal(list, str)        # Staedte, Fehlergrund
    treffer_da = pyqtSignal(str, list, str)  # Anlass, Kennungen, Fehlergrund
    person_fertig = pyqtSignal(str, str, object, str)  # Aktion, Ziel, Ergebnis, Grund
    person_bild = pyqtSignal(str, str)     # Personenkennung, Pfad zum Bild


class _OrteTask(QRunnable):
    """Holt die Ortsliste, ohne das Fenster anzuhalten."""

    def __init__(self, client, signale: _ServerSignale) -> None:
        super().__init__()
        self.client = client
        self.signale = signale

    def run(self) -> None:
        orte, grund = [], ""
        try:
            orte = self.client.vorschlaege("city")
        except ImmichError as exc:
            # Erwartbar: alte Server kennen den Endpunkt nicht, oder dem
            # Schluessel fehlt ein Recht. Das gehoert NICHT ins
            # fehler.log - dort stehen Abstuerze.
            grund = ("Server kennt keine Ortssuche"
                     if getattr(exc, "status", 0) == 404 else str(exc))
        except Exception as exc:
            grund = str(exc)
            crashlog.protokolliere("Ortsliste")
        try:
            self.signale.orte_da.emit(orte, grund)
        except RuntimeError:
            pass


class _GesichtTask(QRunnable):
    """Holt EIN Gesichtsbildchen fuer den Baum."""

    def __init__(self, client, person_id: str, signale: _ServerSignale) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.person_id = person_id
        self.signale = signale

    def run(self) -> None:
        try:
            daten = remote_thumbs.fetch_person(self.client, self.person_id)
        except Exception:
            daten = None          # ohne Bild bleibt der Baum eben schlicht
        if not daten:
            return
        try:
            self.signale.person_bild.emit(
                self.person_id,
                str(remote_thumbs.person_cache_path(self.person_id)))
        except RuntimeError:
            pass


class _PersonTask(QRunnable):
    """Aendert eine Person auf dem SERVER, ohne das Fenster anzuhalten.

    Der Server ist die Wahrheit: erst dort aendern, den lokalen Spiegel
    danach nachziehen. Andersherum stuende in Wimmich ein Name, den
    Immich nicht kennt, und der naechste Abgleich holte den alten
    zurueck.
    """

    def __init__(self, client, aktion: str, ziel_id: str, wert,
                 signale: _ServerSignale) -> None:
        super().__init__()
        self.client = client
        self.aktion = aktion
        self.ziel_id = ziel_id
        self.wert = wert
        self.signale = signale

    def run(self) -> None:
        ergebnis, grund = None, ""
        try:
            if self.aktion == "umbenennen":
                person = self.client.person_aendern(self.ziel_id,
                                                    name=self.wert)
                ergebnis = person.name
            else:
                ergebnis = self.client.personen_zusammenfuehren(
                    self.ziel_id, list(self.wert))
        except ImmichError as exc:
            status = getattr(exc, "status", 0)
            # Erwartbare Faelle benennen, statt die rohe Meldung zu
            # zeigen: 403 heisst fast immer, dass am Schluessel das
            # Schreibrecht fehlt - das ist in den Einstellungen
            # abzuhaken.
            if status == 403:
                recht = ("person.update" if self.aktion == "umbenennen"
                         else "person.merge")
                grund = (f"Dem API-Schlüssel fehlt das Recht {recht} — "
                         "in Immich unter Kontoeinstellungen → "
                         "API-Schlüssel nachtragen")
            elif status == 404 and self.aktion != "umbenennen":
                grund = "Dieser Server kann Personen nicht zusammenführen"
            else:
                grund = str(exc)
        except Exception as exc:
            grund = str(exc)
            crashlog.protokolliere("Personen ändern")
        try:
            self.signale.person_fertig.emit(self.aktion, self.ziel_id,
                                            ergebnis, grund)
        except RuntimeError:
            pass


class _SucheTask(QRunnable):
    """Fragt den Server und meldet die gefundenen Kennungen zurueck.

    `anlass` sagt dem Fenster, wozu die Antwort gehoert - kommt
    inzwischen eine andere Frage, wird die alte Antwort verworfen.
    """

    def __init__(self, client, anlass: str, art: str, wert: str,
                 signale: _ServerSignale) -> None:
        super().__init__()
        self.client = client
        self.anlass = anlass
        self.art = art
        self.wert = wert
        self.signale = signale

    def run(self) -> None:
        kennungen, grund = [], ""
        try:
            if self.art == "ort":
                treffer = self.client.suche_metadaten(city=self.wert)
            else:
                treffer = self.client.suche_klug(self.wert)
            kennungen = [t["id"] for t in treffer if t.get("id")]
        except ImmichError as exc:
            # Auch hier erwartbar: 404 heisst, der Server kann die kluge
            # Suche nicht (kein ML-Dienst), 403 heisst fehlendes Recht.
            grund = ("Dieser Server kann die Suche in normaler Sprache nicht"
                     if getattr(exc, "status", 0) == 404 else str(exc))
        except Exception as exc:
            grund = str(exc)
            crashlog.protokolliere("Serversuche")
        try:
            self.signale.treffer_da.emit(self.anlass, kennungen, grund)
        except RuntimeError:
            pass


class _VorschauTask(QRunnable):
    """Holt die grosse Server-Vorschau, ohne das Fenster anzuhalten.

    Eine Ausnahme hier erreicht sys.excepthook NICHT (das war die
    Luecke aus 0.3.27) - deshalb wird alles gefangen und protokolliert.
    """

    def __init__(self, kennung: str, client, signale: _VorschauSignale) -> None:
        super().__init__()
        self.kennung = kennung
        self.client = client
        self.signale = signale

    def run(self) -> None:
        if self.kennung != self.signale.gewuenscht:
            return          # weitergeblaettert - gar nicht erst holen
        daten = None
        try:
            daten = remote_thumbs.fetch(self.client, self.kennung, gross=True)
            if not daten:
                daten = remote_thumbs.fetch(self.client, self.kennung)
        except Exception:
            crashlog.protokolliere("Server-Vorschau")
        try:
            self.signale.fertig.emit(self.kennung, daten)
        except RuntimeError:
            pass          # Fenster ist inzwischen zu



def _sicherer_dateiname(name: str) -> str:
    """Aus einem Namen VOM SERVER einen harmlosen Dateinamen machen.

    `filename` kommt aus der Immich-Antwort - also von aussen. Direkt an
    den Zielordner gehaengt, bricht „../../x.jpg" oder „C:\\Windows\\x.jpg"
    daraus aus (nachgemessen, unter Windows auch mit Rueckstrichen).
    Deshalb bleibt hier nur der reine Name uebrig.
    """
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = name.split(":")[-1]                       # 'C:datei.jpg'
    name = "".join(z for z in name if z not in '<>:"|?*' and ord(z) >= 32)
    name = name.strip(" .")                          # Windows mag beides nicht
    stamm = name.split(".")[0].upper()
    reserviert = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
        | {f"LPT{i}" for i in range(1, 10)}
    if not name or stamm in reserviert:
        name = f"bild_{name}" if name else "bild.jpg"
    return name[:200]


def _freier_name(pfad: Path) -> Path:
    """Nie eine vorhandene Datei ueberschreiben - Wimmich loescht nichts."""
    if not pfad.exists():
        return pfad
    stamm, endung = pfad.stem, pfad.suffix
    nummer = 1
    while True:
        kandidat = pfad.with_name(f"{stamm} ({nummer}){endung}")
        if not kandidat.exists():
            return kandidat
        nummer += 1


class ServerbilderMixin:
    """Server-Griffe des Hauptfensters. Wird von MainWindow geerbt."""

    def _download_remote(self, rows: list[int]) -> None:
        """Originale vom Server holen - nur auf ausdruecklichen Wunsch.

        Die Dateien landen in einem Ordner, den Harald waehlt. Wimmich
        legt sie NUR ab; eingelesen werden sie erst beim naechsten
        Durchlauf, wenn der Ordner zur Bibliothek gehoert.
        """
        if not rows:
            return
        ziel = QFileDialog.getExistingDirectory(
            self, "Wohin sollen die Originale?",
            self.config.libraries[0] if self.config.libraries else "")
        if not ziel:
            return

        client = ImmichClient(self.config["immich_url"], self.config["immich_key"])
        try:
            client.connect()
        except ImmichError as exc:
            QMessageBox.critical(self, "Immich nicht erreichbar", str(exc))
            return

        geholt, fehler = 0, []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for nummer, row in enumerate(rows, start=1):
                item = self.model.row_data(row) or {}
                if not item.get("_remote"):
                    continue
                self.statusBar().showMessage(
                    f"Lade herunter: {item['filename']} ({nummer} von {len(rows)})")
                QApplication.processEvents()
                try:
                    daten = client.download_original(item["immich_id"])
                except ImmichError as exc:
                    fehler.append(f"{item['filename']}: {exc}")
                    continue
                if not daten:
                    fehler.append(f"{item['filename']}: nichts erhalten")
                    continue
                pfad = _freier_name(Path(ziel) / _sicherer_dateiname(item["filename"]))
                try:
                    pfad.write_bytes(daten)
                    geholt += 1
                except OSError as exc:
                    fehler.append(f"{item['filename']}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

        self.statusBar().showMessage(
            f"{geholt} Original(e) nach {ziel} geholt"
            + (f", {len(fehler)} fehlgeschlagen" if fehler else ""), 8000)
        if fehler:
            QMessageBox.warning(
                self, "Nicht alles geholt",
                "\n".join(fehler[:10]) + ("\n…" if len(fehler) > 10 else ""))

    # -- Serverbilder: bearbeiten und löschen ---------------------------

    def _immich_verbunden(self) -> ImmichClient | None:
        """Verbundener Client oder None samt Meldung an den Benutzer."""
        client = ImmichClient(self.config["immich_url"], self.config["immich_key"])
        if not client.configured:
            QMessageBox.information(self, "Immich",
                                    "Immich ist nicht eingerichtet.")
            return None
        # ERST die kurze Frage, ob ueberhaupt jemand antwortet. Ohne sie
        # steht das Fenster bis zu 60 s in client.connect(), wenn der
        # Server aus ist - genau das Einfrieren, das Harald gemeldet hat.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            erreichbar, grund = client.erreichbar()
        finally:
            QApplication.restoreOverrideCursor()
        if not erreichbar:
            QMessageBox.critical(
                self, "Immich nicht erreichbar",
                f"{self.config['immich_url']} antwortet nicht.\n\n{grund}")
            return None
        try:
            client.connect()
        except ImmichError as exc:
            QMessageBox.critical(self, "Immich nicht erreichbar", str(exc))
            return None
        return client

    def _zielordner(self) -> str | None:
        """Wohin geholte Originale kommen - einmal wählen, dann gemerkt.

        Liegt der Ordner in keiner Bibliothek, taucht das Bild nachher
        nirgends auf. Deshalb wird angeboten, ihn aufzunehmen.
        """
        ziel = str(self.config["download_dir"] or "")
        if not ziel or not Path(ziel).is_dir():
            start = self.config.libraries[0] if self.config.libraries else ""
            ziel = QFileDialog.getExistingDirectory(
                self, "Wohin sollen geholte Originale?", start)
            if not ziel:
                return None
            self.config["download_dir"] = ziel

        if not any(str(Path(ziel)).startswith(str(Path(lib)))
                   for lib in self.config.libraries):
            antwort = QMessageBox.question(
                self, "Ordner gehört nicht zur Bibliothek",
                f"{ziel}\n\nDieser Ordner ist keine Bibliothek von Wimmich. "
                "Ohne ihn erscheint das geholte Bild in keiner Ansicht.\n\n"
                "Ordner jetzt aufnehmen?")
            if antwort == QMessageBox.StandardButton.Yes:
                self.config.add_library(ziel)
                self._reload_folder_tree()
                self._apply_watch_settings()
        return ziel

    def _original_holen(self, item: dict, client: ImmichClient) -> Path | None:
        """Ein Original vom Server holen, ablegen und einlesen."""
        ziel = self._zielordner()
        if not ziel:
            return None
        try:
            daten = client.download_original(item["immich_id"])
        except ImmichError as exc:
            QMessageBox.critical(self, "Nicht geholt", str(exc))
            return None
        if not daten:
            QMessageBox.warning(self, "Nicht geholt",
                                f"{item['filename']}: nichts erhalten")
            return None

        pfad = _freier_name(Path(ziel) / _sicherer_dateiname(item["filename"]))
        try:
            pfad.write_bytes(daten)
        except OSError as exc:
            QMessageBox.critical(self, "Nicht gespeichert", str(exc))
            return None
        self._indiziere(pfad, item["immich_id"])
        return pfad

    def _indiziere(self, pfad: Path, immich_id: str) -> None:
        """Frisch geholte Datei sofort in den Index aufnehmen.

        Ohne das müsste erst F5 laufen, bevor das Bild sichtbar wird.
        Die Immich-Kennung wird gleich mit eingetragen - dadurch fällt
        das Bild aus „Nur auf dem Server" heraus (dort steht nur, wozu
        es KEINE lokale Datei gibt) und wird beim nächsten Abgleich
        nicht erneut hochgeladen.
        """
        st = pfad.stat()
        photo_id, _neu = self.db.upsert_file(
            str(pfad), str(pfad.parent), pfad.name, pfad.suffix.lower(),
            is_raw(pfad), st.st_size, st.st_mtime)

        meta: dict = {}
        if not is_raw(pfad):
            try:
                meta = read_fast([str(pfad)]).get(str(pfad), {})
            except Exception:
                meta = {}
        if not meta and self.exiftool.available:
            try:
                meta = self.exiftool.read_batch([str(pfad)]).get(str(pfad), {})
            except Exception:
                meta = {}
        self.db.store_metadata(photo_id, meta or {})
        self.db.set_immich(photo_id, immich_id)
        self.db.commit()

    def _serverbild_bearbeiten(self, row: int) -> None:
        """Serverbild bearbeitbar machen: Original holen, dann lokal öffnen.

        Bearbeitet wird bei Wimmich immer eine DATEI - die Schrittfolge
        hängt am Pfad, und die Ausgabe rechnet in voller Auflösung neu.
        Ein Bild, das nur auf dem Server liegt, hat beides nicht. Statt
        die Vorschau zu verbiegen, wird deshalb das Original geholt,
        eingelesen und ganz normal als lokales Bild geöffnet.
        """
        item = self.model.row_data(row) or {}
        if not item.get("_remote"):
            return
        client = self._immich_verbunden()
        if client is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Hole Original: {item['filename']} …")
        try:
            pfad = self._original_holen(item, client)
        finally:
            QApplication.restoreOverrideCursor()
        if pfad is None:
            return

        self._refresh_view()
        ziel = self._zeile_mit_pfad(str(pfad))
        if ziel < 0:
            # Wir stehen noch in „Nur auf dem Server" - dort taucht die
            # frisch geholte Datei naturgemaess nicht auf. Also in den
            # Ordner wechseln, in dem sie jetzt liegt.
            self._zeige_ordner(pfad.parent)
            ziel = self._zeile_mit_pfad(str(pfad))
        self.statusBar().showMessage(
            f"{pfad.name} liegt jetzt in {pfad.parent} und ist bearbeitbar", 8000)
        if ziel >= 0:
            self._show_loupe(ziel)

    def _serverbilder_loeschen(self, rows: list[int]) -> None:
        """Bilder auf dem Immich-Server löschen - nach Rückfrage.

        Lokale Dateien rührt Wimmich weiterhin nicht an; hier geht es
        ausschließlich um Aufnahmen, die es NUR auf dem Server gibt.
        """
        eintraege = [self.model.row_data(r) or {} for r in rows]
        ids = [e["immich_id"] for e in eintraege if e.get("_remote")]
        if not ids:
            return
        namen = ", ".join(e["filename"] for e in eintraege[:5] if e.get("_remote"))
        antwort = QMessageBox.question(
            self, "Auf dem Server löschen",
            f"{len(ids)} Aufnahme(n) auf dem Immich-Server löschen?\n\n"
            f"{namen}{' …' if len(ids) > 5 else ''}\n\n"
            "Sie landen im Papierkorb von Immich und lassen sich dort "
            "zurückholen. Lokale Dateien sind nicht betroffen.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if antwort != QMessageBox.StandardButton.Yes:
            return

        client = self._immich_verbunden()
        if client is None:
            return
        try:
            client.delete_assets(ids)
        except ImmichError as exc:
            QMessageBox.critical(self, "Nicht gelöscht", str(exc))
            return
        self.db.forget_remote(ids)
        if self.in_loupe:
            self._show_grid()
        self._refresh_view()
        self.statusBar().showMessage(
            f"{len(ids)} Aufnahme(n) auf dem Server gelöscht "
            "(Papierkorb von Immich)", 8000)

    def _load_remote_loupe(self, row: int, item: dict) -> None:
        """Grossansicht eines Bildes, das nur auf dem Server liegt.

        Gezeigt wird die groessere Server-Vorschau, NICHT das Original -
        das holt Wimmich nur auf ausdruecklichen Wunsch. Bearbeiten ist
        hier ausgeschaltet: es gibt keine Datei, in die etwas
        zurueckgeschrieben werden koennte.
        """
        self._loupe_row = row
        self._loupe_path = ""
        self._want_full = False
        self._show_before = False
        self._stroke = []
        self.panel.sperre(
            "Dieses Bild liegt nur auf dem Server. Bearbeiten braucht "
            "eine Datei — mit „Original holen und bearbeiten“ (oder dem "
            "Stift oben) holt Wimmich sie in deine Bibliothek.")
        self.remote_edit_button.setVisible(True)
        self.remote_delete_button.setVisible(True)
        # Reste des vorigen Bildes wegräumen. Sonst zeigen Zuschnitt,
        # Vorher/Nachher und die Regler noch auf dessen Daten - genau
        # daran sind Aktionen wie „Zuschnitt aufheben" abgestürzt.
        self._stack_edits = EditStack()
        self._loupe_small = None
        self._crop_mode = False
        self.crop_bar.setVisible(False)
        self._set_tool(TOOL_NONE)

        # ERST das, was ohne Netz da ist: Cache, sonst die Kachel aus
        # dem Raster. Der Server wird NUR im Hintergrund gefragt - ein
        # nicht erreichbarer Server hat frueher das ganze Fenster
        # angehalten, weil hier auf die Zeitgrenze gewartet wurde.
        kennung = item["immich_id"]
        self._remote_wartet = kennung
        bild = None
        for daten in (remote_thumbs.cached(kennung, gross=True),
                      remote_thumbs.cached(kennung, gross=False)):
            if not daten:
                continue
            versuch = previews.lade_qimage_aus_bytes(daten)
            if not versuch.isNull():
                bild = versuch
                break
        if bild is None:
            bild = self.model.kachel_bild(row)

        if bild is None or bild.isNull():
            self.canvas.clear_image()
            self.canvas.set_info_overlay("Vorschau wird geholt …")
            self.canvas.show_info_overlay(True)
        else:
            self.canvas.set_image(bild)
            self.canvas.show_info_overlay(self._info_visible)

        # Die groessere Vorschau kommt nach, sobald sie da ist. Aeltere
        # Auftraege in der Warteschlange erledigen sich von selbst: sie
        # sehen beim Start, dass ein anderes Bild gefragt ist.
        self._vorschau_signale.gewuenscht = kennung
        client = getattr(self.model, "_immich_client", None)
        self._pool.start(_VorschauTask(kennung, client, self._vorschau_signale))

        # Spaeter Import: mainwindow laedt DIESE Datei, ein Import oben
        # waere ein Kreis. Zur Aufrufzeit ist alles fertig geladen.
        from .mainwindow import _html_escape
        self.loupe_title.setText(
            f'<b>{_html_escape(item["filename"])}</b>'
            f'&nbsp;&nbsp;&nbsp;<span style="color:{theme.TEXT_MUTED}">'
            f'nur auf dem Server — Vorschau, nicht das Original</span>')
        self.setWindowTitle(
            f"{APP_NAME} {__version__} — {item['filename']} (nur auf dem Server)")
        self.canvas.set_overlay("Nur auf dem Server")

    def _vorschau_da(self, kennung: str, daten: object) -> None:
        """Die im Hintergrund geholte Server-Vorschau ist eingetroffen.

        Sie wird nur uebernommen, wenn immer noch dasselbe Bild in der
        Lupe steht - sonst hat der Anwender laengst weitergeblaettert.
        """
        if kennung != getattr(self, "_remote_wartet", ""):
            return
        bild = previews.lade_qimage_aus_bytes(daten) if daten else None
        if bild is not None and not bild.isNull():
            self.canvas.set_image(bild)
            self.canvas.show_info_overlay(self._info_visible)
            return
        if self.canvas.has_image():
            return          # die Kachel steht schon da - kein Gemecker
        grund = remote_thumbs.letzte_meldung()
        if remote_thumbs.server_gilt_als_tot():
            grund = grund or "Server nicht erreichbar"
        self.canvas.set_info_overlay(
            f"Keine Vorschau vom Server{(' — ' + grund) if grund else ''}")
        self.canvas.show_info_overlay(True)

    def _remote_aktiv(self) -> bool:
        """Steht gerade ein reines Serverbild in der Lupe?

        Für solche Bilder gibt es keine lokale Datei: Zuschnitt, Regler
        und Retusche haben nichts, worauf sie wirken könnten.
        """
        item = self.model.row_data(self._loupe_row) if self.in_loupe else None
        return bool(item and item.get("_remote"))

    def _apply_remote_client(self) -> None:
        """Dem Rastermodell einen Client fuer Server-Vorschauen geben.

        Ohne ihn blieben die Kacheln der reinen Serverbilder leer. Der
        Client wird NUR fuer Vorschauen benutzt; Originale holt allein
        _download_remote() auf ausdruecklichen Wunsch.
        """
        url, key = self.config["immich_url"], self.config["immich_key"]
        self.model._immich_client = None
        if not (url and key):
            return
        # NICHT hier verbinden: das lief im Fensterfaden und hielt beim
        # Start alles an, solange der Server nicht antwortete. Die
        # Vorabpruefung und das Verbinden laufen im Hintergrund; bis sie
        # durch sind, zeigt Wimmich einfach die lokalen Bilder.
        self._pool.start(_ClientTask(url, key, self._vorschau_signale))

    def _client_da(self, client, grund: str) -> None:
        """Ergebnis der Hintergrundpruefung uebernehmen."""
        self.model._immich_client = client
        if client is None and grund:
            self.sync_label.setText(f"Server nicht erreichbar: {grund}")
            self.sync_label.setVisible(True)
        elif client is not None:
            self.sync_label.setVisible(False)
        self._serversuche_freigeben(client is not None)
        if client is not None:
            self._orte_holen()

    # -- Personen verwalten ---------------------------------------------

    def _gesichter_holen(self) -> None:
        """Fuer jede BENANNTE Person das Bildchen an den Baum haengen.

        Schon geholte Symbole liegen in `_gesicht_cache` und werden
        SOFORT gesetzt - sonst liefe bei jedem Neuaufbau des Baums (nach
        jedem Abgleich, nach jedem Umbenennen) fuer jede Person wieder
        eine Aufgabe an, nur um dieselbe Datei noch einmal zu lesen.
        """
        client = self.model._immich_client
        for i in range(self._people_root.childCount()):
            eintrag = self._people_root.child(i)
            daten = eintrag.data(0, Qt.ItemDataRole.UserRole)
            if not daten or daten[0] != "person":
                continue
            symbol = self._gesicht_cache.get(daten[1])
            if symbol is not None:
                eintrag.setIcon(0, symbol)
            elif client is not None:
                self._pool.start(_GesichtTask(client, daten[1],
                                              self._server_signale))

    def _person_bild(self, person_id: str, pfad: str) -> None:
        """Bildchen merken und an den passenden Baumeintrag haengen."""
        from PyQt6.QtGui import QIcon
        symbol = QIcon(pfad)
        if symbol.isNull():
            return
        self._gesicht_cache[person_id] = symbol
        for i in range(self._people_root.childCount()):
            eintrag = self._people_root.child(i)
            daten = eintrag.data(0, Qt.ItemDataRole.UserRole)
            if daten and daten[0] == "person" and daten[1] == person_id:
                eintrag.setIcon(0, symbol)
                return

    def _person_name(self, person_id: str) -> str:
        for row in self.db.people(include_unnamed=True):
            if row["id"] == person_id:
                return row["name"] or "(ohne Namen)"
        return "(unbekannt)"

    def _person_umbenennen(self, person_id: str) -> None:
        client = self.model._immich_client
        if client is None:
            return
        alt = self._person_name(person_id)
        name, ok = QInputDialog.getText(
            self, "Person umbenennen", "Neuer Name:",
            text="" if alt == "(ohne Namen)" else alt)
        name = name.strip()
        if not ok or not name or name == alt:
            return
        self.statusBar().showMessage(
            f"\u201e{alt}\u201c wird umbenannt …", 5000)
        self._pool.start(_PersonTask(client, "umbenennen", person_id, name,
                                     self._server_signale))

    def _person_zusammenfuehren(self, ziel_id: str) -> None:
        """Andere Personen in DIESE hineinziehen.

        Richtung ausdruecklich benannt: das Ziel bleibt bestehen, die
        gewaehlte Person geht darin auf. Immich macht es genauso -
        POST /people/{ziel}/merge mit den Quellen im Rumpf.
        """
        client = self.model._immich_client
        if client is None:
            return
        ziel = self._person_name(ziel_id)
        andere = [(row["id"], row["name"]) for row in self.db.people()
                  if row["id"] != ziel_id and row["name"]]
        if not andere:
            QMessageBox.information(
                self, "Zusammenführen",
                "Es gibt keine zweite benannte Person zum Zusammenführen.")
            return
        namen = [n for _, n in andere]
        wahl, ok = QInputDialog.getItem(
            self, "Personen zusammenführen",
            f"Wer soll in \u201e{ziel}\u201c aufgehen?\n"
            "Die gewählte Person verschwindet, ihre Gesichter hängen "
            f"danach an \u201e{ziel}\u201c.",
            namen, 0, False)
        if not ok or not wahl:
            return
        quelle = next(i for i, n in andere if n == wahl)
        if QMessageBox.question(
                self, "Zusammenführen",
                f"\u201e{wahl}\u201c in \u201e{ziel}\u201c aufgehen lassen?\n"
                "Das geschieht auch auf dem Server und lässt sich nicht "
                "zurücknehmen.") != QMessageBox.StandardButton.Yes:
            return
        self.statusBar().showMessage("Personen werden zusammengeführt …", 5000)
        self._pool.start(_PersonTask(client, "zusammenfuehren", ziel_id,
                                     [quelle], self._server_signale))

    def _gesicht_einordnen(self, person_id: str) -> None:
        """Ein unbenanntes Gesicht in eine BENANNTE Person schieben.

        Umgekehrte Richtung zum Baum: hier ist die angeklickte Kachel die
        QUELLE, die benannte Person das Ziel - sonst muesste man das
        namenlose Haeufchen erst benennen, nur um es gleich wieder
        verschwinden zu lassen.
        """
        client = self.model._immich_client
        if client is None:
            return
        benannte = [(row["id"], row["name"]) for row in self.db.people()
                    if row["name"]]
        if not benannte:
            QMessageBox.information(
                self, "Einordnen",
                "Es gibt noch keine benannte Person, in die das Gesicht "
                "passen könnte.")
            return
        namen = [n for _, n in benannte]
        wahl, ok = QInputDialog.getItem(
            self, "Gesicht einordnen", "Zu welcher Person gehört es?",
            namen, 0, False)
        if not ok or not wahl:
            return
        ziel = next(i for i, n in benannte if n == wahl)
        self.statusBar().showMessage("Gesicht wird eingeordnet …", 5000)
        self._pool.start(_PersonTask(client, "zusammenfuehren", ziel,
                                     [person_id], self._server_signale))

    def _person_fertig(self, aktion: str, ziel_id: str, ergebnis,
                       grund: str) -> None:
        """Antwort des Servers uebernehmen - erst jetzt den Spiegel."""
        if grund:
            QMessageBox.warning(self, "Personen", grund)
            return
        if aktion == "umbenennen":
            self.db.person_umbenennen(ziel_id, ergebnis)
            meldung = f"Person heißt jetzt \u201e{ergebnis}\u201c"
        else:
            gelungen, gescheitert = ergebnis
            self.db.personen_zusammenfuehren(ziel_id, gelungen)
            meldung = (f"{len(gelungen)} Person(en) zusammengeführt"
                       + (f", {len(gescheitert)} nicht" if gescheitert else ""))
        self._reload_immich_tree()
        # Steht gerade die Ansicht der Unbenannten offen, ist die eben
        # benannte Person dort raus - die Kachel muss weg.
        if self._selection and self._selection[0] in ("unbenannt", "person"):
            self._refresh_view()
        self.statusBar().showMessage(meldung, 8000)

    # -- Serversuche ----------------------------------------------------

    def _serversuche_freigeben(self, an: bool) -> None:
        """Suchfeld und Erkunden-Zweig sperren, wenn kein Server da ist.

        Ausgrauen statt still leer lassen: eine leere Ortsliste sieht
        sonst aus, als gaebe es keine Aufnahmen aus Cannes.
        """
        self.server_suche.setEnabled(an)
        self.server_suche.setPlaceholderText(
            "Auf dem Server suchen …" if an
            else "Serversuche: kein Server erreichbar")
        self._explore_root.setDisabled(not an)
        if not an:
            self._places_root.takeChildren()
            hinweis = QTreeWidgetItem(["— Server nicht erreichbar —"])
            hinweis.setData(0, Qt.ItemDataRole.UserRole, None)
            hinweis.setFlags(Qt.ItemFlag.NoItemFlags)
            self._places_root.addChild(hinweis)

    def _orte_holen(self) -> None:
        client = getattr(self.model, "_immich_client", None)
        if client is None:
            return
        self._pool.start(_OrteTask(client, self._server_signale))

    def _orte_da(self, orte: list, grund: str) -> None:
        """Ortsliste in den Baum haengen."""
        gewaehlt = self._selection
        self._places_root.takeChildren()
        for ort in orte:
            eintrag = QTreeWidgetItem([ort])
            eintrag.setData(0, Qt.ItemDataRole.UserRole, ("ort", ort))
            self._places_root.addChild(eintrag)
        if not orte:
            hinweis = QTreeWidgetItem(
                [f"— {grund} —" if grund else "— keine Orte gefunden —"])
            hinweis.setData(0, Qt.ItemDataRole.UserRole, None)
            hinweis.setFlags(Qt.ItemFlag.NoItemFlags)
            self._places_root.addChild(hinweis)
        self._places_root.setExpanded(False)
        self._auswahl_wiederherstellen(gewaehlt)

    def _server_suche_starten(self) -> None:
        """Enter im Suchfeld: den Server in normaler Sprache fragen."""
        text = self.server_suche.text().strip()
        if not text:
            return
        client = getattr(self.model, "_immich_client", None)
        if client is None:
            self.statusBar().showMessage(
                "Für die Serversuche muss Immich erreichbar sein.", 5000)
            return
        self._selection = ("suche", text)
        self.tree.clearSelection()
        self._serversuche_ausloesen("klug", text)

    def _serversuche_ausloesen(self, art: str, wert: str) -> None:
        """Frage abschicken und so lange den Wartehinweis zeigen."""
        client = getattr(self.model, "_immich_client", None)
        if client is None:
            self._server_treffer = []
            self._refresh_view()
            return
        self._such_anlass = f"{art}:{wert}"
        self.statusBar().showMessage(f"Server wird gefragt: {wert} …", 0)
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._pool.start(_SucheTask(client, self._such_anlass, art, wert,
                                    self._server_signale))

    def _treffer_da(self, anlass: str, kennungen: list, grund: str) -> None:
        """Antwort des Servers - nur uebernehmen, wenn sie noch gilt."""
        if anlass != getattr(self, "_such_anlass", ""):
            return
        QApplication.restoreOverrideCursor()
        self._such_anlass = ""
        self._server_treffer = list(kennungen)
        if grund:
            self.statusBar().showMessage(f"Serversuche fehlgeschlagen: {grund}",
                                         8000)
        else:
            self.statusBar().showMessage(
                f"{len(kennungen)} Treffer auf dem Server", 5000)
        self._refresh_view()

    def _apply_sync_settings(self) -> None:
        """Zeitgeber nach den Einstellungen an- oder abschalten."""
        auto = (bool(self.config["immich_auto"])
                and bool(self.config["immich_url"])
                and bool(self.config["immich_key"]))
        if auto:
            minutes = max(1, int(self.config["immich_interval_min"]))
            self._sync_timer.start(minutes * 60 * 1000)
        else:
            self._sync_timer.stop()

    def _auto_sync(self) -> None:
        """Regelmäßiger Durchlauf. Still: keine Fenster, keine Störung."""
        if self._sync_thread is not None or self._scan_thread is not None:
            return
        if not (self.config["immich_url"] and self.config["immich_key"]):
            return
        self._start_sync(quiet=True)

    def _start_sync(self, quiet: bool = False) -> None:
        """quiet=True: der laufende Abgleich. Meldet sich nur in der
        Statuszeile und öffnet bei Fehlern kein Fenster - sonst würde
        ein kurz nicht erreichbarer Server alle 15 Minuten stören."""
        if self._sync_thread is not None:
            if not quiet:
                self.statusBar().showMessage("Der Abgleich läuft bereits", 3000)
            return
        if not (self.config["immich_url"] and self.config["immich_key"]):
            if not quiet:
                self._open_settings()
            return
        self._sync_quiet = quiet

        thread = QThread(self)
        worker = SyncWorker(
            self.config["immich_url"], self.config["immich_key"],
            upload=bool(self.config["immich_upload"]),
            fetch_people=bool(self.config["immich_people"]),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._sync_progress)
        worker.finished.connect(self._sync_finished)
        worker.failed.connect(self._sync_failed)
        thread.start()

        self._sync_thread = thread
        self._sync_worker = worker
        self.sync_progress_bar.setRange(0, 0)   # unbestimmt, bis der erste Wert kommt
        self.sync_progress_bar.setValue(0)
        self.sync_progress_bar.setVisible(True)
        self.sync_label.setText("Abgleich mit Immich läuft …")
        self.sync_label.setVisible(True)

    def _sync_progress(self, message: str, done: int, total: int) -> None:
        self.sync_label.setText(message)
        self.sync_label.setVisible(True)
        if total > 0:
            self.sync_progress_bar.setRange(0, total)
            self.sync_progress_bar.setValue(min(done, total))
        else:
            # Kein Gesamtwert bekannt (z. B. beim Verbinden oder zwischen
            # Alben/Personen) - unbestimmter Balken statt eingefrorener Wert.
            self.sync_progress_bar.setRange(0, 0)

    def _sync_finished(self, result) -> None:
        self._teardown_sync()
        self._reload_immich_tree()
        self._refresh_view()
        self._update_status()

        parts = [f"{result.checked} geprüft"]
        if result.uploaded:
            parts.append(f"{result.uploaded} hochgeladen")
        if result.already_there:
            parts.append(f"{result.already_there} waren schon da")
        if result.skipped:
            parts.append(f"{result.skipped} übersprungen (Typ nicht angenommen)")
        if result.failed:
            parts.append(f"{result.failed} fehlgeschlagen")
        self.sync_label.setText("Abgleich fertig: " + ", ".join(parts))
        self.sync_label.setVisible(True)

        self._apply_remote_client()
        if result.errors and not self._sync_quiet:
            QMessageBox.warning(
                self, "Abgleich mit Fehlern",
                "Diese Dateien konnten nicht übertragen werden:\n\n"
                + "\n".join(result.errors[:10])
                + ("\n…" if len(result.errors) > 10 else ""),
            )
        elif result.errors:
            # Auch im stillen Modus darf ein dauerhaft scheiternder Abgleich
            # nicht spurlos bleiben - sonst laeuft er wochenlang ins Leere.
            self.statusBar().showMessage(
                f"Abgleich mit {result.failed} Fehlern: {result.errors[0]}", 15000)

    def _sync_failed(self, message: str) -> None:
        quiet = self._sync_quiet
        self._teardown_sync()
        if quiet:
            # Still weiterlaufen lassen: beim nächsten Durchlauf wird
            # erneut versucht. Nur die Statuszeile sagt Bescheid.
            self.sync_label.setText(f"Abgleich nicht möglich: {message}")
            self.sync_label.setVisible(True)
        else:
            QMessageBox.critical(self, "Abgleich fehlgeschlagen", message)
            self.statusBar().clearMessage()

    def _teardown_sync(self) -> None:
        if self._sync_thread is not None:
            self._sync_thread.quit()
            self._sync_thread.wait(5000)
        self._sync_thread = None
        self._sync_worker = None
        # Balken noch kurz stehen lassen. Ein Abgleich, bei dem schon alles
        # zugeordnet ist, ist in Sekundenbruchteilen vorbei - der Balken
        # waere sonst nur ein unsichtbares Aufblitzen, und es sieht aus,
        # als sei nie etwas passiert.
        self.sync_progress_bar.setRange(0, 1)
        self.sync_progress_bar.setValue(1)
        QTimer.singleShot(2000, lambda: self.sync_progress_bar.setVisible(
            self._sync_thread is not None))
        # Das Ergebnis bleibt STEHEN, bis der naechste Abgleich es
        # ersetzt. Frueher verschwand es nach 30 Sekunden - wer in der
        # Zeit nicht hinsah, erfuhr nie, wie der Abgleich ausgegangen ist.

    def _zeige_serverfehler(self, grund: str) -> None:
        """Ausbleibende Server-Vorschauen sichtbar machen.

        Vorher blieb die Kachel einfach grau - kein Hinweis, kein
        Logeintrag, nichts zum Nachgehen. Der Text steht in der
        Statuszeile, bis etwas anderes ihn ersetzt.
        """
        self.sync_label.setText(f"Server-Vorschau kommt nicht an — {grund}")
        self.sync_label.setVisible(True)
