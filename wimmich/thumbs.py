"""Vorschaubilder erzeugen und im Cache ablegen.

Für RAW wird zuerst die eingebettete Vorschau genommen (schnell, reicht
fuers Raster). Erst wenn die fehlt oder zu klein ist, wird die RAW-Datei
tatsächlich entwickelt.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps

from .config import THUMB_DIR, is_raw

try:  # rawpy ist optional - ohne rawpy geht nur die eingebettete Vorschau
    import rawpy
    HAVE_RAWPY = True
except ImportError:  # pragma: no cover
    rawpy = None
    HAVE_RAWPY = False


def cache_key(path: str, mtime: float, size: int, edge: int) -> str:
    raw = f"{path}|{mtime:.0f}|{size}|{edge}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()


def cache_path(key: str) -> Path:
    return THUMB_DIR / key[:2] / f"{key}.jpg"


def get_thumbnail(path: str, mtime: float, filesize: int, edge: int = 256,
                  exiftool=None) -> Path | None:
    """Liefert den Pfad zur Vorschau, erzeugt sie bei Bedarf."""
    key = cache_key(path, mtime, filesize, edge)
    dest = cache_path(key)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        image = _load_for_thumb(path, edge, exiftool)
    except Exception:
        return None
    if image is None:
        return None

    try:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        tmp = dest.with_suffix(".tmp.jpg")
        image.save(tmp, "JPEG", quality=85, optimize=True)
        tmp.replace(dest)
    except Exception:
        return None
    finally:
        image.close()
    return dest


def _load_for_thumb(path: str, edge: int, exiftool) -> Image.Image | None:
    if not is_raw(path):
        return Image.open(path)

    embedded = _embedded_preview(path, edge, exiftool)
    if embedded is not None:
        return embedded
    return load_raw_full(path, half_size=True)


def _embedded_preview(path: str, edge: int, exiftool) -> Image.Image | None:
    """Eingebettete JPEG-Vorschau, bevorzugt über rawpy."""
    if HAVE_RAWPY:
        try:
            with rawpy.imread(path) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    import io
                    image = Image.open(io.BytesIO(thumb.data))
                    image.load()
                    if max(image.size) >= edge:
                        return image
                    image.close()
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    return Image.fromarray(thumb.data)
        except Exception:
            pass

    if exiftool is not None and getattr(exiftool, "available", False):
        tmp = THUMB_DIR / "_extract" / (Path(path).stem + ".jpg")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            if exiftool.extract_preview(path, str(tmp)):
                image = Image.open(tmp)
                image.load()
                tmp.unlink(missing_ok=True)
                return image
        except Exception:
            pass
    return None


def load_raw_full(path: str, half_size: bool = False) -> Image.Image | None:
    """Entwickelt eine RAW-Datei mit Standardparametern."""
    if not HAVE_RAWPY:
        return None
    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=half_size,
                no_auto_bright=False,
                output_bps=8,
            )
        return Image.fromarray(rgb)
    except Exception:
        return None


def load_full_image(path: str) -> Image.Image | None:
    """Vollbild-Darstellung: RAW wird entwickelt, alles andere direkt geöffnet."""
    try:
        if is_raw(path):
            return load_raw_full(path, half_size=False)
        image = Image.open(path)
        image.load()
        return ImageOps.exif_transpose(image)
    except Exception:
        return None


def clear_cache() -> int:
    """Loescht den Vorschau-Cache. Rückgabe: Anzahl geloeschter Dateien."""
    removed = 0
    if not THUMB_DIR.exists():
        return 0
    for item in THUMB_DIR.rglob("*.jpg"):
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return removed
