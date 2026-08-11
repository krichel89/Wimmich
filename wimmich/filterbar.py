"""Filterleiste: Sterne und Farben zum Anklicken.

Die Regel stammt aus Cammello und ist dort so festgehalten: Sterne
wirken als UND (»so viele und mehr«, wie Lightrooms >=), Farben als
ODER (mehrere Farben nebeneinander), und beide Gruppen zusammen wieder
als UND. Drei Sterne UND (Rot ODER Grün).

Ein abgelehntes Bild kommt durch keinen aktiven Sternfilter - eine
Ablehnung hat keine sinnvolle Sternzahl.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from . import marks, theme

STERN = "\u2605"
LEER = "\u2606"


class FilterBar(QWidget):
    """Sterne, Farben und Ablehnungen filtern."""

    changed = pyqtSignal()

    def __init__(self, label_set: str = "de", parent=None) -> None:
        super().__init__(parent)
        self._min_rating = 0
        self._label_set = label_set
        self._colors: set[int] = set()
        self._unlabeled = False
        self._show_rejects = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # -- Sterne ----------------------------------------------------
        self._stars: list[QPushButton] = []
        for wert in range(1, 6):
            knopf = QPushButton(LEER)
            knopf.setCheckable(True)
            knopf.setFixedSize(QSize(26, 26))
            knopf.setToolTip(f"{wert} Sterne und mehr - nochmal klicken hebt auf")
            knopf.clicked.connect(lambda _c, v=wert: self._star_clicked(v))
            knopf.setStyleSheet(_STERN_STIL)
            layout.addWidget(knopf)
            self._stars.append(knopf)

        layout.addSpacing(10)

        # -- Farben ----------------------------------------------------
        self._swatches: list[QPushButton] = []
        for index, farbe in enumerate(marks.LABEL_COLORS):
            knopf = QPushButton()
            knopf.setCheckable(True)
            knopf.setFixedSize(QSize(24, 24))
            knopf.setToolTip(
                f"{marks.label_text(index, label_set)} - mehrere möglich")
            knopf.setStyleSheet(_farb_stil(farbe))
            knopf.clicked.connect(lambda _c, i=index: self._color_clicked(i))
            layout.addWidget(knopf)
            self._swatches.append(knopf)

        self._ohne = QPushButton("ohne")
        self._ohne.setCheckable(True)
        self._ohne.setFixedHeight(26)
        self._ohne.setToolTip("Bilder ohne Farbmarkierung")
        self._ohne.clicked.connect(self._unlabeled_clicked)
        layout.addWidget(self._ohne)

        layout.addSpacing(10)

        # -- Abgelehnte ------------------------------------------------
        self._reject = QPushButton("✕")
        self._reject.setCheckable(True)
        self._reject.setChecked(True)
        self._reject.setFixedSize(QSize(26, 26))
        self._reject.setToolTip(
            "Abgelehnte mitzeigen.\nBei aktivem Sternfilter fallen sie "
            "immer heraus.")
        self._reject.clicked.connect(self._reject_clicked)
        layout.addWidget(self._reject)

        self._clear = QPushButton("zurücksetzen")
        self._clear.setFixedHeight(26)
        self._clear.clicked.connect(self.reset)
        layout.addWidget(self._clear)

        self._summary = QLabel("")
        self._summary.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 13px;")
        layout.addWidget(self._summary)

        self._update_stars()
        self._update_summary()

    # -- Zustand -------------------------------------------------------

    def min_rating(self) -> int:
        return self._min_rating

    def show_rejects(self) -> bool:
        return self._show_rejects

    def label_texts(self) -> list[str] | None:
        """Markierungstexte in ALLEN Sprachen - sonst findet ein Filter
        auf „Rot" die Bilder nicht, die Lightroom als „Red" markiert hat."""
        if not self._colors:
            return None
        texte = []
        for index in sorted(self._colors):
            texte.extend(satz[index] for satz in marks.LABEL_SETS.values()
                         if index < len(satz))
        return texte

    def include_unlabeled(self) -> bool:
        return self._unlabeled

    def active(self) -> bool:
        return bool(self._min_rating or self._colors or self._unlabeled
                    or not self._show_rejects)

    def set_label_set(self, name: str) -> None:
        self._label_set = name
        for index, knopf in enumerate(self._swatches):
            knopf.setToolTip(f"{marks.label_text(index, name)} - mehrere möglich")

    # -- Klicks --------------------------------------------------------

    def _star_clicked(self, wert: int) -> None:
        # Nochmal auf denselben Stern hebt den Filter auf
        self._min_rating = 0 if self._min_rating == wert else wert
        self._update_stars()
        self._fertig()

    def _color_clicked(self, index: int) -> None:
        if index in self._colors:
            self._colors.discard(index)
        else:
            self._colors.add(index)
        self._swatches[index].setChecked(index in self._colors)
        self._fertig()

    def _unlabeled_clicked(self) -> None:
        self._unlabeled = self._ohne.isChecked()
        self._fertig()

    def _reject_clicked(self) -> None:
        self._show_rejects = self._reject.isChecked()
        self._fertig()

    def reset(self) -> None:
        self._min_rating = 0
        self._colors.clear()
        self._unlabeled = False
        self._show_rejects = True
        for knopf in self._swatches:
            knopf.setChecked(False)
        self._ohne.setChecked(False)
        self._reject.setChecked(True)
        self._update_stars()
        self._fertig()

    def _fertig(self) -> None:
        self._update_summary()
        self.changed.emit()

    # -- Anzeige -------------------------------------------------------

    def _update_stars(self) -> None:
        for index, knopf in enumerate(self._stars, start=1):
            gefuellt = index <= self._min_rating
            knopf.setText(STERN if gefuellt else LEER)
            knopf.setChecked(gefuellt)

    def _update_summary(self) -> None:
        teile = []
        if self._min_rating:
            teile.append(f"ab {self._min_rating}\u2605")
        farben = [marks.label_text(i, self._label_set)
                  for i in sorted(self._colors)]
        if self._unlabeled:
            farben.append("ohne")
        if farben:
            teile.append(" oder ".join(farben))
        if not self._show_rejects:
            teile.append("ohne Abgelehnte")
        self._summary.setText("  ·  ".join(teile))


_STERN_STIL = f"""
QPushButton {{
    background: transparent;
    border: none;
    color: {theme.TEXT_MUTED};
    font-size: 20px;
    padding: 0;
}}
QPushButton:hover {{ color: {theme.STAR}; }}
QPushButton:checked {{ color: {theme.STAR}; }}
"""


def _farb_stil(farbe: str) -> str:
    return f"""
QPushButton {{
    background: {farbe};
    border: 2px solid {theme.BORDER};
    border-radius: 12px;
}}
QPushButton:hover {{ border-color: {theme.TEXT_MUTED}; }}
QPushButton:checked {{ border: 3px solid #ffffff; }}
"""
