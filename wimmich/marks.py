"""Farbmarkierungen und Ablehnen.

Konventionen aus Cammello übernommen, weil sie zugleich die von
Lightroom und Bridge sind:

  * Ablehnen ist Bewertung -1, kein eigenes Feld.
  * Die Farbmarkierung steht als TEXT in xmp:Label. Der Text hängt an
    der Sprache des Lightroom-Farbmarkierungssatzes: derselbe rote
    Punkt heißt einmal "Red" und einmal "Rot".

Deshalb: beim LESEN gegen alle bekannten Sätze prüfen, beim SCHREIBEN
den eingestellten Satz benutzen. Ein unbekannter Text wird nicht
angetastet - sonst zerstört Wimmich eine Markierung, die jemand mit
einem eigenen Satz vergeben hat.
"""

from __future__ import annotations

REJECT = -1

LABEL_SETS = {
    "de": ["Rot", "Gelb", "Grün", "Blau", "Lila"],
    "en": ["Red", "Yellow", "Green", "Blue", "Purple"],
}

# Index-gleich zu den Sätzen oben
LABEL_COLORS = ["#dd3333", "#ddcc33", "#33aa33", "#3366cc", "#9933cc"]

LABEL_KEYS = "6789"     # Lightroom-Belegung: 6=Rot, 7=Gelb, 8=Grün, 9=Blau


def label_text(index: int | None, set_name: str = "de") -> str:
    """Text für einen Farbindex 0-4 im angegebenen Satz."""
    labels = LABEL_SETS.get(set_name) or LABEL_SETS["de"]
    if index is None or not (0 <= index < len(labels)):
        return ""
    return labels[index]


def label_index(text: str | None) -> int | None:
    """Farbindex zu einem Markierungstext, geprüft gegen ALLE Sätze.

    None bedeutet: unbekannter Text. Der Aufrufer behält ihn dann
    unverändert bei, statt ihn zu überschreiben.
    """
    if not text:
        return None
    needle = text.strip().casefold()
    for labels in LABEL_SETS.values():
        for i, name in enumerate(labels):
            if name.casefold() == needle:
                return i
    return None


def label_color(text: str | None) -> str | None:
    """Farbe für die Kachel, oder None bei fehlender/unbekannter Markierung."""
    index = label_index(text)
    return LABEL_COLORS[index] if index is not None else None


def is_reject(rating: int | None) -> bool:
    return int(rating or 0) == REJECT


def clamp_rating(value: int) -> int:
    """Erlaubt -1 (abgelehnt) sowie 0 bis 5."""
    value = int(value)
    if value <= REJECT:
        return REJECT
    return max(0, min(5, value))
