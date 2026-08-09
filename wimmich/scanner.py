"""Hintergrundarbeit: Ordner einlesen und Metadaten nachziehen.

Läuft in einem eigenen QThread. Der Scanner berührt keine Dateien
schreibend - er liest nur und füllt den Index.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .config import IMAGE_EXTS, RAW_EXTS
from .db import Database
from .exif import ExifTool

_METADATA_BATCH = 60
_COMMIT_EVERY = 500


class ScanWorker(QObject):
    """Durchläuft Bibliotheksordner und füllt den Index."""

    progress = pyqtSignal(str, int)      # Meldung, Anzahl bisher
    folder_done = pyqtSignal(str, int)   # Ordnerpfad, Anzahl Bilder
    finished = pyqtSignal(int, int)      # gefunden, entfernt
    failed = pyqtSignal(str)

    def __init__(self, db_path, exiftool_path: str | None = None,
                 excluded: list[str] | None = None) -> None:
        super().__init__()
        self._db_path = db_path
        self._exiftool_path = exiftool_path
        self._excluded = [str(Path(p)) for p in (excluded or [])]
        self._db: Database | None = None
        self._exif: ExifTool | None = None
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @pyqtSlot(list)
    def scan(self, roots: list[str]) -> None:
        try:
            self._db = Database(self._db_path)
            self._exif = ExifTool(self._exiftool_path or None)
            found = 0
            removed = 0

            for root in roots:
                if self._cancel:
                    break
                root_path = Path(root)
                if not root_path.is_dir():
                    continue
                for folder, count, gone in self._walk(root_path):
                    found += count
                    removed += gone
                    self.folder_done.emit(folder, count)
                    self.progress.emit(f"Eingelesen: {folder}", found)

            self._db.commit()
            if not self._cancel:
                self._read_metadata()
            self._db.commit()
            self.finished.emit(found, removed)
        except Exception as exc:  # Hintergrundthread darf nicht still sterben
            self.failed.emit(str(exc))
        finally:
            if self._exif is not None:
                self._exif.stop()
            if self._db is not None:
                self._db.close()

    # -- Ordnerdurchlauf -----------------------------------------------

    def _walk(self, root: Path):
        pending = 0
        for dirpath, dirnames, filenames in os.walk(root):
            if self._cancel:
                return
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

            folder = str(Path(dirpath))
            if self._is_excluded(folder):
                # Nicht hineinlaufen: der ganze Zweig bleibt draußen
                dirnames[:] = []
                continue
            seen: set[str] = set()
            count = 0

            for name in sorted(filenames):
                ext = Path(name).suffix.lower()
                if ext not in IMAGE_EXTS:
                    continue
                full = str(Path(dirpath) / name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                seen.add(full)
                self._db.upsert_file(
                    path=full,
                    folder=folder,
                    filename=name,
                    ext=ext,
                    is_raw=ext in RAW_EXTS,
                    filesize=stat.st_size,
                    mtime=stat.st_mtime,
                )
                count += 1
                pending += 1
                if pending >= _COMMIT_EVERY:
                    self._db.commit()
                    pending = 0

            gone = self._db.delete_missing(folder, seen) if seen or count == 0 else 0
            if count or gone:
                yield folder, count, gone

        self._db.commit()

    def _is_excluded(self, folder: str) -> bool:
        for blocked in self._excluded:
            if folder == blocked or folder.startswith(blocked.rstrip("/\\") + os.sep):
                return True
        return False

    # -- Metadaten -----------------------------------------------------

    def _read_metadata(self) -> None:
        if not self._exif or not self._exif.available:
            self.progress.emit(
                "exiftool nicht gefunden - Metadaten und Bewertungen bleiben leer", 0
            )
            return

        done = 0
        while not self._cancel:
            rows = self._db.pending_metadata(_METADATA_BATCH)
            if not rows:
                break
            paths = [r["path"] for r in rows]
            try:
                data = self._exif.read_batch(paths)
                sidecars = self._read_sidecars(rows)
            except Exception:
                break

            for row in rows:
                meta = data.get(str(Path(row["path"])))
                extra = sidecars.get(row["id"])
                if meta is not None and extra:
                    # Sidecar gewinnt: dort steht, was Lightroom oder wir
                    # zuletzt geschrieben haben.
                    for key, value in extra.items():
                        if value not in (None, "", 0) or key == "rating":
                            meta[key] = value
                if meta is None:
                    # Nicht lesbar: trotzdem als erledigt markieren, sonst Endlosschleife
                    self._db.store_metadata(row["id"], {})
                else:
                    self._db.store_metadata(row["id"], meta)
                done += 1

            self._db.commit()
            self.progress.emit(f"Metadaten gelesen: {done}", done)

    def _read_sidecars(self, rows) -> dict[int, dict]:
        """Liest vorhandene XMP-Sidecars zu RAW-Dateien.

        exiftool liest beim Aufruf auf die RAW-Datei den danebenliegenden
        Sidecar nicht mit - Bewertungen aus Lightroom oder aus Wimmich
        selbst waeren sonst nach dem nächsten Durchlauf wieder weg.
        """
        wanted: dict[str, int] = {}
        for row in rows:
            if not row["is_raw"]:
                continue
            sidecar = Path(row["path"]).with_suffix(".xmp")
            if sidecar.exists():
                wanted[str(sidecar)] = row["id"]
        if not wanted:
            return {}

        data = self._exif.read_batch(list(wanted))
        result: dict[int, dict] = {}
        for sidecar_path, photo_id in wanted.items():
            meta = data.get(str(Path(sidecar_path)))
            if meta:
                result[photo_id] = {
                    key: meta.get(key)
                    for key in ("rating", "label", "title", "caption", "keywords")
                }
        return result
