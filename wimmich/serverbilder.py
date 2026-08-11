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
    QApplication, QFileDialog, QMessageBox,
)

from . import APP_NAME, __version__, crashlog, previews, remote_thumbs, theme
from .canvas import NONE as TOOL_NONE
from .config import is_raw
from .edits import EditStack
from .exif import read_fast
from .immich import ImmichClient, ImmichError
from .sync import SyncWorker


class _VorschauSignale(QObject):
    """Traegt das Ergebnis aus dem Hintergrundfaden zurueck ins Fenster.

    `gewuenscht` ist die Kennung des Bildes, das GERADE in der Lupe
    steht. Beim schnellen Blaettern mit totem Server stapeln sich sonst
    Auftraege, die je bis zu 8 Sekunden warten - jeder fuer ein Bild,
    das laengst niemand mehr sieht.
    """

    fertig = pyqtSignal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.gewuenscht = ""


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
        self._loupe_full = None
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
        if url and key:
            client = ImmichClient(url, key)
            try:
                client.connect()
            except ImmichError:
                client = None       # spaeter erneut versuchen
        else:
            client = None
        self.model._immich_client = client
        self._remote_item.setHidden(client is None and not self.db.remote_count())

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
