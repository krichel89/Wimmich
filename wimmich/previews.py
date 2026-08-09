"""Vorschau-Pipeline für die Großansicht.

Übernommen aus Cammellos previews.py und auf PyQt6 gebracht. Der Kern
ist der Grund, warum RAW dort flott ist: für die Anzeige wird NICHT die
RAW-Datei entwickelt, sondern die eingebettete JPEG-Vorschau benutzt.
Die ist bei heutigen Kameras in voller Auflösung vorhanden. Entwickelt
wird nur, wenn keine eingebettete Vorschau existiert.

Zwei Ebenen im Arbeitsspeicher:
  screen  auf Bildschirmgröße verkleinert - was man beim Blättern sieht
  full    unverkleinert - nur nötig, sobald über die Einpassung hinaus
          gezoomt wird

Dazu ein Orientierungs-Cache: die Ausrichtung einmal je Datei lesen
statt einmal je Ebene spart bei RAW ein komplettes rawpy.imread.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage

from .config import is_raw

try:
    import rawpy
    HAVE_RAWPY = True
except ImportError:  # pragma: no cover
    rawpy = None
    HAVE_RAWPY = False

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

SCREEN_EDGE = 2560          # längste Kante der Bildschirm-Ebene


# -- Orientierung ------------------------------------------------------

_orientation: dict[str, int] = {}
_orientation_lock = threading.Lock()


def read_orientation(path: str) -> int:
    """EXIF-Ausrichtung (1-8), je Datei nur einmal gelesen."""
    with _orientation_lock:
        cached = _orientation.get(path)
    if cached is not None:
        return cached

    value = 1
    try:
        if is_raw(path) and HAVE_RAWPY:
            with rawpy.imread(path) as raw:
                # rawpy zählt Vierteldrehungen, EXIF zählt anders
                value = {0: 1, 3: 3, 5: 8, 6: 6}.get(
                    getattr(raw.sizes, "flip", 0), 1
                )
        elif HAVE_PIL:
            with Image.open(path) as img:
                exif = img.getexif()
                value = int(exif.get(274, 1) or 1)
    except Exception:
        value = 1

    with _orientation_lock:
        _orientation[path] = value
    return value


def clear_orientation_cache() -> None:
    with _orientation_lock:
        _orientation.clear()


# -- Dekodieren --------------------------------------------------------

def _embedded_bytes(path: str) -> bytes | None:
    """Eingebettete JPEG-Vorschau einer RAW-Datei.

    Das ist der eigentliche Tempogewinn: ein rawpy.postprocess() kostet
    bei einer 45-MP-Datei Sekunden, das Herausziehen der eingebetteten
    Vorschau Millisekunden.
    """
    if not HAVE_RAWPY:
        return None
    try:
        with rawpy.imread(path) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return bytes(thumb.data)
            if thumb.format == rawpy.ThumbFormat.BITMAP and HAVE_PIL:
                buffer = io.BytesIO()
                Image.fromarray(thumb.data).save(buffer, "JPEG", quality=92)
                return buffer.getvalue()
    except Exception:
        return None
    return None


def _apply_orientation(image: QImage, orientation: int) -> QImage:
    """Dreht ein QImage gemäß EXIF-Ausrichtung."""
    from PyQt6.QtGui import QTransform
    transforms = {
        2: QTransform().scale(-1, 1),
        3: QTransform().rotate(180),
        4: QTransform().scale(1, -1),
        5: QTransform().rotate(90).scale(-1, 1),
        6: QTransform().rotate(90),
        7: QTransform().rotate(270).scale(-1, 1),
        8: QTransform().rotate(270),
    }
    transform = transforms.get(orientation)
    return image.transformed(transform) if transform else image


def decode(path: str, max_edge: int | None = None) -> QImage | None:
    """Bild als QImage laden, bei Bedarf verkleinert.

    RAW geht über die eingebettete Vorschau; nur wenn die fehlt, wird
    die Datei tatsächlich entwickelt.
    """
    image = QImage()

    if is_raw(path):
        data = _embedded_bytes(path)
        if data:
            image.loadFromData(data)
        if image.isNull():
            image = _develop_raw(path)
            if image is None or image.isNull():
                return None
            # Entwickelte Bilder stehen schon aufrecht
            return _scaled(image, max_edge)
    else:
        image.load(path)

    if image.isNull():
        return None

    image = _apply_orientation(image, read_orientation(path))
    return _scaled(image, max_edge)


def _scaled(image: QImage, max_edge: int | None) -> QImage:
    if not max_edge or max(image.width(), image.height()) <= max_edge:
        return image
    from PyQt6.QtCore import Qt
    return image.scaled(
        max_edge, max_edge,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _develop_raw(path: str) -> QImage | None:
    """Notnagel: RAW wirklich entwickeln. Langsam, daher zuletzt."""
    if not HAVE_RAWPY or not HAVE_PIL:
        return None
    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False,
                                  output_bps=8)
        height, width, _ = rgb.shape
        payload = rgb.tobytes()
        return QImage(payload, width, height, width * 3,
                      QImage.Format.Format_RGB888).copy()
    except Exception:
        return None


# -- Zwischenspeicher --------------------------------------------------

class PreviewCache:
    """Nach Bytes begrenzter LRU je Ebene. Threadsicher."""

    def __init__(self, screen_budget: int = 768 * 1024 * 1024,
                 full_budget: int = 512 * 1024 * 1024) -> None:
        self._levels = {
            "screen": [OrderedDict(), 0, screen_budget],
            "full": [OrderedDict(), 0, full_budget],
        }
        self._lock = threading.Lock()

    def get(self, level: str, key: str) -> QImage | None:
        with self._lock:
            store = self._levels[level][0]
            image = store.get(key)
            if image is not None:
                store.move_to_end(key)
            return image

    def put(self, level: str, key: str, image: QImage) -> None:
        if image is None or image.isNull():
            return
        size = image.sizeInBytes()
        with self._lock:
            entry = self._levels[level]
            store, used, budget = entry[0], entry[1], entry[2]
            if key in store:
                used -= store[key].sizeInBytes()
                del store[key]
            store[key] = image
            used += size
            while used > budget and len(store) > 1:
                _old_key, old = store.popitem(last=False)
                used -= old.sizeInBytes()
            entry[1] = used

    def clear(self) -> None:
        with self._lock:
            for entry in self._levels.values():
                entry[0].clear()
                entry[1] = 0


# -- Nachladen im Hintergrund ------------------------------------------

class _LoadSignals(QObject):
    ready = pyqtSignal(str, str, QImage)   # Pfad, Ebene, Bild
    failed = pyqtSignal(str, str)


class _LoadTask(QRunnable):
    def __init__(self, path: str, level: str, cache: PreviewCache,
                 signals: _LoadSignals, announce: bool) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._args = (path, level, cache, signals, announce)

    def run(self) -> None:
        path, level, cache, signals, announce = self._args
        if cache.get(level, path) is not None:
            if announce:
                signals.ready.emit(path, level, cache.get(level, path))
            return
        image = decode(path, SCREEN_EDGE if level == "screen" else None)
        if image is None or image.isNull():
            if announce:
                signals.failed.emit(path, level)
            return
        cache.put(level, path, image)
        if announce:
            signals.ready.emit(path, level, image)


class PreviewLoader(QObject):
    """Lädt Vorschauen nebenläufig und lädt Nachbarn im Voraus."""

    ready = pyqtSignal(str, str, QImage)
    failed = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.cache = PreviewCache()
        self._pool = QThreadPool.globalInstance()
        self._signals = _LoadSignals()
        self._signals.ready.connect(self.ready)
        self._signals.failed.connect(self.failed)

    def request(self, path: str, level: str = "screen") -> QImage | None:
        """Sofort liefern, wenn im Cache; sonst nachladen und melden."""
        cached = self.cache.get(level, path)
        if cached is not None:
            return cached
        self._pool.start(_LoadTask(path, level, self.cache, self._signals, True))
        return None

    def prefetch(self, paths: list[str], level: str = "screen") -> None:
        """Nachbarbilder still vorbereiten - kein Signal, nur Cache füllen."""
        for path in paths:
            if path and self.cache.get(level, path) is None:
                self._pool.start(
                    _LoadTask(path, level, self.cache, self._signals, False)
                )

    def clear(self) -> None:
        self.cache.clear()
        clear_orientation_cache()
