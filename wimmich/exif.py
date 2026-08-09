"""Metadaten lesen und schreiben über exiftool.

Bewusst kein Python-Metadatenmodul: exiftool ist bei Harald ohnehin
vorhanden, kennt jedes RAW-Format und schreibt XMP so, wie Lightroom es
erwartet. Gelesen wird im Stapel über den -stay_open-Modus.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from .config import find_exiftool, is_raw
from .marks import clamp_rating

# Tags, die wir in den Index übernehmen.
_READ_ARGS = [
    "-j",                    # JSON-Ausgabe
    "-n",                    # Rohwerte statt hübscher Umschreibungen
    "-charset", "filename=utf8",
    "-ImageWidth", "-ImageHeight",
    "-DateTimeOriginal", "-CreateDate",
    "-Model", "-LensModel", "-LensID",
    "-Rating", "-XMP:Rating",
    "-Label", "-XMP:Label",
    "-ColorTemperature", "-WB_RGGBLevelsAsShot",
    "-Title", "-XMP:Title",
    "-Description", "-ImageDescription", "-Caption-Abstract",
    "-Keywords", "-XMP:Subject",
]

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ExifToolError(RuntimeError):
    pass


class ExifTool:
    """Dauerläufer-Prozess nach dem dokumentierten -stay_open-Protokoll."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or find_exiftool()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def start(self) -> None:
        if self._proc is not None or not self.executable:
            return
        self._proc = subprocess.Popen(
            [self.executable, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b"-stay_open\nFalse\n")
                proc.stdin.flush()
            proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()

    def execute(self, args: list[str]) -> str:
        """Führt einen exiftool-Aufruf aus und liefert die Ausgabe."""
        if not self.executable:
            raise ExifToolError("exiftool nicht gefunden")
        with self._lock:
            if self._proc is None:
                self.start()
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise ExifToolError("exiftool-Prozess nicht verfügbar")

            payload = "\n".join(args) + "\n-execute\n"
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()

            chunks: list[bytes] = []
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise ExifToolError("exiftool hat den Prozess beendet")
                if line.strip() == b"{ready}":
                    break
                chunks.append(line)
        return b"".join(chunks).decode("utf-8", errors="replace")

    # -- Lesen ---------------------------------------------------------

    def read_batch(self, paths: list[str]) -> dict[str, dict]:
        """Liest die Tags mehrerer Dateien auf einmal."""
        if not paths:
            return {}
        raw = self.execute(_READ_ARGS + list(paths))
        try:
            entries = json.loads(raw) if raw.strip() else []
        except ValueError:
            return {}

        result: dict[str, dict] = {}
        for entry in entries:
            source = entry.get("SourceFile")
            if source:
                result[str(Path(source))] = _normalise(entry)
        return result

    # -- Schreiben -----------------------------------------------------

    def write_marks(self, path: str, rating: int | None = None,
                    label: str | None = None) -> str:
        """Bewertung und/oder Farbmarkierung schreiben.

        RAW-Dateien werden nie angefasst; dafür entsteht ein Sidecar mit
        dem gleichen Basisnamen (Lightroom-Konvention: BILD.xmp).
        Rückgabe: der tatsächlich beschriebene Pfad.

        Eine Ablehnung (-1) wandert nur nach XMP. EXIF:Rating ist auf
        0 bis 5 festgelegt und würde eine -1 nicht überstehen.
        """
        tags: list[str] = []
        if rating is not None:
            rating = clamp_rating(rating)
            tags.append(f"-XMP:Rating={rating}")
        if label is not None:
            tags.append(f"-XMP:Label={label}")
        if not tags:
            return path

        target = Path(path)
        if is_raw(target):
            sidecar = target.with_suffix(".xmp")
            if not sidecar.exists():
                # Sidecar aus der RAW-Datei erzeugen. Achtung: eine
                # Tag-Zuweisung im selben -o-Aufruf wird von exiftool
                # ignoriert (geprüft mit 12.76), deshalb zwei Schritte.
                self.execute(["-o", str(sidecar), str(target)])
            self.execute(["-overwrite_original", *tags, str(sidecar)])
            return str(sidecar)

        if rating is not None and rating >= 0:
            tags.append(f"-EXIF:Rating={rating}")
        self.execute(["-overwrite_original", *tags, str(target)])
        return str(target)

    def write_rating(self, path: str, rating: int) -> str:
        """Nur die Bewertung - für vorhandene Aufrufe erhalten."""
        return self.write_marks(path, rating=rating)

    def extract_preview(self, path: str, dest: str) -> bool:
        """Zieht die größte eingebettete Vorschau aus einer RAW-Datei.

        Läuft bewusst als eigener Prozess: der -stay_open-Kanal liefert
        Text, hier brauchen wir rohe Bytes auf stdout.
        """
        if not self.executable:
            return False
        for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
            try:
                result = subprocess.run(
                    [self.executable, "-b", tag, str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if result.returncode == 0 and len(result.stdout) > 1024:
                try:
                    Path(dest).write_bytes(result.stdout)
                except OSError:
                    return False
                return True
        return False


def _first(entry: dict, *keys):
    for key in keys:
        value = entry.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_number(value) -> int | None:
    """Farbtemperatur als ganze Zahl, sonst None.

    Nicht jede Datei hat sie: viele JPEGs geben nichts her, bei RAW
    steckt sie meist in den Herstellerdaten. Fehlt sie, rechnet Wimmich
    mit Tageslicht und sagt das auch dazu.
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if 1000 <= number <= 30000 else None


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def _normalise(entry: dict) -> dict:
    taken = _first(entry, "DateTimeOriginal", "CreateDate")
    if isinstance(taken, str) and len(taken) >= 19:
        # exiftool liefert 2026:08:08 12:00:00 - für Sortierung umbauen
        taken = taken[:10].replace(":", "-") + " " + taken[11:19]
    rating = _first(entry, "Rating", "XMP:Rating")
    try:
        rating = int(float(rating)) if rating is not None else 0
    except (TypeError, ValueError):
        rating = 0

    return {
        "width": _first(entry, "ImageWidth"),
        "height": _first(entry, "ImageHeight"),
        "taken_at": taken,
        "camera": _as_text(_first(entry, "Model")),
        "lens": _as_text(_first(entry, "LensModel", "LensID")),
        "rating": clamp_rating(rating),
        "label": _as_text(_first(entry, "Label", "XMP:Label")),
        "color_temp": _as_number(_first(entry, "ColorTemperature")),
        "title": _as_text(_first(entry, "Title", "XMP:Title")),
        "caption": _as_text(
            _first(entry, "Description", "Caption-Abstract", "ImageDescription")
        ),
        "keywords": _as_text(_first(entry, "Keywords", "Subject")),
    }
