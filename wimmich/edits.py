"""Retuschen verwalten - nichtdestruktiv.

Eine Retusche ist eine Liste von Arbeitsschritten, nicht ein verändertes
Bild. Die Schritte liegen als JSON in der Datenbank und werden beim
Anzeigen und beim Ausgeben frisch gerechnet. Die Originaldatei wird
niemals überschrieben.

Koordinaten stehen RELATIV zur Bildbreite und -höhe (0…1). Sonst würde
eine Retusche, die auf der verkleinerten Ansicht gesetzt wurde, beim
Ausgeben in voller Größe an der falschen Stelle landen.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from . import retouch

SPOT = "spot"
STROKE = "stroke"
RED_EYE = "red_eye"
FADED = "faded"
TONE = "tone"
GEOMETRY = "geometry"
CROP = "crop"


@dataclass
class Step:
    """Ein Arbeitsschritt. Maße sind Bruchteile der Bildgröße."""

    kind: str
    x: float = 0.0
    y: float = 0.0
    radius: float = 0.02
    points: list = field(default_factory=list)   # [[x, y], …] für Striche
    strength: float = 1.0
    neutralise: bool = True
    saturation: float = 0.0
    # Grundeinstellungen
    exposure: float = 0.0
    contrast: float = 0.0
    warmth: float = 0.0
    tint: float = 0.0
    shadows: float = 0.0
    highlights: float = 0.0
    clarity: float = 0.0
    detail: float = 0.0
    # Zuschnitt, als Bruchteile der Bildgröße
    width: float = 1.0
    height: float = 1.0
    # Geometrie
    quarters: int = 0        # Vierteldrehungen im Uhrzeigersinn
    angle: float = 0.0       # feine Drehung in Grad
    persp_h: float = 0.0     # Entzerrung waagerecht
    persp_v: float = 0.0     # Entzerrung senkrecht

    def label(self) -> str:
        return {
            SPOT: "Fleck entfernt",
            STROKE: "Riss repariert",
            RED_EYE: "Rote Augen",
            FADED: "Farben aufgefrischt",
            TONE: "Grundeinstellungen",
            GEOMETRY: "Drehen und Entzerren",
            CROP: "Zuschnitt",
        }.get(self.kind, self.kind)


class EditStack:
    """Die Schrittfolge eines Bildes."""

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps: list[Step] = list(steps or [])

    # -- Bearbeiten ----------------------------------------------------

    # Diese Schritte gibt es je Bild nur einmal; ein neuer ersetzt den alten.
    SINGLE = (FADED, TONE, GEOMETRY, CROP)

    def add(self, step: Step) -> None:
        if step.kind in self.SINGLE:
            self.steps = [s for s in self.steps if s.kind != step.kind]
        self.steps.append(step)

    def single(self, kind: str) -> Step | None:
        for step in self.steps:
            if step.kind == kind:
                return step
        return None

    def remove_kind(self, kind: str) -> None:
        self.steps = [s for s in self.steps if s.kind != kind]

    def undo(self) -> Step | None:
        return self.steps.pop() if self.steps else None

    def clear(self) -> None:
        self.steps.clear()

    def __len__(self) -> int:
        return len(self.steps)

    def __bool__(self) -> bool:
        return bool(self.steps)

    # -- Speichern -----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps([asdict(s) for s in self.steps], ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str | None) -> "EditStack":
        if not payload:
            return cls()
        try:
            raw = json.loads(payload)
        except ValueError:
            return cls()
        steps = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict) or "kind" not in entry:
                continue
            allowed = {k: v for k, v in entry.items()
                       if k in Step.__dataclass_fields__}
            try:
                steps.append(Step(**allowed))
            except TypeError:
                continue
        return cls(steps)

    # -- Anwenden ------------------------------------------------------

    def apply(self, rgb: np.ndarray, with_crop: bool = True) -> np.ndarray:
        """Alle Schritte auf ein RGB-Feld (float32, 0…1) rechnen.

        Die Reihenfolge ist fest, nicht die des Setzens:

          1. Auffrischen   - sonst passt eine Retusche zum verblassten Bild
          2. Grundeinstellungen
          3. Flecken, Risse, rote Augen
          4. Zuschnitt     - ZULETZT, damit die Koordinaten der Retuschen
                             sich immer auf das ganze Bild beziehen

        with_crop=False liefert das ungeschnittene Ergebnis - das braucht
        das Bearbeitungsfenster, das den Zuschnitt als Rahmen zeigt.
        """
        if not self.steps:
            return rgb

        result = rgb

        faded = self.single(FADED)
        if faded:
            result = retouch.restore_faded(
                result, faded.strength, faded.neutralise, faded.saturation)

        tone = self.single(TONE)
        if tone:
            result = retouch.apply_tone(
                result, exposure=tone.exposure, contrast=tone.contrast,
                saturation=tone.saturation, warmth=tone.warmth,
                tint=tone.tint, shadows=tone.shadows,
                highlights=tone.highlights, clarity=tone.clarity,
                detail=tone.detail)

        height, width = result.shape[:2]
        # Der Radius bezieht sich auf die kürzere Kante, damit ein Fleck
        # im Hoch- und im Querformat gleich groß ausfällt.
        unit = min(width, height)

        for step in self.steps:
            radius = max(2, int(round(step.radius * unit)))
            if step.kind == SPOT:
                result = retouch.heal_spot(
                    result, int(step.x * width), int(step.y * height), radius)
            elif step.kind == STROKE:
                points = [(int(px * width), int(py * height))
                          for px, py in step.points]
                result = retouch.heal_stroke(result, points, radius)
            elif step.kind == RED_EYE:
                result = retouch.fix_red_eye(
                    result, int(step.x * width), int(step.y * height),
                    radius, step.strength)

        # Geometrie NACH den Retuschen: deren Koordinaten beziehen sich
        # aufs ungedrehte Bild. Vor dem Zuschnitt, weil der Zuschnitt im
        # gedrehten Rahmen gesetzt wird.
        geo = self.single(GEOMETRY)
        if geo is not None:
            result = retouch.apply_geometry(result, geo.quarters, geo.angle,
                                            geo.persp_h, geo.persp_v)

        crop_step = self.single(CROP) if with_crop else None
        if crop_step:
            result = retouch.crop(result, crop_step.x, crop_step.y,
                                  crop_step.width, crop_step.height)
        return result


def downscale(array: np.ndarray, max_edge: int) -> np.ndarray:
    """Verkleinerte Fassung für die Vorschau beim Reglerziehen.

    Die Rechenzeit wächst mit der Fläche: eine Kante von 2560 px kostet
    rund 700 ms je Reglerschritt, 900 px nur noch etwa 45 ms. Beim
    Ziehen wird deshalb auf der kleinen Fassung gerechnet und erst nach
    einer Ruhepause in voller Größe nachgezogen.
    """
    height, width = array.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return array
    step = max(1, int(np.ceil(longest / max_edge)))
    return np.ascontiguousarray(array[::step, ::step])


# -- Umwandlung QImage <-> numpy ---------------------------------------

def qimage_to_array(image) -> np.ndarray:
    """QImage → RGB-Feld float32, 0…1."""
    from PyQt6.QtGui import QImage
    if image.format() != QImage.Format.Format_RGB888:
        image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    pointer = image.constBits()
    pointer.setsize(image.sizeInBytes())
    # bytesPerLine kann größer sein als width*3 (Zeilenausrichtung)
    raw = np.frombuffer(pointer, dtype=np.uint8).reshape(
        height, image.bytesPerLine())
    return raw[:, : width * 3].reshape(height, width, 3).astype(np.float32) / 255.0


def array_to_qimage(array: np.ndarray):
    """RGB-Feld → QImage. Die Kopie löst das Bild vom numpy-Puffer."""
    from PyQt6.QtGui import QImage
    data = retouch.to_uint8(array)
    data = np.ascontiguousarray(data)
    height, width = data.shape[:2]
    return QImage(data.tobytes(), width, height, width * 3,
                  QImage.Format.Format_RGB888).copy()


def geometry_is_neutral(step: Step) -> bool:
    """True, wenn ein Geometrie-Schritt nichts bewirkt."""
    return not any((step.quarters % 4, step.angle, step.persp_h, step.persp_v))


def tone_is_neutral(step: Step) -> bool:
    """True, wenn ein Grundeinstellungs-Schritt nichts bewirkt."""
    return not any((step.exposure, step.contrast, step.saturation,
                    step.warmth, step.tint, step.shadows, step.highlights,
                    step.clarity, step.detail))
