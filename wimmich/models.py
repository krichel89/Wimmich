"""Modell und Zeichenroutine für die Rasteransicht."""

from __future__ import annotations

from typing import ClassVar

from PyQt6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QPointF, QRect, QRectF, QRunnable,
    QSize, Qt, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from pathlib import Path

from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

import time

from . import marks, previews, remote_thumbs, theme, thumbs

REMOTE_PAUSE = 5.0     # Sekunden Ruhe, bevor eine Serverkachel neu versucht wird

ROLE_PATH = Qt.ItemDataRole.UserRole + 1
ROLE_ID = Qt.ItemDataRole.UserRole + 2
ROLE_RATING = Qt.ItemDataRole.UserRole + 3
ROLE_ROW = Qt.ItemDataRole.UserRole + 4
ROLE_STACK = Qt.ItemDataRole.UserRole + 5
ROLE_LABEL = Qt.ItemDataRole.UserRole + 6
ROLE_EDITED = Qt.ItemDataRole.UserRole + 7
ROLE_HEADER = Qt.ItemDataRole.UserRole + 8
ROLE_REMOTE = Qt.ItemDataRole.UserRole + 9   # liegt nur auf dem Server
ROLE_ASPECT = Qt.ItemDataRole.UserRole + 10  # Breite/Hoehe der Aufnahme

MONATE = {
    "01": "Januar", "02": "Februar", "03": "März", "04": "April",
    "05": "Mai", "06": "Juni", "07": "Juli", "08": "August",
    "09": "September", "10": "Oktober", "11": "November", "12": "Dezember",
}

WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
              "Samstag", "Sonntag")


def tagestitel(taken: str) -> str:
    """„Donnerstag, 13. August 2026" aus „2026-08-13 …".

    Bewusst ohne locale: die Namen sind hier fest deutsch, sonst haengt
    die Ueberschrift von den Spracheinstellungen des Rechners ab.
    """
    jahr, monat, tag = taken[:4], taken[5:7], taken[8:10]
    name = ""
    try:
        from datetime import date
        name = WOCHENTAGE[date(int(jahr), int(monat), int(tag)).weekday()] + ", "
    except (ValueError, IndexError):
        name = ""
    return f"{name}{int(tag)}. {MONATE.get(monat, monat)} {jahr}"


class _ThumbSignals(QObject):
    done = pyqtSignal(int, str)   # Zeilennummer, Cache-Pfad
    fail = pyqtSignal(int)


class _ThumbTask(QRunnable):
    def __init__(self, row: int, path: str, mtime: float, filesize: int,
                 edge: int, exiftool, signals: _ThumbSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._args = (row, path, mtime, filesize, edge, exiftool)
        self._signals = signals

    def run(self) -> None:
        row, path, mtime, filesize, edge, exiftool = self._args
        try:
            result = thumbs.get_thumbnail(path, mtime, filesize, edge, exiftool)
        except BaseException:
            # Eine Ausnahme in einem Pool-Faden erreicht sys.excepthook
            # NICHT - sie verschwindet spurlos, und die Kachel bleibt fuer
            # immer grau. Genau deshalb hier protokollieren.
            from . import crashlog
            crashlog.protokolliere(f"Vorschau fehlgeschlagen: {path}")
            self._signals.fail.emit(row)
            return
        if result is None:
            self._signals.fail.emit(row)
        else:
            self._signals.done.emit(row, str(result))


class _RemoteThumbTask(QRunnable):
    """Holt die Vorschau eines Bildes, das nur auf dem Server liegt.

    Laeuft im selben Pool wie die lokalen Kacheln. Der Cache in
    remote_thumbs sorgt dafuer, dass der Server jede Vorschau nur EINMAL
    liefern muss.
    """

    def __init__(self, row: int, immich_id: str, client, signals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._args = (row, immich_id, client)
        self._signals = signals

    def run(self) -> None:
        row, immich_id, client = self._args
        try:
            daten = remote_thumbs.fetch(client, immich_id)
        except Exception:            # Netzfehler darf keine Kachel killen
            daten = None
        if daten:
            self._signals.done.emit(row, str(remote_thumbs.cache_path(immich_id)))
        else:
            self._signals.fail.emit(row)


class PhotoModel(QAbstractListModel):
    """Haelt die Ergebnisliste einer Abfrage und lädt Vorschauen nach."""

    # Bleibt eine Server-Vorschau aus, soll das SICHTBAR werden statt
    # nur eine graue Kachel zu hinterlassen.
    serverfehler = pyqtSignal(str)
    # Die Masse einer Aufnahme wurden erst mit der Vorschau bekannt -
    # die Kachelbreite im dichten Raster haengt daran, also muss die
    # Anordnung neu gerechnet werden.
    masse_bekannt = pyqtSignal(QModelIndex)

    def __init__(self, exiftool=None, thumb_edge: int = 256, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._pixmaps: dict[int, QPixmap] = {}
        self._requested: set[int] = set()
        self._thumb_edge = thumb_edge
        self._immich_client = None
        self._remote_fehler: dict[int, float] = {}
        self._exiftool = exiftool

        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.done.connect(self._thumb_ready)
        self._signals.fail.connect(self._thumb_failed)
        self._serverfehler_gemeldet = ""

        self._placeholder = QPixmap()   # leer = Delegate zeichnet Platzhalter
        self._edited: set[str] = set()
        self._source = None
        self._group_by: str | None = None
        self._streaming = False
        self._last_group = None
        self._erschoepft = True
        self._zusatz: list[dict] = []
        self._sortschluessel = None
        self._desc = False

    # -- Daten ---------------------------------------------------------

    CHUNK = 300      # so viele Zeilen je Nachschub

    def set_rows(self, rows, group_by: str | None = None) -> None:
        """Eine fertige Liste anzeigen - für Suche, Alben, Personen."""
        self._start(iter(rows), group_by, streaming=False)

    def set_cursor(self, cursor, group_by: str | None = None,
                   zusatz: list[dict] | None = None,
                   sortschluessel=None, desc: bool = False) -> None:
        """Aus einem Datenbank-Cursor stückweise nachladen.

        Das ist das endlose Scrollen: es wird nur geholt, was die Ansicht
        gerade braucht. Bei 50.000 Bildern steht das erste Stück nach
        wenigen Millisekunden, statt dass die Oberfläche zwei Sekunden
        steht.

        `zusatz` sind Zeilen, die NICHT aus dem Cursor kommen - die
        Bilder, die nur auf dem Server liegen. Sie sind bereits sortiert
        und werden beim Nachladen an der richtigen Stelle eingefädelt,
        damit sie nicht als Klumpen am Ende hängen.
        """
        self._start(cursor, group_by, streaming=True,
                    zusatz=zusatz, sortschluessel=sortschluessel, desc=desc)

    def _start(self, quelle, group_by: str | None, streaming: bool,
               zusatz: list[dict] | None = None,
               sortschluessel=None, desc: bool = False) -> None:
        self.beginResetModel()
        self._rows = []
        self._pixmaps.clear()
        self._requested.clear()
        self._source = quelle
        self._group_by = group_by
        self._streaming = streaming
        self._last_group = None
        self._erschoepft = False
        self._zusatz = list(zusatz or [])
        self._sortschluessel = sortschluessel
        self._desc = bool(desc)
        self.endResetModel()
        # Ein erstes Stück sofort, damit die Ansicht nicht leer bleibt
        self._nachladen(self.CHUNK if streaming else None)

    # -- Nachschub ------------------------------------------------------

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and not self._erschoepft

    def fetchMore(self, parent=QModelIndex()) -> None:
        if not parent.isValid():
            self._nachladen(self.CHUNK)

    def _nachladen(self, anzahl: int | None) -> None:
        """Holt das nächste Stück und setzt Kopfzeilen bei Gruppenwechsel."""
        if self._erschoepft or self._source is None:
            return

        if self._streaming:
            roh = (self._source.fetchall() if anzahl is None
                   else self._source.fetchmany(anzahl))
        else:
            import itertools
            roh = (list(self._source) if anzahl is None
                   else list(itertools.islice(self._source, anzahl)))
        if not roh or (anzahl is not None and len(roh) < anzahl):
            self._erschoepft = True

        posten = [dict(z) for z in roh]
        if self._zusatz:
            posten = self._einfaedeln(posten)
        if not posten:
            return

        neue: list[dict] = []
        for item in posten:
            titel, zusatz = self._gruppe(item)
            if self._group_by and titel != self._last_group:
                self._last_group = titel
                neue.append({"_header": titel, "_header_detail": zusatz,
                             "filename": "", "path": "", "id": -1})
            neue.append(item)

        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(neue) - 1)
        self._rows.extend(neue)
        self.endInsertRows()

    def _einfaedeln(self, posten: list[dict]) -> list[dict]:
        """Zusatzzeilen an der Stelle einsortieren, an die sie gehören.

        Der Cursor liefert sortiert, die Zusatzliste ist sortiert - also
        wird verschmolzen wie bei zwei sortierten Stapeln. Alles, was vor
        dem letzten Posten dieses Stücks liegt, kommt jetzt dran; der
        Rest wartet auf das nächste Stück. Ist der Cursor erschöpft,
        kommt der ganze Rest hinterher.
        """
        if self._sortschluessel is None:
            if self._erschoepft:
                posten = posten + self._zusatz
                self._zusatz = []
            return posten

        def vor(a, b) -> bool:
            return a > b if self._desc else a < b

        ergebnis: list[dict] = []
        for item in posten:
            schluessel = self._sortschluessel(item)
            while self._zusatz and vor(self._sortschluessel(self._zusatz[0]),
                                       schluessel):
                ergebnis.append(self._zusatz.pop(0))
            ergebnis.append(item)
        if self._erschoepft and self._zusatz:
            ergebnis.extend(self._zusatz)
            self._zusatz = []
        return ergebnis

    def _gruppe(self, item: dict) -> tuple[str, str]:
        """Überschrift und Zusatz, unter denen eine Aufnahme einsortiert wird."""
        if self._group_by == "folder":
            pfad = item.get("folder") or ""
            return (Path(pfad).name or pfad), pfad
        if self._group_by == "date":
            taken = item.get("taken_at") or ""
            if len(taken) >= 7:
                return f"{MONATE.get(taken[5:7], taken[5:7])} {taken[:4]}", ""
            return "ohne Aufnahmedatum", ""
        if self._group_by == "day":
            taken = item.get("taken_at") or ""
            if len(taken) >= 10:
                return tagestitel(taken), ""
            return "ohne Aufnahmedatum", ""
        return "", ""

    def photo_rows(self) -> list[int]:
        """Zeilennummern, die wirklich Bilder sind (ohne Kopfzeilen)."""
        return [i for i, r in enumerate(self._rows) if "_header" not in r]

    def is_header(self, row: int) -> bool:
        item = self.row_data(row)
        return bool(item and "_header" in item)

    def alles_laden(self) -> None:
        """Rest nachziehen - nötig, wenn über das Ende hinaus geblättert wird."""
        while not self._erschoepft:
            self._nachladen(self.CHUNK * 4)

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._rows):
            return None
        item = self._rows[row]

        if "_header" in item:
            if role == ROLE_HEADER:
                return item["_header"]
            if role == ROLE_ROW:
                return item
            if role == Qt.ItemDataRole.DisplayRole:
                return item["_header"]
            return None

        if role == ROLE_HEADER:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return item["filename"]
        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmap_for(row, item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _tooltip(item)
        if role == ROLE_REMOTE:
            return bool(item.get("_remote"))
        if role == ROLE_ASPECT:
            # Seitenverhaeltnis fuer die Kachelbreite im dichten Raster.
            # Die Ausrichtung ist in width/height schon eingerechnet
            # (exif.read_fast dreht 90°-Aufnahmen), deshalb reicht die
            # rohe Rechnung.
            breite = item.get("width") or 0
            hoehe = item.get("height") or 0
            try:
                return float(breite) / float(hoehe) if breite and hoehe else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return 0.0
        if role == ROLE_PATH:
            return item.get("path") or ""
        if role == ROLE_ID:
            return item["id"]
        if role == ROLE_RATING:
            return item.get("rating") or 0
        if role == ROLE_STACK:
            return _stack_label(item)
        if role == ROLE_LABEL:
            return marks.label_color(item.get("label"))
        if role == ROLE_EDITED:
            return (item.get("thumb_path") or item["path"]) in self._edited
        if role == ROLE_ROW:
            return item
        return None

    def flags(self, index):
        """Kopfzeilen sind nicht auswählbar."""
        base = super().flags(index)
        if index.isValid() and self.is_header(index.row()):
            return Qt.ItemFlag.NoItemFlags
        return base

    def update_rating(self, row: int, rating: int) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row]["rating"] = rating
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [ROLE_RATING, Qt.ItemDataRole.DisplayRole])

    def set_edited(self, paths: set[str]) -> None:
        """Pfade mit Retusche - für das Abzeichen auf der Kachel."""
        if paths != self._edited:
            self._edited = set(paths)
            if self._rows:
                self.dataChanged.emit(
                    self.index(0, 0), self.index(len(self._rows) - 1, 0),
                    [ROLE_EDITED],
                )

    def update_label(self, row: int, label: str) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row]["label"] = label
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [ROLE_LABEL, Qt.ItemDataRole.DisplayRole])

    # -- Vorschauen ----------------------------------------------------

    def _pixmap_for(self, row: int, item: dict) -> QPixmap:
        pixmap = self._pixmaps.get(row)
        if pixmap is not None:
            return pixmap
        if row not in self._requested:
            self._requested.add(row)
            if item.get("_remote"):
                letzter = self._remote_fehler.get(row)
                if letzter is not None and time.monotonic() - letzter < REMOTE_PAUSE:
                    self._requested.discard(row)   # spaeter noch einmal
                    return self._placeholder
                self._pool.start(_RemoteThumbTask(
                    row, item["immich_id"], self._immich_client, self._signals))
                return self._placeholder
            # thumb_path zeigt bei Stapeln auf das JPEG - das spart das
            # Entwickeln der RAW-Datei für die Kachel
            self._pool.start(_ThumbTask(
                row,
                item.get("thumb_path") or item["path"],
                item.get("thumb_mtime") or item.get("mtime") or 0.0,
                item.get("thumb_size") or item.get("filesize") or 0,
                self._thumb_edge, self._exiftool, self._signals,
            ))
        return self._placeholder

    def _thumb_ready(self, row: int, cache_file: str) -> None:
        pixmap = previews.pixmap_aus_datei(cache_file)
        self._pixmaps[row] = pixmap
        self._masse_nachtragen(row, pixmap)
        self._touch(row)

    def _masse_nachtragen(self, row: int, pixmap: QPixmap) -> None:
        """Fehlende Bildmasse aus der Vorschau uebernehmen.

        Im dichten Raster bestimmt das Seitenverhaeltnis die Kachelbreite.
        Steht es nicht im Index - RAW-Dateien gehen ueber exiftool, aeltere
        Eintraege haben es womoeglich gar nicht -, waere JEDE Kachel
        quadratisch, und ein Hochformat bekaeme wieder Luft links und
        rechts. Die Vorschau kennt die Masse aber; sie werden hier
        nachgetragen und die Anordnung neu angestossen.
        """
        if not (0 <= row < len(self._rows)):
            return
        item = self._rows[row]
        if item.get("width") and item.get("height"):
            return
        if pixmap is None or pixmap.isNull():
            return
        if not pixmap.width() or not pixmap.height():
            return
        item["width"], item["height"] = pixmap.width(), pixmap.height()
        self.masse_bekannt.emit(self.index(row, 0))

    def kachel_bild(self, row: int):
        """Die bereits geladene Kachel als QImage - oder None.

        Wird in der Lupe als letzter Rueckfall benutzt: was im Raster zu
        sehen ist, kann auch gross gezeigt werden, statt „keine Vorschau"
        ueber ein vorhandenes Bild zu schreiben.
        """
        pixmap = self._pixmaps.get(row)
        if pixmap is None or pixmap.isNull() or pixmap is self._placeholder:
            return None
        return pixmap.toImage()

    def _thumb_failed(self, row: int) -> None:
        item = self._rows[row] if 0 <= row < len(self._rows) else {}
        if item.get("_remote"):
            # Server-Vorschauen NICHT dauerhaft aufgeben: der Server kann
            # beim Start noch nicht erreichbar gewesen sein oder die
            # Verbindung kurz gehangen haben. Zeile wieder freigeben,
            # damit ein spaeterer Anlauf (Scrollen, Ansicht wechseln,
            # nach dem Abgleich) es erneut versucht.
            self._requested.discard(row)
            self._remote_fehler[row] = time.monotonic()
            self._pixmaps.pop(row, None)
            grund = remote_thumbs.letzte_meldung()
            if grund and grund != self._serverfehler_gemeldet:
                self._serverfehler_gemeldet = grund
                self.serverfehler.emit(grund)
            self._touch(row)
            return
        self._pixmaps[row] = self._placeholder
        self._touch(row)

    def kachel_stand(self) -> tuple[int, int, int]:
        """(Bildzeilen, Vorschau vorhanden, Vorschau leer geblieben)."""
        zeilen = self.photo_rows()
        vorhanden = leer = 0
        for r in zeilen:
            pixmap = self._pixmaps.get(r)
            if pixmap is None:
                continue
            if pixmap.isNull():
                leer += 1
            else:
                vorhanden += 1
        return len(zeilen), vorhanden, leer

    def _touch(self, row: int) -> None:
        if row < len(self._rows):
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


class PhotoDelegate(QStyledItemDelegate):
    """Zeichnet Kachel, Dateiname, Sterne und Stapelabzeichen."""

    # Was auf der Kachel steht - alles einzeln abschaltbar (Menue Ansicht).
    # Grenzen der Kachelbreite im dichten Raster (Vielfache der Hoehe)
    MIN_VERHAELTNIS, MAX_VERHAELTNIS = 0.55, 2.2

    VORGABEN: ClassVar[dict] = {
        "packed": True,      # dicht an dicht, ohne Beschriftungsband
        "filenames": False,  # Dateiname unter der Kachel
        "stars": True,       # Sterne (im dichten Raster auf dem Bild)
        "labels": True,      # Farbmarkierung
        "stack": True,       # RAW+JPG-Abzeichen
    }

    def __init__(self, tile: int = 180, optionen: dict | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.tile = tile
        self.optionen = dict(self.VORGABEN)
        if optionen:
            self.optionen.update(optionen)
        # Dicht an dicht ist keine Wahl mehr, sondern der Bauplan:
        # gleich hohe Kachel, Breite nach Seitenverhaeltnis, eckig.
        # Solange es ein Schalter war, stand er bei Harald aus - mit
        # dem Ergebnis, das er zweimal gemeldet hat (gleich grosse,
        # abgerundete Kacheln mit Luft daneben).
        self.optionen["packed"] = True
        self._masse()

    def _masse(self) -> None:
        """Raender und Beschriftungsband aus den Optionen ableiten."""
        packed = self.optionen["packed"]
        self.pad = 2 if packed else 8
        band = 0
        if self.optionen["filenames"]:
            band += 20 if packed else 24
        if not packed and self.optionen["stars"]:
            band += 18          # klassisch: Sterne unter dem Bild
        self.label_height = band

    def setze(self, name: str, wert: bool) -> None:
        if name == "packed":
            return          # nicht mehr abschaltbar, siehe __init__
        self.optionen[name] = bool(wert)
        self._masse()

    def sizeHint(self, option, index) -> QSize:
        if index is not None and index.isValid() and index.data(ROLE_HEADER):
            # Volle Breite, damit die Kopfzeile eine eigene Reihe bekommt
            width = self._viewport_width(option)
            return QSize(width, 40)
        if not self.optionen["packed"]:
            return QSize(self.tile + self.pad * 2,
                         self.tile + self.label_height + self.pad)

        # Dicht an dicht: ALLE Kacheln gleich hoch, die Breite folgt dem
        # Seitenverhaeltnis. Ein Hochformat bekommt dadurch eine schmale
        # Kachel statt einer quadratischen mit Luft links und rechts.
        return QSize(self._breite(index) + self.pad * 2,
                     self.tile + self.label_height + self.pad)

    def _breite(self, index) -> int:
        """Kachelbreite aus dem Seitenverhaeltnis, gedeckelt.

        Ohne Deckel wuerde ein Panorama eine ganze Zeile fuellen und ein
        extremes Hochformat zum Strich schrumpfen.
        """
        try:
            verhaeltnis = float(index.data(ROLE_ASPECT) or 0.0)
        except (TypeError, ValueError):
            verhaeltnis = 0.0
        if verhaeltnis <= 0:
            verhaeltnis = 1.0     # ohne Massangaben quadratisch
        verhaeltnis = min(max(verhaeltnis, self.MIN_VERHAELTNIS),
                          self.MAX_VERHAELTNIS)
        return round(self.tile * verhaeltnis)

    @staticmethod
    def _viewport_width(option) -> int:
        widget = getattr(option, "widget", None)
        if widget is not None and widget.viewport() is not None:
            return max(200, widget.viewport().width() - 8)
        return 900

    def paint(self, painter: QPainter, option, index) -> None:
        if index.data(ROLE_HEADER) is not None:
            self._paint_header(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = option.rect
        packed = self.optionen["packed"]
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        rand = 1 if packed else 3
        card = QRectF(rect.adjusted(rand, rand, -rand, -rand))
        if selected or hovered:
            farbe = QBrush(QColor(theme.ACCENT_DIM if selected else theme.HOVER))
            if packed:
                # Dicht an dicht: eckig. Abgerundete Ecken lassen zwischen
                # den Kacheln ueberall Hintergrund durchblitzen.
                painter.fillRect(card, farbe)
            else:
                path = QPainterPath()
                path.addRoundedRect(card, theme.RADIUS + 2, theme.RADIUS + 2)
                painter.fillPath(path, farbe)

        innen = 1 if packed else 5
        breite = (self._breite(index) - 2 * (innen - 1)) if packed else self.tile
        image_rect = QRectF(card.x() + innen, card.y() + innen,
                            max(8.0, float(breite)), self.tile)
        rejected = marks.is_reject(int(index.data(ROLE_RATING) or 0))
        if rejected:
            painter.setOpacity(0.38)
        self._draw_image(painter, image_rect, index)
        painter.setOpacity(1.0)

        if selected:
            # Deutlicher Rahmen UM das Bild. Die blasse Fuellung dahinter
            # allein war bei dicht liegenden Kacheln kaum zu sehen -
            # zwischen zwei ausgewaehlten Bildern gab es gar keine
            # sichtbare Grenze mehr.
            stift = QPen(QColor(theme.ACCENT), 3)
            stift.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(stift)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(card.adjusted(1.5, 1.5, -1.5, -1.5))

        if not self.label_height:
            painter.restore()
            return

        painter.setPen(QPen(QColor("#ffffff" if selected else theme.TEXT_MUTED)))
        font = QFont(option.font)
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1.8))
        painter.setFont(font)

        unten = int(image_rect.bottom()) + (2 if packed else 5)
        if self.optionen["filenames"]:
            name_rect = QRect(int(card.x()) + 4, unten,
                              int(card.width()) - 8, 18)
            elided = painter.fontMetrics().elidedText(
                str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                Qt.TextElideMode.ElideMiddle, name_rect.width(),
            )
            painter.drawText(name_rect, int(Qt.AlignmentFlag.AlignHCenter), elided)
            unten = name_rect.bottom() + 1

        # Klassische Ansicht: Sterne unter dem Bild. Im dichten Raster
        # sitzen sie auf dem Bild (siehe _draw_stars).
        if not packed and self.optionen["stars"]:
            rating = int(index.data(ROLE_RATING) or 0)
            mark_rect = QRect(int(card.x()) + 4, unten, int(card.width()) - 8, 17)
            if marks.is_reject(rating):
                painter.setPen(QPen(QColor("#ff6b6b")))
                painter.drawText(
                    mark_rect,
                    int(Qt.AlignmentFlag.AlignHCenter
                        | Qt.AlignmentFlag.AlignVCenter),
                    "\u2715",
                )
            elif rating > 0:
                painter.setPen(QPen(QColor(theme.STAR)))
                painter.drawText(
                    mark_rect,
                    int(Qt.AlignmentFlag.AlignHCenter
                        | Qt.AlignmentFlag.AlignVCenter),
                    "\u2605" * rating,
                )
        painter.restore()

    def _paint_header(self, painter: QPainter, option, index) -> None:
        """Trennzeile zwischen den Gruppen - Ordnername oder Monat."""
        painter.save()
        rect = option.rect
        item = index.data(ROLE_ROW) or {}
        titel = str(index.data(ROLE_HEADER) or "")
        zusatz = str(item.get("_header_detail") or "")

        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawText(
            rect.adjusted(8, 8, -8, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            titel,
        )

        if zusatz and zusatz != titel:
            metrics = painter.fontMetrics()
            start = rect.x() + 16 + metrics.horizontalAdvance(titel)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                QRect(start, rect.y() + 8,
                      max(0, rect.width() - start), rect.height() - 8),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                zusatz,
            )

        painter.setPen(QPen(QColor(theme.BORDER)))
        line_y = rect.bottom() - 3
        painter.drawLine(rect.x() + 8, line_y, rect.right() - 8, line_y)
        painter.restore()

    def _draw_image(self, painter: QPainter, area: QRectF, index) -> None:
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)

        radius = 0.0 if self.optionen["packed"] else float(theme.RADIUS)

        if not isinstance(pixmap, QPixmap) or pixmap.isNull():
            # Ruhiger Platzhalter, solange die Vorschau noch entsteht
            path = QPainterPath()
            path.addRoundedRect(area, radius, radius)
            painter.fillPath(path, QBrush(QColor(theme.ELEVATED)))
            return

        scaled = pixmap.scaled(
            area.size().toSize(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        target = QRectF(0, 0, scaled.width(), scaled.height())
        target.moveCenter(area.center())

        painter.save()
        if radius:
            clip = QPainterPath()
            clip.addRoundedRect(target, radius, radius)
            painter.setClipPath(clip)
        painter.drawPixmap(target.toRect(), scaled)
        painter.restore()

        badge = index.data(ROLE_STACK)
        if badge and self.optionen["stack"]:
            self._draw_badge(painter, target, badge)

        if index.data(ROLE_REMOTE):
            # Wolke oben links: dieses Bild liegt NUR auf dem Server,
            # es gibt keine lokale Datei dazu.
            self._draw_remote(painter, target)

        colour = index.data(ROLE_LABEL)
        if colour and self.optionen["labels"]:
            self._draw_dot(painter, target, colour)

        if self.optionen["packed"] and self.optionen["stars"]:
            self._draw_stars(painter, target, int(index.data(ROLE_RATING) or 0))

        if index.data(ROLE_EDITED):
            self._draw_edited(painter, target)

    def _draw_remote(self, painter: QPainter, image_rect: QRectF) -> None:
        """Wolkenzeichen fuer Bilder, die nur auf dem Server liegen.

        Bewusst zurueckhaltend: kleiner als frueher, ohne schwarzen Hof
        und halbdurchsichtig. Es ist ein Hinweis, keine Auszeichnung -
        in einer Ansicht voller Serverbilder saesse sonst auf jeder
        Kachel ein Abzeichen.
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(0.55)
        mitte = QPointF(image_rect.left() + 12, image_rect.top() + 11)

        wolke = QPainterPath()
        wolke.addEllipse(QPointF(mitte.x() - 2.6, mitte.y() + 0.4), 3.0, 3.0)
        wolke.addEllipse(QPointF(mitte.x() + 0.4, mitte.y() - 1.5), 3.8, 3.8)
        wolke.addEllipse(QPointF(mitte.x() + 3.0, mitte.y() + 0.8), 2.9, 2.9)
        wolke.addRoundedRect(
            QRectF(mitte.x() - 4.6, mitte.y() + 0.8, 8.4, 3.4), 1.7, 1.7)
        # Dunkler Umriss statt Hof: die Wolke bleibt auch auf hellem
        # Himmel erkennbar, ohne einen Fleck auf das Bild zu setzen.
        painter.setPen(QPen(QColor(0, 0, 0, 130), 2.0))
        painter.drawPath(wolke)
        painter.fillPath(wolke, QBrush(QColor(255, 255, 255, 235)))
        painter.restore()

    def _draw_badge(self, painter: QPainter, image_rect: QRectF, text: str) -> None:
        """RAW+JPG oben rechts - klein und zurueckhaltend.

        Das Abzeichen soll die Aufnahme nicht zudecken: kleine Schrift,
        halbdurchsichtiger Grund, gedaempftes Weiss.
        """
        painter.save()
        font = QFont(painter.font())
        font.setPointSizeF(7.5)
        font.setBold(False)
        painter.setFont(font)

        width = painter.fontMetrics().horizontalAdvance(text) + 8
        badge = QRectF(image_rect.right() - width - 4, image_rect.top() + 4,
                       width, 13)
        path = QPainterPath()
        path.addRoundedRect(badge, 3, 3)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 110)))
        painter.setPen(QPen(QColor(255, 255, 255, 190)))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    def _draw_stars(self, painter: QPainter, image_rect: QRectF,
                    rating: int) -> None:
        """Bewertung unten links AUF dem Bild.

        Im dichten Raster gibt es kein Band unter der Kachel mehr. Damit
        die Sterne trotzdem auf hellen Aufnahmen lesbar bleiben, sitzen
        sie auf einem dunklen, halbdurchsichtigen Streifen.
        """
        if not rating:
            return
        text = "\u2715" if marks.is_reject(rating) else "\u2605" * rating
        painter.save()
        font = QFont(painter.font())
        font.setPointSizeF(8.0)
        painter.setFont(font)
        breite = painter.fontMetrics().horizontalAdvance(text) + 8
        feld = QRectF(image_rect.left() + 4, image_rect.bottom() - 17,
                      breite, 13)
        path = QPainterPath()
        path.addRoundedRect(feld, 3, 3)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 110)))
        painter.setPen(QPen(QColor("#ff6b6b") if marks.is_reject(rating)
                            else QColor(theme.STAR)))
        painter.drawText(feld, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    def _draw_dot(self, painter: QPainter, image_rect: QRectF, colour: str) -> None:
        """Farbmarkierung unten rechts.

        Kleiner als frueher (22 px waren im dichten Raster eine Plakette
        auf jedem zweiten Bild) und unten rechts, damit sie sich nicht
        mit dem Wolkenzeichen oben links stapelt. Heller Ring bleibt,
        sonst verschwindet Blau auf blauem Grund.
        """
        painter.save()
        size = 13.0
        dot = QRectF(image_rect.right() - size - 5,
                     image_rect.bottom() - size - 5, size, size)
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1.4))
        painter.setBrush(QBrush(QColor(colour)))
        painter.drawEllipse(dot)
        painter.restore()

    def _draw_edited(self, painter: QPainter, image_rect: QRectF) -> None:
        """Zeigt an, dass für dieses Bild Retuschen gespeichert sind."""
        painter.save()
        size = 14.0
        rechts = 24.0 if self.optionen["labels"] else 5.0
        badge = QRectF(image_rect.right() - size - rechts,
                       image_rect.bottom() - size - 4, size, size)
        path = QPainterPath()
        path.addRoundedRect(badge, 5, 5)
        painter.fillPath(path, QBrush(QColor(0, 0, 0, 170)))
        font = QFont(painter.font())
        font.setPointSizeF(9.5)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "\u270e")
        painter.restore()


def _stack_label(item: dict) -> str:
    """Abzeichen oben rechts auf der Kachel."""
    count = int(item.get("stack_count") or 1)
    if count > 1:
        return "RAW+JPG" if count == 2 else f"RAW+{count - 1}"
    return "RAW" if item.get("is_raw") else ""


def _tooltip(item: dict) -> str:
    lines = [item["path"]]
    count = int(item.get("stack_count") or 1)
    if count > 1:
        lines.append(f"Stapel aus {count} Dateien")
    if item.get("taken_at"):
        lines.append(f"Aufnahme: {item['taken_at']}")
    if item.get("camera"):
        lines.append(f"Kamera: {item['camera']}")
    if item.get("lens"):
        lines.append(f"Objektiv: {item['lens']}")
    if item.get("width") and item.get("height"):
        lines.append(f"{item['width']} x {item['height']} px")
    if item.get("keywords"):
        lines.append(f"Stichwörter: {item['keywords']}")
    if item.get("label"):
        lines.append(f"Markierung: {item['label']}")
    return "\n".join(lines)
