"""Retusche-Rechenkerne.

Alles arbeitet auf einem RGB-Feld (numpy, float32, 0…1) und gibt ein
neues Feld zurück - keine Funktion verändert ihre Eingabe. Das Original
auf der Platte wird nie angefasst; gerechnet wird beim Anzeigen und beim
Ausgeben.

Absichtlich ohne Zwangsabhängigkeit: alles läuft mit numpy allein. Ist
OpenCV zufällig installiert, wird es für das Füllen von Rissen benutzt,
weil das Telea-Verfahren dünne Strukturen sauberer schließt als das
einfache Ausbreiten hier.
"""

from __future__ import annotations

import numpy as np

try:
    import cv2
    HAVE_CV2 = True
except ImportError:      # pragma: no cover
    cv2 = None
    HAVE_CV2 = False


# -- Hilfen ------------------------------------------------------------

def _disc(radius: int, feather: float = 0.35) -> np.ndarray:
    """Weiche Kreisscheibe als Deckungsmaske, Werte 0…1."""
    size = radius * 2 + 1
    y, x = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    dist = np.sqrt(x * x + y * y) / max(radius, 1)
    inner = max(0.0, 1.0 - feather)
    alpha = np.clip((1.0 - dist) / max(feather, 1e-6), 0.0, 1.0)
    alpha[dist <= inner] = 1.0
    alpha[dist > 1.0] = 0.0
    return alpha.astype(np.float32)


def _clip_box(shape, cx: int, cy: int, radius: int):
    """Ausschnitt um einen Punkt, am Bildrand beschnitten."""
    height, width = shape[:2]
    x0, x1 = cx - radius, cx + radius + 1
    y0, y1 = cy - radius, cy + radius + 1
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None
    return x0, y0, x1, y1


# -- Flecken und Pickel ------------------------------------------------

def heal_spot(rgb: np.ndarray, cx: int, cy: int, radius: int,
              feather: float = 0.35) -> np.ndarray:
    """Einen runden Fleck durch passende Umgebung ersetzen.

    Vorgehen wie beim Reparaturpinsel: in der Nachbarschaft die Stelle
    suchen, deren RAND dem Rand der Fehlstelle am ähnlichsten ist, diese
    Stelle herüberkopieren, ihre Helligkeit an die Fehlstelle angleichen
    und weich einblenden. Der Helligkeitsabgleich ist wichtig - ohne ihn
    entsteht ein Fleck anderer Farbe statt einer unauffälligen Stelle.
    """
    radius = max(2, int(radius))
    box = _clip_box(rgb.shape, int(cx), int(cy), radius)
    if box is None:
        return rgb
    x0, y0, x1, y1 = box

    alpha = _disc(radius, feather)
    ring = (alpha > 0.02) & (alpha < 0.75)     # der Randbereich
    if not ring.any():
        ring = alpha > 0.02

    target = rgb[y0:y1, x0:x1]
    target_ring = target[ring]

    best = None
    best_error = np.inf
    # Kandidaten ringsum, in mehreren Abständen und Richtungen
    for factor in (1.8, 2.6, 3.6):
        step = int(radius * factor)
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step),
                       (step, step), (-step, -step),
                       (step, -step), (-step, step)):
            sbox = _clip_box(rgb.shape, int(cx) + dx, int(cy) + dy, radius)
            if sbox is None:
                continue
            sx0, sy0, sx1, sy1 = sbox
            source = rgb[sy0:sy1, sx0:sx1]
            error = float(np.mean((source[ring] - target_ring) ** 2))
            if error < best_error:
                best_error = error
                best = source

    if best is None:
        return rgb

    # Farbversatz ausgleichen: der Rand der Quelle soll dem Rand des
    # Ziels entsprechen, sonst sieht man den Flicken.
    offset = target_ring.mean(axis=0) - best[ring].mean(axis=0)
    patch = np.clip(best + offset, 0.0, 1.0)

    result = rgb.copy()
    blend = alpha[..., None]
    result[y0:y1, x0:x1] = target * (1.0 - blend) + patch * blend
    return result


def heal_stroke(rgb: np.ndarray, points: list[tuple[int, int]],
                radius: int) -> np.ndarray:
    """Reparaturpinsel entlang einer Linie - für Risse und Kratzer."""
    if not points:
        return rgb
    result = rgb
    step = max(2, radius // 2)
    for x, y in _resample(points, step):
        result = heal_spot(result, x, y, radius)
    return result


def _resample(points: list[tuple[int, int]], step: int):
    """Punkte auf gleichmäßigen Abstand bringen, damit keine Lücken bleiben."""
    out = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distance = max(abs(x1 - x0), abs(y1 - y0))
        count = max(1, distance // step)
        for i in range(1, count + 1):
            out.append((int(x0 + (x1 - x0) * i / count),
                        int(y0 + (y1 - y0) * i / count)))
    return out


# -- Risse füllen ------------------------------------------------------

def inpaint(rgb: np.ndarray, mask: np.ndarray, radius: int = 3) -> np.ndarray:
    """Maskierte Stellen aus der Umgebung auffüllen.

    Mit OpenCV das Telea-Verfahren, sonst ein einfaches Ausbreiten der
    Nachbarwerte. Für dünne Risse reicht beides; bei breiten Fehlstellen
    ist der Unterschied deutlich.
    """
    mask = mask.astype(bool)
    if not mask.any():
        return rgb

    if HAVE_CV2:
        source = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        filled = cv2.inpaint(source, mask.astype(np.uint8) * 255,
                             radius, cv2.INPAINT_TELEA)
        return filled.astype(np.float32) / 255.0

    return _diffuse(rgb, mask)


def _diffuse(rgb: np.ndarray, mask: np.ndarray, rounds: int = 60) -> np.ndarray:
    """Notlösung ohne OpenCV: Fehlstellen von außen nach innen zuwachsen lassen."""
    result = rgb.copy()
    unknown = mask.copy()
    for _ in range(rounds):
        if not unknown.any():
            break
        padded = np.pad(result, ((1, 1), (1, 1), (0, 0)), mode="edge")
        known = np.pad((~unknown).astype(np.float32), ((1, 1), (1, 1)),
                       mode="edge")[..., None]

        total = np.zeros_like(result)
        weight = np.zeros_like(known[1:-1, 1:-1])
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                if dy == 1 and dx == 1:
                    continue
                neighbour = padded[dy:dy + result.shape[0],
                                   dx:dx + result.shape[1]]
                mark = known[dy:dy + result.shape[0], dx:dx + result.shape[1]]
                total += neighbour * mark
                weight += mark

        fillable = unknown & (weight[..., 0] > 0)
        if not fillable.any():
            break
        safe = np.maximum(weight, 1e-6)
        result[fillable] = (total / safe)[fillable]
        unknown = unknown & ~fillable
    return result


# -- Rote Augen --------------------------------------------------------

def fix_red_eye(rgb: np.ndarray, cx: int, cy: int, radius: int,
                strength: float = 1.0) -> np.ndarray:
    """Rotes Leuchten in einem Kreis dämpfen.

    Betroffen sind nur Bildpunkte, in denen Rot deutlich über Grün und
    Blau liegt. Die Pupille wird dabei nicht schwarz gemalt, sondern der
    Rotkanal auf das Niveau der anderen beiden gezogen - Glanzlichter im
    Auge bleiben so erhalten.
    """
    radius = max(2, int(radius))
    box = _clip_box(rgb.shape, int(cx), int(cy), radius)
    if box is None:
        height, width = rgb.shape[:2]
        x0, y0 = max(0, int(cx) - radius), max(0, int(cy) - radius)
        x1, y1 = min(width, int(cx) + radius + 1), min(height, int(cy) + radius + 1)
        if x1 <= x0 or y1 <= y0:
            return rgb
    else:
        x0, y0, x1, y1 = box

    region = rgb[y0:y1, x0:x1]
    red, green, blue = region[..., 0], region[..., 1], region[..., 2]
    other = np.maximum(green, blue)

    # Wie stark überwiegt Rot? 0 = gar nicht.
    excess = np.clip(red - other, 0.0, 1.0)
    mask = (excess > 0.06) & (red > 0.18)
    if not mask.any():
        return rgb

    # Weicher Übergang: je röter, desto stärker die Korrektur
    weight = np.clip(excess / 0.35, 0.0, 1.0) * float(strength)
    target = (green + blue) * 0.5

    result = rgb.copy()
    fixed = region.copy()
    fixed[..., 0] = np.where(mask, red * (1 - weight) + target * weight, red)
    result[y0:y1, x0:x1] = fixed
    return result


# -- Ausgeblichene Farben ----------------------------------------------

def restore_faded(rgb: np.ndarray, strength: float = 1.0,
                  neutralise: bool = True, saturation: float = 0.25,
                  low: float = 0.005, high: float = 0.995) -> np.ndarray:
    """Ausgeblichene Aufnahmen auffrischen.

    Drei Schritte, wie man es bei alten Abzügen und Dias braucht:

      1. Jeden Farbkanal einzeln auf den vollen Bereich ziehen. Das
         allein holt Kontrast zurück UND entfernt den typischen
         Farbstich, weil verblasste Kanäle unterschiedlich weit
         geschrumpft sind.
      2. Auf Wunsch den Rest des Stichs über die Grauwelt-Annahme
         wegrechnen.
      3. Eine Spur Sättigung, weil Schritt 1 den Kontrast stärker hebt
         als die Farbigkeit.

    strength blendet zwischen Original (0) und voller Korrektur (1).
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0:
        return rgb

    work = np.clip(rgb, 0.0, 1.0)
    stretched = np.empty_like(work)
    for channel in range(3):
        values = work[..., channel]
        lo = float(np.quantile(values, low))
        hi = float(np.quantile(values, high))
        if hi - lo < 1e-4:
            stretched[..., channel] = values
        else:
            stretched[..., channel] = np.clip((values - lo) / (hi - lo), 0.0, 1.0)

    if neutralise:
        means = stretched.reshape(-1, 3).mean(axis=0)
        grey = float(means.mean())
        if grey > 1e-4:
            factors = np.clip(grey / np.maximum(means, 1e-4), 0.75, 1.35)
            stretched = np.clip(stretched * factors, 0.0, 1.0)

    if saturation:
        luma = (stretched * np.array([0.2126, 0.7152, 0.0722],
                                     dtype=np.float32)).sum(axis=2, keepdims=True)
        stretched = np.clip(
            luma + (stretched - luma) * (1.0 + float(saturation)), 0.0, 1.0
        )

    return np.clip(work * (1 - strength) + stretched * strength, 0.0, 1.0)


def auto_faded_strength(rgb: np.ndarray) -> float:
    """Schätzt, wie stark ein Bild ausgeblichen ist (0…1).

    Grundlage ist der genutzte Tonwertumfang: ein Bild, das nur zwischen
    0,3 und 0,7 lebt, ist verblasst; eines, das von 0 bis 1 reicht,
    braucht nichts.
    """
    work = np.clip(rgb, 0.0, 1.0)
    spans = []
    for channel in range(3):
        values = work[..., channel]
        spans.append(float(np.quantile(values, 0.99) - np.quantile(values, 0.01)))
    span = float(np.mean(spans))
    return float(np.clip((0.85 - span) / 0.55, 0.0, 1.0))


# -- Grundeinstellungen ------------------------------------------------

def apply_tone(rgb: np.ndarray, exposure: float = 0.0, contrast: float = 0.0,
               saturation: float = 0.0, warmth: float = 0.0, tint: float = 0.0,
               shadows: float = 0.0, highlights: float = 0.0,
               clarity: float = 0.0, detail: float = 0.0) -> np.ndarray:
    """Belichtung, Kontrast, Farbe - alle Regler in einem Durchgang.

    Alle Werte sind 0 = unverändert. exposure zählt in Blendenstufen,
    der Rest läuft von -1 bis +1.

    Die Reihenfolge ist nicht beliebig: erst Weißabgleich (sonst
    verschiebt der Kontrast den Farbstich mit), dann Belichtung, dann
    Tiefen und Lichter, dann Kontrast, zuletzt Sättigung.
    """
    result = np.clip(rgb, 0.0, 1.0).astype(np.float32)

    if warmth or tint:
        # Grob, aber für die Praxis brauchbar: Rot gegen Blau für die
        # Wärme, Grün gegen Magenta für den Tint.
        factors = np.array([
            1.0 + 0.30 * warmth,
            1.0 + 0.18 * tint,
            1.0 - 0.30 * warmth,
        ], dtype=np.float32)
        result = np.clip(result * factors, 0.0, 1.0)

    if exposure:
        result = np.clip(result * (2.0 ** float(exposure)), 0.0, 1.0)

    if shadows or highlights:
        luma = _luma(result)
        if shadows:
            # Maske, die zu den dunklen Stellen hin ansteigt
            mask = np.clip(1.0 - luma * 2.0, 0.0, 1.0)[..., None]
            result = np.clip(result + shadows * 0.45 * mask * (1.0 - result), 0.0, 1.0) \
                if shadows > 0 else np.clip(result + shadows * 0.45 * mask * result, 0.0, 1.0)
        if highlights:
            mask = np.clip((luma - 0.5) * 2.0, 0.0, 1.0)[..., None]
            result = np.clip(result + highlights * 0.45 * mask * (1.0 - result), 0.0, 1.0) \
                if highlights > 0 else np.clip(result + highlights * 0.45 * mask * result, 0.0, 1.0)

    if contrast:
        factor = 1.0 + float(contrast)
        result = np.clip((result - 0.5) * factor + 0.5, 0.0, 1.0)

    if clarity:
        result = apply_clarity(result, clarity)
    if detail:
        result = apply_detail(result, detail)

    if saturation:
        luma = _luma(result)[..., None]
        result = np.clip(luma + (result - luma) * (1.0 + float(saturation)),
                         0.0, 1.0)
    return result


def _blur(rgb: np.ndarray, radius: float) -> np.ndarray:
    """Weichzeichnen über Pillow - schneller als alles von Hand Gebaute."""
    if radius < 0.5:
        return rgb
    from PIL import Image, ImageFilter
    image = Image.fromarray(to_uint8(rgb))
    image = image.filter(ImageFilter.GaussianBlur(radius=float(radius)))
    return np.asarray(image, dtype=np.float32) / 255.0


def apply_clarity(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Klarheit: Kontrast der mittleren Strukturen.

    Unterschied zu „Details": der Radius ist groß (etwa 2 % der kürzeren
    Kante). Das hebt Wolken, Hauttextur, Mauerwerk - ohne die feinen
    Kanten zu überschärfen.

    Die Mitteltöne werden stärker angefasst als Lichter und Tiefen.
    Ohne diese Gewichtung laufen helle Flächen zu und dunkle saufen ab -
    genau das, was übertriebene Klarheit hässlich macht.
    """
    amount = float(amount)
    if not amount:
        return rgb
    radius = max(2.0, min(rgb.shape[:2]) * 0.02)
    base = _blur(rgb, radius)
    detail_layer = rgb - base

    luma = _luma(rgb)
    # 1 in den Mitteltönen, 0 an den Enden
    weight = (1.0 - np.abs(luma - 0.5) * 2.0) ** 0.75
    weight = np.clip(weight, 0.0, 1.0)[..., None]

    return np.clip(rgb + detail_layer * amount * 1.2 * weight, 0.0, 1.0)


def apply_detail(rgb: np.ndarray, amount: float,
                 threshold: float = 0.010) -> np.ndarray:
    """Details: feine Schärfung mit kleinem Radius.

    Die Schwelle dämpft schwache Unterschiede, damit in glatten Flächen
    (Himmel, Haut) nicht das Rauschen mitgeschärft wird.

    Wichtig: sie wirkt WEICH, nicht als harter Schnitt. Ein harter
    Schwellwert löscht auch echte Kanten, sobald sie knapp darunter
    liegen - eine leicht unscharfe Kante bewegte sich damit gar nicht
    mehr. Der weiche Verlauf lässt schwache Anteile durch, nur eben
    gedämpft.
    """
    amount = float(amount)
    if not amount:
        return rgb
    base = _blur(rgb, 1.2)
    detail_layer = rgb - base

    if threshold > 0:
        # Weicher Verlauf mit leichter Krümmung. Ein harter Schnitt
        # löscht echte Kanten, die knapp darunter liegen; rein linear
        # bleibt zu viel Rauschen übrig. Der Kompromiss liegt bei 1,5.
        weight = np.clip(np.abs(detail_layer) / threshold, 0.0, 1.0) ** 1.5
        detail_layer = detail_layer * weight

    return np.clip(rgb + detail_layer * amount * 2.5, 0.0, 1.0)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return (rgb * np.array([0.2126, 0.7152, 0.0722],
                           dtype=np.float32)).sum(axis=2)


def white_balance_from_pixel(rgb: np.ndarray, cx: int, cy: int,
                             radius: int = 4) -> tuple[float, float]:
    """Pipette: liefert die Regler, die den angeklickten Punkt neutral machen.

    Gerechnet wird gegen die Formeln in apply_tone: Rot und Blau werden
    um 0,30 gegeneinander verschoben, Grün um 0,18. Daraus lässt sich
    direkt auflösen, statt zu probieren.
    """
    height, width = rgb.shape[:2]
    x0 = max(0, int(cx) - radius); x1 = min(width, int(cx) + radius + 1)
    y0 = max(0, int(cy) - radius); y1 = min(height, int(cy) + radius + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0

    red, green, blue = [float(v) for v in
                        rgb[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)]
    if red + blue < 1e-4:
        return 0.0, 0.0

    # Rot gleich Blau: r*(1+0.3w) = b*(1-0.3w)
    warmth = float(np.clip((blue - red) / (0.30 * (blue + red)), -1.0, 1.0))
    corrected = red * (1.0 + 0.30 * warmth)
    if green < 1e-4:
        return warmth, 0.0
    # Grün auf dieselbe Höhe: g*(1+0.18t) = corrected
    tint = float(np.clip((corrected / green - 1.0) / 0.18, -1.0, 1.0))
    return warmth, tint


def crop(rgb: np.ndarray, x: float, y: float, width: float,
         height: float) -> np.ndarray:
    """Zuschnitt. Alle vier Werte sind Bruchteile der Bildgröße (0…1)."""
    full_h, full_w = rgb.shape[:2]
    x0 = int(round(np.clip(x, 0.0, 1.0) * full_w))
    y0 = int(round(np.clip(y, 0.0, 1.0) * full_h))
    x1 = int(round(np.clip(x + width, 0.0, 1.0) * full_w))
    y1 = int(round(np.clip(y + height, 0.0, 1.0) * full_h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return rgb
    return rgb[y0:y1, x0:x1]


# -- Umwandlung --------------------------------------------------------

def to_float(image: np.ndarray) -> np.ndarray:
    if image.dtype == np.uint8:
        return image.astype(np.float32) / 255.0
    return image.astype(np.float32)


def to_uint8(image: np.ndarray) -> np.ndarray:
    return (np.clip(image, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# -- Farbtemperatur ----------------------------------------------------

DEFAULT_KELVIN = 5500        # Tageslicht, wenn die Datei nichts hergibt
MIRED_PER_UNIT = 80.0        # Wärme +1 verschiebt um so viele Mired

def warmth_to_kelvin(warmth: float, base_kelvin: float = DEFAULT_KELVIN) -> float:
    """Reglerwert in eine Kelvin-Anzeige übersetzen.

    Gerechnet wird über Mired (eine Million geteilt durch Kelvin), weil
    dort gleiche Schritte auch gleich aussehen: von 3000 auf 3500 K ist
    ein sichtbarer Sprung, von 9000 auf 9500 K kaum etwas.

    ACHTUNG: Das ist eine ANZEIGE, keine farbmetrische Umrechnung. Der
    Wärmeregler verschiebt Rot und Blau linear gegeneinander; ein echter
    Weißabgleich würde über das Aufnahmeprofil der Kamera gehen. Die
    Zahl ist als Anhaltspunkt gedacht, nicht als Messwert.
    """
    base = float(base_kelvin or DEFAULT_KELVIN)
    base = min(max(base, 1500.0), 25000.0)
    mired = 1e6 / base - float(warmth) * MIRED_PER_UNIT
    mired = min(max(mired, 40.0), 666.0)      # etwa 25000 bis 1500 K
    return 1e6 / mired


def kelvin_to_warmth(kelvin: float, base_kelvin: float = DEFAULT_KELVIN) -> float:
    """Gegenrichtung - für eine Eingabe in Kelvin."""
    base = float(base_kelvin or DEFAULT_KELVIN)
    base = min(max(base, 1500.0), 25000.0)
    kelvin = min(max(float(kelvin), 1500.0), 25000.0)
    return (1e6 / base - 1e6 / kelvin) / MIRED_PER_UNIT
