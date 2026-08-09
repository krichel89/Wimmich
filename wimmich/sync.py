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

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .config import DB_PATH
from .db import Database
from .immich import ImmichClient, ImmichError, SyncResult, file_checksum

CHECK_BATCH = 100        # so viele Prüfsummen je Anfrage


class SyncWorker(QObject):
    """Läuft in einem eigenen QThread."""

    progress = pyqtSignal(str, int, int)     # Meldung, erledigt, gesamt
    finished = pyqtSignal(object)            # SyncResult
    failed = pyqtSignal(str)

    def __init__(self, base_url: str, api_key: str, upload: bool = True,
                 fetch_albums: bool = True, fetch_people: bool = True) -> None:
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key
        self._upload = upload
        self._fetch_albums = fetch_albums
        self._fetch_people = fetch_people
        self._cancel = False
        self._db: Database | None = None

    def cancel(self) -> None:
        self._cancel = True

    @pyqtSlot()
    def run(self) -> None:
        result = SyncResult()
        try:
            client = ImmichClient(self._base_url, self._api_key)
            info = client.connect()
            self.progress.emit(
                f"Verbunden mit Immich {info.version or '?'}"
                + (f" als {info.user}" if info.user else ""), 0, 0)

            self._db = Database(DB_PATH)
            self._match_and_upload(client, result)
            if not self._cancel and self._fetch_albums:
                self._sync_albums(client)
            if not self._cancel and self._fetch_people:
                self._sync_people(client)

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
            self.progress.emit("Alle Bilder sind bereits zugeordnet", 0, 0)
            return

        done = 0
        while not self._cancel:
            rows = self._db.unsynced(CHECK_BATCH)
            if not rows:
                break

            # Prüfsummen bilden (und merken - sie ändern sich nur mit der Datei)
            items: list[tuple[str, str]] = []
            by_id: dict[str, dict] = {}
            for row in rows:
                checksum = row["checksum"]
                if not checksum:
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

            known = client.check_uploaded(items)
            result.checked += len(items)

            for key, asset_id in known.items():
                if self._cancel:
                    break
                row = by_id[key]
                if asset_id:
                    self._db.set_immich(int(key), asset_id, row["checksum"])
                    result.already_there += 1
                elif self._upload:
                    self._upload_one(client, int(key), row, result)
                else:
                    # Nicht hochladen: leere Kennung merken, damit die Datei
                    # nicht in jedem Durchlauf erneut geprüft wird.
                    self._db.set_immich(int(key), "", row["checksum"])

                done += 1
                if done % 10 == 0:
                    self.progress.emit(
                        f"Abgeglichen: {done} von {offen}", done, offen)
            self._db.commit()

        self.progress.emit(f"Bilder fertig: {done} von {offen}", done, offen)

    def _upload_one(self, client: ImmichClient, photo_id: int, row: dict,
                    result: SyncResult) -> None:
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
            return

        self._db.set_immich(photo_id, asset_id or "", row.get("checksum"))
        if duplicate:
            result.already_there += 1
        else:
            result.uploaded += 1

    # -- Alben und Personen --------------------------------------------

    def _sync_albums(self, client: ImmichClient) -> None:
        albums = client.albums()
        self._db.replace_albums(albums)
        self.progress.emit(f"{len(albums)} Alben geholt", 0, 0)

        for index, album in enumerate(albums, start=1):
            if self._cancel:
                return
            try:
                assets = client.album_assets(album.id)
            except ImmichError:
                continue
            self._db.set_album_assets(
                album.id, [a["id"] for a in assets if a.get("id")]
            )
            if index % 5 == 0:
                self.progress.emit(
                    f"Alben: {index} von {len(albums)}", index, len(albums))

    def _sync_people(self, client: ImmichClient) -> None:
        people = client.people()
        self._db.replace_people(people)
        self.progress.emit(f"{len(people)} Personen geholt", 0, 0)

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
            if index % 5 == 0:
                self.progress.emit(
                    f"Personen: {index} von {len(people)}", index, len(people))
