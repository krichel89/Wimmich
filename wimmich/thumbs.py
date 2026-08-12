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


def cache_key(path: str, mtime: float, size: int, edge: int,
              steps: str | None = None) -> str:
    """Schluessel im Vorschau-Cache.

    Die Schrittfolge geht MIT ein: sonst zeigte die Kachel nach einer
    Aenderung weiter das alte Bild, und ein Zuruecknehmen fiele gar
    nicht auf. Ohne Schritte bleibt der Schluessel derselbe wie frueher -
    ein voller Cache wird also nicht entwertet.
    """
    roh = f"{path}|{mtime:.0f}|{size}|{edge}"
    if steps:
        roh += "|" + hashlib.sha1(steps.encode("utf-8", "replace")).hexdigest()[:16]
    return hashlib.sha1(roh.encode("utf-8", errors="replace")).hexdigest()


def cache_path(key: str) -> Path:
    return THUMB_DIR / key[:2] / f"{key}.jpg"


def get_thumbnail(path: str, mtime: float, filesize: int, edge: int = 256,
                  exiftool=None, steps: str | None = None) -> Path | None:
    """Liefert den Pfad zur Vorschau, erzeugt sie bei Bedarf.

    `steps` ist die Schrittfolge als JSON. Ist sie gesetzt, zeigt die
    Kachel das BEARBEITETE Bild. Die Schritte rechnen in Bruchteilen der
    Bildgroesse, laufen also auf der kleinen Vorschau genauso wie auf
    dem Original - nur eben in Millisekunden statt Sekunden.
    """
    key = cache_key(path, mtime, filesize, edge, steps)
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
        if steps:
            image = _bearbeitet(image, steps)
        tmp = dest.with_suffix(".tmp.jpg")
        image.save(tmp, "JPEG", quality=85, optimize=True)
        tmp.replace(dest)
    except Exception:
        return None
    finally:
        image.close()
    return dest


def _bearbeitet(image: Image.Image, steps: str) -> Image.Image:
    """Die Schrittfolge auf die kleine Vorschau rechnen.

    Scheitert das - kaputte Schrittfolge, fehlendes numpy - bleibt das
    unbearbeitete Bild stehen. Eine graue Kachel waere schlimmer als
    eine, die den alten Stand zeigt.
    """
    try:
        import numpy as np

        from .edits import EditStack
        stapel = EditStack.from_json(steps)
        if not stapel.steps:
            return image
        feld = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        ergebnis = stapel.apply(feld)
        ergebnis = np.clip(ergebnis, 0.0, 1.0) * 255.0
        return Image.fromarray(ergebnis.astype("uint8"), "RGB")
    except Exception:
        from . import crashlog
        crashlog.protokolliere("Bearbeitete Vorschau")
        return image


def _load_for_thumb(path: str, edge: int, exiftool) -> Image.Image | None:
    if not is_raw(path):
        bild = Image.open(path)
        # draft() lässt den JPEG-Dekoder gleich verkleinert arbeiten,
        # statt erst das ganze Bild aufzubauen und dann zu schrumpfen.
        # Bei einer 45-MP-Datei ist das der Unterschied zwischen einem
        # Wimpernschlag und einer Sekunde.
        try:
            bild.draft("RGB", (edge * 2, edge * 2))
        except Exception:
            pass
        return bild

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

    # Ohne rawpy: die eingebettete Vorschau selbst aus der Datei holen.
    #
    # Der frühere Weg rief exiftool auf - EIN PROZESS JE DATEI. Das kostet
    # unter Linux 85 ms, unter Windows mit der mitgelieferten
    # Perl-Umgebung ein Vielfaches. Bei einem Ordner mit 300 RAW-Dateien
    # sind das Minuten, in denen nichts erscheint.
    daten = embedded_jpeg(path)
    if daten:
        try:
            import io
            image = Image.open(io.BytesIO(daten))
            image.load()
            return image
        except Exception:
            pass
    return None


def embedded_jpeg(path: str) -> bytes | None:
    """Größtes eingebettetes JPEG einer RAW-Datei, ohne fremde Hilfe.

    RAW-Dateien tragen ihre Vorschauen als vollständige JPEG-Blöcke in
    sich. Die lassen sich an ihren Markierungen erkennen: FF D8 beginnt,
    FF D9 beendet einen Block. Von allen gefundenen wird der größte
    genommen - das ist die Vorschau in voller Größe.

    Gelesen wird höchstens der Anfang der Datei; die Vorschauen liegen
    dort. Ein 45-MB-NEF komplett einzulesen wäre teurer als der Gewinn.
    """
    # Stufenweise lesen: die große Vorschau liegt bei den meisten
    # Herstellern im vorderen Teil der Datei. Ein 45-MB-NEF komplett
    # einzulesen, nur um eine Kachel zu zeichnen, wäre verschwendet.
    roh = b""
    for grenze in (4 * 1024 * 1024, 24 * 1024 * 1024, 64 * 1024 * 1024):
        try:
            with open(path, "rb") as datei:
                roh = datei.read(grenze)
        except OSError:
            return None
        if _finde_groesstes_jpeg(roh, mindestens = 120 * 1024) is not None:
            break
        if len(roh) < grenze:
            break        # Datei ist kürzer als die Schranke
    # Zuletzt auch eine kleinere Vorschau nehmen - eine grobe Kachel ist
    # besser als eine graue Fläche.
    return _finde_groesstes_jpeg(roh, mindestens=2000)


def _finde_groesstes_jpeg(roh: bytes, mindestens: int) -> bytes | None:
    bester = None
    start = roh.find(b"\xff\xd8\xff")
    while start != -1:
        ende = roh.find(b"\xff\xd9", start + 2)
        if ende == -1:
            break
        block = roh[start:ende + 2]
        if len(block) >= mindestens and (bester is None or len(block) > len(bester)):
            bester = block
        start = roh.find(b"\xff\xd8\xff", ende + 2)
    return bester


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
