"""Konfiguration, Pfade und unterstützte Dateitypen."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_DIRNAME = "Wimmich"

# Endungen, die als Bild gelten. RAW getrennt, weil sie anders geöffnet werden.
RAW_EXTS = {
    ".nef", ".nrw",          # Nikon
    ".cr2", ".cr3", ".crw",  # Canon
    ".arw", ".srf", ".sr2",  # Sony
    ".raf",                  # Fujifilm
    ".orf",                  # Olympus / OM
    ".rw2",                  # Panasonic
    ".pef",                  # Pentax
    ".dng",                  # Adobe / diverse
    ".iiq", ".3fr", ".fff",  # Phase One / Hasselblad
}
BITMAP_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif"}
IMAGE_EXTS = RAW_EXTS | BITMAP_EXTS


def _base_dir(kind: str) -> Path:
    """kind ist 'config' oder 'cache'. Ergibt einen plattformüblichen Pfad."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()
        return Path(root) / APP_DIRNAME / kind
    if sys.platform == "darwin":
        if kind == "cache":
            return Path.home() / "Library" / "Caches" / APP_DIRNAME
        return Path.home() / "Library" / "Application Support" / APP_DIRNAME
    xdg = os.environ.get("XDG_CACHE_HOME" if kind == "cache" else "XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / (".cache" if kind == "cache" else ".config")
    return root / APP_DIRNAME.lower()


CONFIG_DIR = _base_dir("config")
CACHE_DIR = _base_dir("cache")
THUMB_DIR = CACHE_DIR / "thumbs"
DB_PATH = CONFIG_DIR / "index.sqlite3"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "libraries": [],          # Liste absoluter Ordnerpfade
    "excluded": [],           # Ordner, die Wimmich übergeht (samt Unterordnern)
    "thumb_size": 256,        # Kantenlänge der Cache-Vorschau in Pixeln
    "grid_size": 180,         # Kachelgröße im Raster
    "exiftool": "",           # leer = im PATH suchen
    "write_xmp": True,        # Bewertungen zusätzlich in Datei/Sidecar schreiben
    "stack_raw_jpeg": True,   # RAW und JPEG derselben Aufnahme als eine Kachel
    "prefer_raw": True,       # welche Datei den Stapel vertritt
    "label_set": "de",        # Sprache der Lightroom-Farbmarkierungen
    "sort_desc": False,       # Sortierrichtung merken (↓ absteigend)
    "show_subfolders": True,  # Ordneransicht zeigt Unterordner mit an
    "theme": "dunkel",        # "dunkel" oder "hell"
    # Immich. Der Schlüssel liegt im Klartext in dieser Datei - sie steht
    # im Benutzerprofil und ist nur für den angemeldeten Benutzer lesbar,
    # aber sie ist keine Schlüsselverwaltung. Wer mehr will, vergibt in
    # Immich einen eigenen Schlüssel mit wenigen Rechten für Wimmich.
    "immich_url": "",
    "immich_key": "",
    "immich_upload": True,    # fehlende Bilder hochladen
    "immich_people": True,    # Personen vom Server holen
    "immich_auto": True,      # laufend abgleichen, solange die Verbindung steht
    "immich_interval_min": 15,
    "watch_folders": True,    # auf Änderungen in den Bibliotheksordnern reagieren
}


class Config:
    def __init__(self) -> None:
        self._data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if CONFIG_PATH.exists():
            try:
                stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in DEFAULTS:
                        self._data[key] = value

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)

    def __getitem__(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    @property
    def libraries(self) -> list[str]:
        return list(self._data.get("libraries", []))

    def add_library(self, path: str) -> bool:
        path = str(Path(path).resolve())
        libs = self.libraries
        if path in libs:
            return False
        libs.append(path)
        self["libraries"] = libs
        return True

    def remove_library(self, path: str) -> None:
        self["libraries"] = [p for p in self.libraries if p != path]

    @property
    def excluded(self) -> list[str]:
        return list(self._data.get("excluded", []))

    def exclude(self, path: str) -> bool:
        path = str(Path(path).resolve())
        current = self.excluded
        if path in current:
            return False
        current.append(path)
        self["excluded"] = current
        return True

    def include_again(self, path: str) -> None:
        self["excluded"] = [p for p in self.excluded if p != path]

    def is_excluded(self, path: str) -> bool:
        """Auch Unterordner eines ausgeschlossenen Ordners sind draußen."""
        path = str(Path(path))
        for blocked in self.excluded:
            if path == blocked or path.startswith(blocked.rstrip("/\\") + os.sep):
                return True
        return False


def bundled_exiftool() -> Path | None:
    """Beigelegtes exiftool in einer gebauten Fassung.

    PyInstaller entpackt mitgegebene Dateien nach sys._MEIPASS - bei
    --onedir ist das der Unterordner _internal, bei --onefile ein
    Temporärverzeichnis. In beiden Fällen liegt exiftool dort unter
    exiftool/. Im normalen Python-Lauf gibt es _MEIPASS nicht, dann
    liefert die Funktion None und es wird im PATH gesucht.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    name = "exiftool.exe" if sys.platform == "win32" else "exiftool"
    candidate = Path(base) / "exiftool" / name
    return candidate if candidate.exists() else None


def find_exiftool(configured: str = "") -> str | None:
    """Pfad zu exiftool, in dieser Reihenfolge.

    1. was in den Einstellungen steht
    2. das beigelegte exiftool der gebauten Fassung
    3. der PATH des Systems

    Die beigelegte Fassung hat Vorrang vor dem PATH, damit eine
    Standalone-Fassung sich immer gleich verhält - unabhängig davon, ob
    auf dem Rechner zufällig ein anderes (womöglich älteres) exiftool
    installiert ist.
    """
    if configured:
        return configured if Path(configured).exists() else None
    bundled = bundled_exiftool()
    if bundled is not None:
        return str(bundled)
    return shutil.which("exiftool") or shutil.which("exiftool.exe")


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTS


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS
