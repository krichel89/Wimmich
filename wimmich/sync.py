"""Abgleich mit Immich im Hintergrund.

Ablauf, entsprechend der Grundregel „lokale Ordner sind führend":

  1. Für lokale Bilder ohne Serverkennung die SHA-1-Prüfsumme bilden.
  2. Stapelweise beim Server anfragen, was davon schon dort liegt
     (``/assets/bulk-upload-check``). Das ist der günstige Teil - es
     wandern nur Prüfsummen über die Leitung, keine Bilder.
  3. Nur die tatsächlich fehlenden Dateien hochladen. Ein vorhandener
     XMP-Sidecar wird mitgeschickt, damit Bewertung und Farbmarkierung
     gleich mitkommen.
  4. Alben und Personen vom Server holen und lokal spiegeln.

Was auf dem Server liegt, aber nicht lokal, wird NICHT heruntergeladen.
Immich ist der Spiegel, nicht die Quelle.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .config import DB_PATH
from .db import Database
from .immich import (ImmichClient, ImmichError, SyncResult, file_checksum,
                     ist_dauerhafter_fehler)

CHECK_BATCH = 100        # so viele Prüfsummen je Anfrage
PROGRESS_INTERVAL = 0.3  # Sekunden zwischen zwei Fortschrittsmeldungen
PARALLEL_VORGABE = 4     # gleichzeitige Uploads
ABBRUCH_NACH_FEHLERN = 5  # so viele Fehlversuche hintereinander, dann Schluss


class SyncWorker(QObject):
    """Läuft in einem eigenen QThread."""

    progress = pyqtSignal(str, int, int)     # Meldung, erledigt, gesamt
    finished = pyqtSignal(object)            # SyncResult
    failed = pyqtSignal(str)

    def __init__(self, base_url: str, api_key: str, upload: bool = True,
                 fetch_albums: bool = True, fetch_people: bool = True,
                 fetch_remote: bool = True, parallel: int = PARALLEL_VORGABE) -> None:
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key
        self._upload = upload
        self._fetch_albums = fetch_albums
        self._fetch_people = fetch_people
        self._fetch_remote = fetch_remote
        self._parallel = max(1, min(8, int(parallel)))
        self._cancel = False
        self._db: Database | None = None
        self._last_progress = 0.0
        self._zurueckgestellt: set[int] = set()
        self._letzter_fehler = ""
        self._abgebrochen = False
        self._abgelehnte_typen: set[str] = set()

    def cancel(self) -> None:
        self._cancel = True

    def _report(self, message: str, done: int, total: int,
               force: bool = False) -> None:
        """Fortschritt melden, aber zeitlich gedrosselt.

        Ohne Drosselung würde ein schneller Abgleich (Prüfsummen aus dem
        Plattencache, kein Hochladen nötig) die Ereignisschleife mit
        Signalen fluten. force=True für den letzten Schritt, damit der
        Balken nie auf einem Zwischenstand stehen bleibt.
        """
        now = time.monotonic()
        if not force and (now - self._last_progress) < PROGRESS_INTERVAL:
            return
        self._last_progress = now
        self.progress.emit(message, done, total)

    @pyqtSlot()
    def run(self) -> None:
        result = SyncResult()
        try:
            client = ImmichClient(self._base_url, self._api_key)
            info = client.connect()
            self._report(
                f"Verbunden mit Immich {info.version or '?'}"
                + (f" als {info.user}" if info.user else ""), 0, 0, force=True)

            self._db = Database(DB_PATH)
            # Alben und Personen ZUERST: die sind in Sekunden da und
            # erscheinen sofort im Baum. Die Bilder danach koennen Stunden
            # dauern - stuenden sie davor, saehe Harald tagelang keine
            # Alben, obwohl der Abgleich laeuft.
            if not self._cancel and self._fetch_albums:
                self._sync_albums(client)
            if not self._cancel and self._fetch_people:
                self._sync_people(client)
            if not self._cancel and self._fetch_remote:
                self._sync_remote(client, result)
            self._db.commit()
            self._match_and_upload(client, result)

            # Eigene Albenaenderungen ZULETZT: erst jetzt haben die
            # gerade hochgeladenen Bilder eine Immich-Kennung.
            if not self._cancel and self._fetch_albums:
                self._push_albums(client, result)

            self._db.commit()
            self.finished.emit(result)
        except ImmichError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:      # Hintergrundthread darf nicht still sterben
            self.failed.emit(f"Unerwarteter Fehler: {exc}")
        finally:
            if self._db is not None:
                self._db.close()

    # -- Bilder --------------------------------------------------------

    def _match_and_upload(self, client: ImmichClient, result: SyncResult) -> None:
        total, synced = self._db.sync_counts()
        offen = total - synced
        if not offen:
            self._report("Alle Bilder sind bereits zugeordnet", 0, 0, force=True)
            return
        self._report(f"Abgeglichen: 0 von {offen}", 0, offen, force=True)

        done = 0
        misserfolge = 0
        while not self._cancel:
            rows = [r for r in self._db.unsynced(CHECK_BATCH + len(self._zurueckgestellt))
                    if r["id"] not in self._zurueckgestellt][:CHECK_BATCH]
            if not rows:
                break

            # Prüfsummen bilden (und merken - sie ändern sich nur mit der Datei).
            # Das ist die LANGSAMSTE Stelle des Abgleichs: jede Datei wird
            # ganz gelesen (45 MB RAW ≈ 41 ms auf schneller Platte, auf einer
            # Netzwerkfreigabe ein Vielfaches). Ohne Meldung hier stünde der
            # Balken minutenlang still, obwohl gearbeitet wird.
            items: list[tuple[str, str]] = []
            by_id: dict[str, dict] = {}
            for row in rows:
                if self._cancel:
                    break
                checksum = row["checksum"]
                if not checksum:
                    self._report(
                        f"Prüfsummen: {row['filename']} ({done} von {offen})",
                        done, offen)
                    try:
                        checksum = file_checksum(row["path"])
                    except OSError as exc:
                        result.failed += 1
                        result.errors.append(f"{row['filename']}: {exc}")
                        # Damit die Schleife nicht ewig dieselbe Datei zieht
                        self._db.set_immich(row["id"], "", None)
                        continue
                    self._db.set_checksum(row["id"], checksum)
                key = str(row["id"])
                items.append((key, checksum))
                by_id[key] = dict(row) | {"checksum": checksum}
            self._db.commit()

            if not items:
                continue

            self._report(f"Frage Server: {len(items)} Bilder ({done} von {offen})",
                         done, offen, force=True)
            known = client.check_uploaded(items)
            result.checked += len(items)

            # Erst alles erledigen, was ohne Netz geht
            hochzuladen: list[str] = []
            for key, asset_id in known.items():
                if self._cancel:
                    break
                row = by_id[key]
                if asset_id:
                    self._db.set_immich(int(key), asset_id, row["checksum"])
                    result.already_there += 1
                    done += 1
                elif self._upload:
                    hochzuladen.append(key)
                    continue
                else:
                    # Nicht hochladen: leere Kennung merken, damit die Datei
                    # nicht in jedem Durchlauf erneut geprüft wird.
                    self._db.set_immich(int(key), "", row["checksum"])
                    done += 1
                self._report(f"Abgeglichen: {done} von {offen}", done, offen)

            # Uploads GLEICHZEITIG: sie warten fast nur auf das Netz.
            # Gemessen bei 150 ms Serverantwort: viermal so schnell mit
            # vier Strängen. Die Datenbank wird weiterhin NUR hier im
            # Abgleichfaden beschrieben - sqlite-Verbindungen sind nicht
            # zwischen Fäden teilbar.
            for stapel in _haeppchen(hochzuladen, self._parallel):
                if self._cancel:
                    break
                ergebnisse = []
                with ThreadPoolExecutor(max_workers=self._parallel) as pool:
                    auftraege = {
                        pool.submit(self._upload_versuch, client, by_id[k]): k
                        for k in stapel
                    }
                    for auftrag in as_completed(auftraege):
                        ergebnisse.append((auftrag.result(), auftraege[auftrag]))

                for (asset_id, duplikat, fehler), key in ergebnisse:
                    row = by_id[key]
                    self._report(f"Lade hoch: {row['filename']} ({done} von {offen})",
                                 done, offen)
                    if self._verbuche_upload(int(key), row, asset_id, duplikat,
                                             fehler, result):
                        misserfolge = 0
                    else:
                        # Fehlgeschlagene Datei für DIESEN Lauf zurückstellen,
                        # sonst holt unsynced() sie sofort wieder und der
                        # Abgleich dreht sich ewig im Kreis.
                        self._zurueckgestellt.add(int(key))
                        misserfolge += 1
                        self._report(
                            f"Fehlgeschlagen ({result.failed}): {row['filename']}"
                            f" — {self._letzter_fehler}", done, offen, force=True)
                        if misserfolge >= ABBRUCH_NACH_FEHLERN:
                            result.errors.append(
                                f"Nach {misserfolge} Fehlversuchen hintereinander "
                                f"abgebrochen — der Server nimmt nichts an.")
                            self._abgebrochen = True
                            self._db.commit()
                            return

                    done += 1
                    self._report(f"Abgeglichen: {done} von {offen}", done, offen)
                self._db.commit()
            self._db.commit()

        if self._abgelehnte_typen:
            typen = ", ".join(sorted(self._abgelehnte_typen))
            result.errors.append(
                f"Dieser Immich-Server nimmt {typen} nicht an — diese Dateien wurden übersprungen.")
        self._report(f"Bilder fertig: {done} von {offen}", done, offen, force=True)

    def _upload_versuch(self, client: ImmichClient, row: dict):
        """NUR die Netzarbeit - laeuft in mehreren Faeden gleichzeitig.

        Hier wird bewusst NICHT auf die Datenbank zugegriffen: eine
        sqlite-Verbindung gehoert genau einem Faden. Das Ergebnis wird
        zurueckgegeben und im Abgleichfaden verbucht.
        """
        sidecar = Path(row["path"]).with_suffix(".xmp")
        mit_sidecar = bool(row.get("is_raw")) and sidecar.exists()
        try:
            asset_id, duplikat = client.upload(
                row["path"], checksum=row.get("checksum"),
                sidecar=str(sidecar) if mit_sidecar else None)
            return asset_id, duplikat, None
        except (ImmichError, OSError) as exc:
            return None, False, exc

    def _verbuche_upload(self, photo_id: int, row: dict, asset_id, duplikat,
                         fehler, result: SyncResult) -> bool:
        """Ergebnis eines Uploads in die Datenbank schreiben."""
        if fehler is not None:
            result.failed += 1
            if len(result.errors) < 20:
                result.errors.append(f"{row['filename']}: {fehler}")
            self._letzter_fehler = str(fehler)
            if ist_dauerhafter_fehler(str(fehler)):
                self._db.set_immich(photo_id, "", row.get("checksum"))
                result.skipped += 1
                result.failed -= 1
                self._abgelehnte_typen.add(Path(row["path"]).suffix.lower())
                return True
            return False

        self._db.set_immich(photo_id, asset_id or "", row.get("checksum"))
        if duplikat:
            result.already_there += 1
        else:
            result.uploaded += 1
        return True

    def _upload_one(self, client: ImmichClient, photo_id: int, row: dict,
                    result: SyncResult) -> bool:
        """Eine Datei hochladen. Liefert True bei Erfolg.

        Bei Misserfolg wird die Datei NICHT als erledigt vermerkt (sie soll
        beim nächsten Lauf wieder drankommen) - deshalb muss der Aufrufer
        sie für DIESEN Lauf zurückstellen, sonst zieht die Arbeitsliste
        immer wieder dieselben Dateien und der Abgleich endet nie.
        """
        # Sidecar nur bei RAW mitschicken. Bei einem JPEG steckt XMP in der
        # Datei selbst; die danebenliegende BILD.xmp gehört zur RAW-Fassung
        # und hätte am JPEG nichts zu suchen.
        sidecar = Path(row["path"]).with_suffix(".xmp")
        send_sidecar = bool(row.get("is_raw")) and sidecar.exists()
        try:
            asset_id, duplicate = client.upload(
                row["path"],
                checksum=row.get("checksum"),
                sidecar=str(sidecar) if send_sidecar else None,
            )
        except (ImmichError, OSError) as exc:
            result.failed += 1
            if len(result.errors) < 20:
                result.errors.append(f"{row['filename']}: {exc}")
            self._letzter_fehler = str(exc)
            if ist_dauerhafter_fehler(str(exc)):
                # Der Server nimmt diesen Dateityp grundsaetzlich nicht.
                # Leere Kennung merken, damit die Datei nicht bei jedem
                # Lauf erneut angeboten wird - und den Abgleich nicht
                # wegen einer Eigenart des Servers abbrechen lassen.
                self._db.set_immich(photo_id, "", row.get("checksum"))
                result.skipped += 1
                result.failed -= 1
                self._abgelehnte_typen.add(Path(row["path"]).suffix.lower())
                return True
            return False

        self._db.set_immich(photo_id, asset_id or "", row.get("checksum"))
        if duplicate:
            result.already_there += 1
        else:
            result.uploaded += 1
        return True

    def _sync_remote(self, client: ImmichClient, result: SyncResult) -> None:
        """Verzeichnis der Server-Bilder spiegeln.

        Es werden NUR Angaben geholt, keine Bilddaten - Vorschauen kommen
        erst, wenn eine Kachel sie wirklich braucht. Das Original holt
        Wimmich ausschliesslich auf ausdruecklichen Wunsch.
        """
        jetzt = time.time()
        seite, gesamt = 1, 0
        while not self._cancel:
            try:
                elemente, weiter = client.list_assets(seite)
            except ImmichError as exc:
                result.errors.append(f"Serverbilder: {exc}")
                return
            if not elemente:
                break
            self._db.upsert_remote(
                [_remote_eintrag(e) for e in elemente if e.get("id")], jetzt)
            gesamt += len(elemente)
            self._report(f"Serverbilder: {gesamt} gelesen", gesamt, 0)
            self._db.commit()
            if not weiter:
                break
            seite += 1

        if not self._cancel:
            entfernt = self._db.prune_remote(jetzt)
            self._report(f"Serverbilder: {gesamt} bekannt"
                         + (f", {entfernt} nicht mehr vorhanden" if entfernt else ""),
                         gesamt, gesamt, force=True)

    # -- Alben und Personen --------------------------------------------

    def _sync_albums(self, client: ImmichClient) -> None:
        albums = client.albums()
        self._db.replace_albums(albums)
        self._report(f"{len(albums)} Alben geholt", 0, len(albums), force=True)

        for index, album in enumerate(albums, start=1):
            if self._cancel:
                return
            try:
                assets = client.album_assets(album.id)
            except ImmichError:
                continue
            self._db.set_album_assets(
                self._db.album_id_fuer_immich(album.id) or album.id,
                [a["id"] for a in assets if a.get("id")]
            )
            self._report(f"Alben: {index} von {len(albums)}", index, len(albums))
        self._report(f"Alben fertig: {len(albums)} von {len(albums)}",
                     len(albums), len(albums), force=True)

    def _push_albums(self, client: ImmichClient, result: SyncResult) -> None:
        """Eigene Albenänderungen auf den Server schieben.

        Läuft NACH den Bildern: erst dann haben frisch hochgeladene
        Aufnahmen eine Immich-Kennung und können überhaupt in ein Album
        gelegt werden. Was noch keine hat, bleibt vorgemerkt und geht
        beim nächsten Abgleich mit - das Album bleibt deshalb „dirty".
        """
        offen = self._db.album_dirty()
        if not offen:
            return
        self._report(f"Alben schieben: 0 von {len(offen)}", 0, len(offen),
                     force=True)

        for index, album in enumerate(offen, start=1):
            if self._cancel:
                return
            album_id = album["id"]
            immich_id = album["immich_id"] or ""
            zugeordnet = self._album_kennungen(album_id, entfernt=False)
            entfernt = self._album_kennungen(album_id, entfernt=True)
            vollstaendig = not self._album_wartet(album_id)

            try:
                if not immich_id:
                    immich_id = client.create_album(album["name"],
                                                    sorted(zugeordnet))
                    if not immich_id:
                        continue
                    self._report(f"Album angelegt: {album['name']}",
                                 index, len(offen), force=True)
                else:
                    if album["dirty"]:
                        client.rename_album(immich_id, album["name"])
                    bekannt = set(self._db.album_asset_ids(album_id))
                    neu = sorted(zugeordnet - bekannt)
                    if neu:
                        client.add_to_album(immich_id, neu)
                    weg = sorted(entfernt & (bekannt | zugeordnet))
                    if weg:
                        client.remove_from_album(immich_id, weg)
            except ImmichError as exc:
                result.errors.append(f"Album {album['name']}: {exc}")
                continue

            # Spiegel nachziehen, damit der Baum sofort stimmt
            self._db.set_album_assets(album_id, sorted(zugeordnet - entfernt))
            if vollstaendig:
                self._db.album_sauber(album_id, immich_id)
            else:
                # Es fehlen noch Uploads - Kennung merken, Rest bleibt offen
                self._db.set_album_immich(album_id, immich_id)
            self._report(f"Alben schieben: {index} von {len(offen)}",
                         index, len(offen))
        self._report(f"Alben geschoben: {len(offen)}", len(offen), len(offen),
                     force=True)

    def _album_kennungen(self, album_id: str, entfernt: bool) -> set[str]:
        """Immich-Kennungen der lokalen Albumeinträge.

        Lokale Dateien steuern ihre Kennung über photos.immich_id bei -
        wer noch nicht hochgeladen ist, hat keine und fällt hier heraus.
        """
        ids: set[str] = set()
        for zeile in self._db.album_eintraege(album_id, entfernt=entfernt):
            if zeile["immich_id"]:
                ids.add(zeile["immich_id"])
            elif zeile["path"]:
                kennung = self._db.immich_id_fuer_pfad(zeile["path"])
                if kennung:
                    ids.add(kennung)
        return ids

    def _album_wartet(self, album_id: str) -> bool:
        """Steht noch ein Bild ohne Immich-Kennung im Album?"""
        for zeile in self._db.album_eintraege(album_id, entfernt=False):
            if not zeile["immich_id"] and zeile["path"]:
                if not self._db.immich_id_fuer_pfad(zeile["path"]):
                    return True
        return False

    def _sync_people(self, client: ImmichClient) -> None:
        people = client.people()
        self._db.replace_people(people)
        self._report(f"{len(people)} Personen geholt", 0, len(people), force=True)

        for index, person in enumerate(people, start=1):
            if self._cancel:
                return
            try:
                assets = client.assets_of_person(person.id)
            except ImmichError:
                continue
            self._db.set_person_assets(
                person.id, [a["id"] for a in assets if a.get("id")]
            )
            self._report(f"Personen: {index} von {len(people)}", index, len(people))
        self._report(f"Personen fertig: {len(people)} von {len(people)}",
                     len(people), len(people), force=True)


def _remote_eintrag(asset: dict) -> dict:
    """Server-Antwort auf die Spalten unserer Spiegel-Tabelle bringen."""
    exif = asset.get("exifInfo") or {}
    return {
        "immich_id": asset.get("id"),
        "filename": asset.get("originalFileName") or "",
        "taken_at": (asset.get("fileCreatedAt") or "")[:19].replace("T", " "),
        "checksum": asset.get("checksum"),
        "kind": asset.get("type") or "IMAGE",
        "width": exif.get("exifImageWidth"),
        "height": exif.get("exifImageHeight"),
    }


def _haeppchen(werte: list, groesse: int):
    """Liste in Stapel schneiden - je Stapel ein Schwung gleichzeitiger Uploads."""
    for anfang in range(0, len(werte), groesse):
        yield werte[anfang:anfang + groesse]
