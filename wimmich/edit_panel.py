"""Bearbeitungsleiste - die rechte Spalte der Lupenansicht.

Kein eigenes Fenster mehr: das Panel sitzt im Hauptfenster und meldet
Änderungen über Signale. Es kennt die Datenbank nicht und lädt keine
Bilder; es sagt nur, was eingestellt wurde.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)

from . import theme
from . import retouch
from .edits import (
    GEOMETRY, Step, TONE, geometry_is_neutral, tone_is_neutral,
)

# (Feld im Step, Beschriftung, kleinster, größter, Einheit, Teiler)
TONE_SLIDERS = [
    ("exposure", "Belichtung", -200, 200, "EV", 100.0),
    ("contrast", "Kontrast", -100, 100, "", 100.0),
    ("shadows", "Tiefen", -100, 100, "", 100.0),
    ("highlights", "Lichter", -100, 100, "", 100.0),
    ("clarity", "Klarheit", -100, 100, "", 100.0),
    ("detail", "Details", -100, 100, "", 100.0),
    ("saturation", "Sättigung", -100, 100, "", 100.0),
    ("warmth", "Wärme", -100, 100, "K", 100.0),
    ("tint", "Tint", -100, 100, "", 100.0),
]

# (Feld, Beschriftung, kleinster, größter, Teiler)
GEO_SLIDERS = [
    ("angle", "Drehung", -4500, 4500, 100.0),        # ±45 Grad, 0,01er Schritte
    ("persp_v", "Perspektive ↕", -100, 100, 100.0),
    ("persp_h", "Perspektive ↔", -100, 100, 100.0),
]

EV_STEP = 25        # ein Tastendruck auf + oder - in Reglereinheiten (0,25 EV)


class EditPanel(QWidget):
    """Regler für Grundeinstellungen und Auffrischen."""

    tone_changed = pyqtSignal(object)      # Step oder None
    geometry_changed = pyqtSignal(object)  # Step oder None
    rotate_quarter = pyqtSignal(int)       # +1 rechts, -1 links
    fade_changed = pyqtSignal(float, bool)  # Stärke, Farbstich entfernen
    suggest_fade = pyqtSignal()
    pipette_toggled = pyqtSignal(bool)
    crop_cleared = pyqtSignal()
    reset_all = pyqtSignal()
    undo = pyqtSignal()
    export = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(300)
        self._loading = False
        # Aufnahmetemperatur des gerade gezeigten Bildes; None heißt:
        # die Datei gibt nichts her, wir rechnen mit Tageslicht.
        self._base_kelvin: int | None = None
        self._quarters = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 10, 4)
        layout.setSpacing(6)

        layout.addWidget(_heading("Grundeinstellungen"))
        form = QFormLayout()
        form.setSpacing(6)
        self.sliders: dict[str, QSlider] = {}
        self.readouts: dict[str, QLabel] = {}

        for field, label, low, high, _unit, _scale in TONE_SLIDERS:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            slider.valueChanged.connect(self._emit_tone)
            row_layout.addWidget(slider, 1)

            readout = QLabel("–")
            readout.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            readout.setFixedWidth(64)
            readout.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(readout)

            self.sliders[field] = slider
            self.readouts[field] = readout
            form.addRow(label, row)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.pipette_button = QPushButton("Pipette  W")
        self.pipette_button.setCheckable(True)
        self.pipette_button.setToolTip(
            "Auf eine neutrale graue oder weiße Stelle klicken"
        )
        self.kelvin_hint = QLabel("")
        self.kelvin_hint.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 13px;")
        self.kelvin_hint.setWordWrap(True)
        self.pipette_button.toggled.connect(self.pipette_toggled)
        row.addWidget(self.pipette_button)
        reset_tone = QPushButton("Zurücksetzen")
        reset_tone.clicked.connect(self._reset_tone)
        row.addWidget(reset_tone)
        layout.addLayout(row)

        layout.addWidget(self.kelvin_hint)

        layout.addWidget(_separator())
        layout.addWidget(_heading("Drehen und Entzerren"))

        dreh = QHBoxLayout()
        links = QPushButton("↺ 90°")
        links.setToolTip("Gegen den Uhrzeigersinn (Umschalt+R)")
        links.clicked.connect(lambda: self.rotate_quarter.emit(-1))
        dreh.addWidget(links)
        rechts = QPushButton("↻ 90°")
        rechts.setToolTip("Im Uhrzeigersinn (R)")
        rechts.clicked.connect(lambda: self.rotate_quarter.emit(1))
        dreh.addWidget(rechts)
        layout.addLayout(dreh)

        geo_form = QFormLayout()
        geo_form.setSpacing(6)
        self.geo_sliders: dict[str, QSlider] = {}
        self.geo_readouts: dict[str, QLabel] = {}
        for field, label, low, high, _scale in GEO_SLIDERS:
            zeile = QWidget()
            zl = QHBoxLayout(zeile)
            zl.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            slider.valueChanged.connect(self._emit_geometry)
            zl.addWidget(slider, 1)
            readout = QLabel("–")
            readout.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            readout.setFixedWidth(64)
            readout.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)
            zl.addWidget(readout)
            self.geo_sliders[field] = slider
            self.geo_readouts[field] = readout
            geo_form.addRow(label, zeile)
        layout.addLayout(geo_form)

        geo_reset = QPushButton("Geometrie zurücksetzen")
        geo_reset.clicked.connect(self._reset_geometry)
        layout.addWidget(geo_reset)

        layout.addWidget(_separator())
        layout.addWidget(_heading("Ausgeblichene Farben"))

        fade_row = QHBoxLayout()
        self.fade_slider = QSlider(Qt.Orientation.Horizontal)
        self.fade_slider.setRange(0, 100)
        self.fade_slider.valueChanged.connect(self._emit_fade)
        fade_row.addWidget(self.fade_slider, 1)
        self.fade_label = QLabel("aus")
        self.fade_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.fade_label.setFixedWidth(52)
        self.fade_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter)
        fade_row.addWidget(self.fade_label)
        layout.addLayout(fade_row)

        self.neutral_box = QCheckBox("Farbstich entfernen")
        self.neutral_box.setChecked(True)
        self.neutral_box.toggled.connect(self._emit_fade)
        layout.addWidget(self.neutral_box)

        suggest = QPushButton("Stärke vorschlagen")
        suggest.clicked.connect(self.suggest_fade)
        layout.addWidget(suggest)

        layout.addWidget(_separator())
        self.crop_label = QLabel("Zuschnitt: keiner")
        self.crop_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.crop_label)
        crop_button = QPushButton("Zuschnitt aufheben  Shift+C")
        crop_button.clicked.connect(self.crop_cleared)
        layout.addWidget(crop_button)

        layout.addStretch(1)

        layout.addWidget(_separator())
        undo_button = QPushButton("Rückgängig  Strg+Z")
        undo_button.clicked.connect(self.undo)
        layout.addWidget(undo_button)
        reset_button = QPushButton("Alles zurücknehmen")
        reset_button.clicked.connect(self.reset_all)
        layout.addWidget(reset_button)
        export_button = QPushButton("Als Datei speichern …")
        export_button.clicked.connect(self.export)
        layout.addWidget(export_button)

    # -- Von außen befüllen --------------------------------------------

    def load(self, tone: Step | None, fade: Step | None,
             crop_text: str, base_kelvin: int | None = None,
             geo: Step | None = None) -> None:
        """Regler auf ein Bild setzen, ohne Signale auszulösen."""
        self._base_kelvin = base_kelvin
        self._quarters = int(getattr(geo, "quarters", 0) or 0) % 4
        self._loading = True
        for field, _l, _lo, _hi, scale in GEO_SLIDERS:
            wert = getattr(geo, field, 0.0) if geo else 0.0
            self.geo_sliders[field].setValue(int(round(wert * scale)))
        for field, _l, _lo, _hi, _u, scale in TONE_SLIDERS:
            value = getattr(tone, field, 0.0) if tone else 0.0
            self.sliders[field].setValue(int(round(value * scale)))
        self.fade_slider.setValue(int(round((fade.strength if fade else 0) * 100)))
        if fade:
            self.neutral_box.setChecked(bool(fade.neutralise))
        self._loading = False
        self._update_readouts()
        self._update_geo_readouts()
        self._update_fade_label()
        self.crop_label.setText(crop_text)
        self.kelvin_hint.setText(
            f"Aufnahme: {base_kelvin} K (aus der Datei)" if base_kelvin
            else f"Aufnahme: keine Angabe in der Datei, "
                 f"gerechnet ab {retouch.DEFAULT_KELVIN} K"
        )

    def set_crop_text(self, text: str) -> None:
        self.crop_label.setText(text)

    def set_pipette_checked(self, on: bool) -> None:
        self.pipette_button.blockSignals(True)
        self.pipette_button.setChecked(on)
        self.pipette_button.blockSignals(False)

    def step_exposure(self, direction: int) -> None:
        """Taste + oder - ohne Steuerungstaste."""
        slider = self.sliders["exposure"]
        slider.setValue(slider.value() + direction * EV_STEP)

    def set_white_balance(self, warmth: float, tint: float) -> None:
        self.sliders["warmth"].setValue(int(round(warmth * 100)))
        self.sliders["tint"].setValue(int(round(tint * 100)))

    def set_quarters(self, quarters: int) -> None:
        """Vierteldrehung von außen setzen (Taste R)."""
        self._quarters = int(quarters) % 4
        self._emit_geometry()

    def current_geometry(self) -> Step | None:
        step = Step(kind=GEOMETRY, quarters=self._quarters)
        for field, _l, _lo, _hi, scale in GEO_SLIDERS:
            setattr(step, field, self.geo_sliders[field].value() / scale)
        return None if geometry_is_neutral(step) else step

    def quarters(self) -> int:
        return self._quarters

    def current_tone(self) -> Step | None:
        step = Step(kind=TONE)
        for field, _l, _lo, _hi, _u, scale in TONE_SLIDERS:
            setattr(step, field, self.sliders[field].value() / scale)
        return None if tone_is_neutral(step) else step

    # -- Innen ---------------------------------------------------------

    def _emit_tone(self) -> None:
        if self._loading:
            return
        self._update_readouts()
        self.tone_changed.emit(self.current_tone())

    def _emit_geometry(self) -> None:
        if self._loading:
            return
        self._update_geo_readouts()
        self.geometry_changed.emit(self.current_geometry())

    def _update_geo_readouts(self) -> None:
        for field, _l, _lo, _hi, scale in GEO_SLIDERS:
            wert = self.geo_sliders[field].value() / scale
            readout = self.geo_readouts[field]
            if abs(wert) < 1e-9:
                readout.setText(f"{self._quarters * 90}°"
                                if field == "angle" and self._quarters else "–")
            elif field == "angle":
                gesamt = self._quarters * 90
                readout.setText(f"{wert:+.2f}°" if not gesamt
                                else f"{gesamt}{wert:+.2f}°")
            else:
                readout.setText(f"{wert * 100:+.0f}")

    def _reset_geometry(self) -> None:
        self._loading = True
        self._quarters = 0
        for slider in self.geo_sliders.values():
            slider.setValue(0)
        self._loading = False
        self._update_geo_readouts()
        self.geometry_changed.emit(None)

    def _emit_fade(self) -> None:
        if self._loading:
            return
        self._update_fade_label()
        self.fade_changed.emit(self.fade_slider.value() / 100.0,
                               self.neutral_box.isChecked())

    def _update_readouts(self) -> None:
        for field, _l, _lo, _hi, unit, scale in TONE_SLIDERS:
            value = self.sliders[field].value() / scale
            readout = self.readouts[field]
            if abs(value) < 1e-6:
                # Bei der Wärme ist "nichts geändert" trotzdem eine
                # Temperatur - nämlich die der Aufnahme.
                if unit == "K":
                    base = self._base_kelvin or retouch.DEFAULT_KELVIN
                    readout.setText(
                        f"{base} K" if self._base_kelvin else f"~{base} K")
                else:
                    readout.setText("–")
            elif unit == "EV":
                # Blendenstufen: +1 EV ist die doppelte Lichtmenge
                readout.setText(f"{value:+.2f}")
            elif unit == "K":
                kelvin = retouch.warmth_to_kelvin(
                    value, self._base_kelvin or retouch.DEFAULT_KELVIN)
                readout.setText(f"{kelvin:.0f} K")
            else:
                readout.setText(f"{value * 100:+.0f}")

    def _update_fade_label(self) -> None:
        value = self.fade_slider.value()
        self.fade_label.setText(f"{value} %" if value else "aus")

    def _reset_tone(self) -> None:
        self._loading = True
        for slider in self.sliders.values():
            slider.setValue(0)
        self._loading = False
        self._update_readouts()
        self.tone_changed.emit(None)


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600; padding: 4px 0;")
    return label


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {theme.BORDER}; max-height: 1px;")
    return line
