"""SQLite-Index für die lokale Bibliothek.

Der Index ist ein Cache, keine Datenhaltung: Wahrheit sind die Dateien auf
der Platte. Der Index darf jederzeit gelöscht und neu aufgebaut werden.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import DB_PATH
from .marks import REJECT, clamp_rating

SCHEMA_VERSION = 11

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    folder        TEXT    NOT NULL,
    filename      TEXT    NOT NULL,
    ext           TEXT,
    is_raw        INTEGER NOT NULL DEFAULT 0,
    stack_key     TEXT,
    filesize      INTEGER,
    mtime         REAL,
    width         INTEGER,
    height        INTEGER,
    taken_at      TEXT,
    camera        TEXT,
    lens          TEXT,
    color_temp    INTEGER,
    aperture      REAL,
    iso           INTEGER,
    focal_length  REAL,
    exposure_time TEXT,
    rating        INTEGER NOT NULL DEFAULT 0,
    label         TEXT,
    title         TEXT,
    caption       TEXT,
    keywords      TEXT,
    immich_id     TEXT,
    immich_seen   REAL,
    checksum      TEXT,
    meta_read     INTEGER NOT NULL DEFAULT 0,
    indexed_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_photos_folder   ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_taken    ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_rating   ON photos(rating);
CREATE INDEX IF NOT EXISTS idx_photos_metaread ON photos(meta_read);
-- Für die Stapelbildung: findet den Vertreter einer Aufnahme sofort,
-- statt die ganze Tabelle sortieren zu müssen.
CREATE INDEX IF NOT EXISTS idx_photos_pick     ON photos(stack_key, is_raw, filename);
CREATE INDEX IF NOT EXISTS idx_photos_folder_taken ON photos(folder, taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_immich   ON photos(immich_id);

-- Alben und Personen kommen vom Server und sind hier nur gespiegelt.
-- Sie dürfen jederzeit gelöscht und neu geholt werden.
CREATE TABLE IF NOT EXISTS remote_assets (
    immich_id   TEXT PRIMARY KEY,
    filename    TEXT,
    taken_at    TEXT,
    checksum    TEXT,
    kind        TEXT,
    width       INTEGER,
    height      INTEGER,
    seen        REAL
);
CREATE INDEX IF NOT EXISTS idx_remote_taken ON remote_assets(taken_at);

CREATE TABLE IF NOT EXISTS albums (
    id          TEXT PRIMARY KEY,   -- intern; bei Serveralben = immich_id
    name        TEXT NOT NULL,
    asset_count INTEGER NOT NULL DEFAULT 0,
    synced_at   REAL,
    immich_id   TEXT NOT NULL DEFAULT '',  -- leer = noch nicht auf dem Server
    local       INTEGER NOT NULL DEFAULT 0,  -- in Wimmich angelegt
    dirty       INTEGER NOT NULL DEFAULT 0   -- lokal geaendert, noch nicht geschoben
);
CREATE TABLE IF NOT EXISTS people (
    id        TEXT PRIMARY KEY,
    name      TEXT,
    hidden    INTEGER NOT NULL DEFAULT 0,
    synced_at REAL
);
CREATE TABLE IF NOT EXISTS album_assets (
    album_id  TEXT NOT NULL,
    immich_id TEXT NOT NULL,
    PRIMARY KEY (album_id, immich_id)
);
CREATE TABLE IF NOT EXISTS person_assets (
    person_id TEXT NOT NULL,
    immich_id TEXT NOT NULL,
    PRIMARY KEY (person_id, immich_id)
);
-- Lokale Albenzugehoerigkeit. album_assets ist der SPIEGEL des Servers
-- und wird bei jedem Abgleich ueberschrieben; hier steht, was Harald in
-- Wimmich selbst zugeordnet hat. Eine Zeile mit entfernt=1 ist ein
-- Grabstein: das Bild wurde aus dem Album genommen und muss beim
-- naechsten Abgleich auch auf dem Server heraus.
-- Lokale Datei: path gesetzt, immich_id leer. Reines Serverbild:
-- umgekehrt. So bleibt der Schluessel eindeutig.
CREATE TABLE IF NOT EXISTS album_photos (
    album_id  TEXT NOT NULL,
    path      TEXT NOT NULL DEFAULT '',
    immich_id TEXT NOT NULL DEFAULT '',
    entfernt  INTEGER NOT NULL DEFAULT 0,
    added     REAL,
    PRIMARY KEY (album_id, path, immich_id)
);
CREATE INDEX IF NOT EXISTS idx_album_photos_path ON album_photos(path);
CREATE INDEX IF NOT EXISTS idx_album_assets_asset  ON album_assets(immich_id);
CREATE INDEX IF NOT EXISTS idx_person_assets_asset ON person_assets(immich_id);

-- Retuschen: nichtdestruktiv, je Datei eine Schrittfolge als JSON.
-- Am Pfad festgemacht, nicht an der Zeilennummer - so überlebt eine
-- Retusche das Löschen und Neuaufbauen des Index.
CREATE TABLE IF NOT EXISTS edits (
    path       TEXT PRIMARY KEY,
    steps      TEXT NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Spalten, die in den Volltextindex gehen. Reihenfolge muss zu _FTS_INSERT passen.
_FTS_COLUMNS = ("filename", "folder", "camera", "lens", "title", "caption", "keywords")

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    filename, folder, camera, lens, title, caption, keywords,
    tokenize='unicode61'
);
"""


class Database:
    """Dünne Hülle um sqlite3 mit einer Verbindung pro Thread."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.has_fts = False
        # NULLS LAST gibt es seit SQLite 3.30. Ohne die Angabe wäre der
        # Ausdruck "taken_at IS NULL, taken_at ASC" nötig - und der
        # verhindert, dass SQLite den Index benutzt: 124 ms statt 2 ms
        # bis zur ersten Zeile. Deshalb wird die Fähigkeit einmal geprüft.
        self.has_nulls_last = _supports_nulls_last()
        self._init_schema()

    # -- Verbindung ----------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        conn = self.conn
        conn.executescript(_SCHEMA)
        self._migrate()
        try:
            conn.executescript(_FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            # FTS5 ist in dieser SQLite-Version nicht einkompiliert.
            # Suche fällt dann auf LIKE zurück (siehe search()).
            self.has_fts = False
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def _migrate(self) -> None:
        """Ergänzt Spalten, die in älteren Datenbanken fehlen.

        Bewusst nur additiv: der Index ist Cache, im Zweifel darf er neu
        aufgebaut werden. Es soll nur niemand nach einem Versionswechsel
        vor einer unbrauchbaren Datenbank stehen.
        """
        conn = self.conn
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}
        if "stack_key" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN stack_key TEXT")
        if "label" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN label TEXT")
        if "checksum" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN checksum TEXT")
        if "color_temp" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN color_temp INTEGER")
        if "aperture" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN aperture REAL")
        if "iso" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN iso INTEGER")
        if "focal_length" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN focal_length REAL")
        if "exposure_time" not in columns:
            conn.execute("ALTER TABLE photos ADD COLUMN exposure_time TEXT")
        # Schema 9: Bilder, die (noch) nur auf dem Server liegen
        conn.execute("""CREATE TABLE IF NOT EXISTS remote_assets (
            immich_id TEXT PRIMARY KEY, filename TEXT, taken_at TEXT,
            checksum TEXT, kind TEXT, width INTEGER, height INTEGER, seen REAL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_remote_taken "
                     "ON remote_assets(taken_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_immich ON photos(immich_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_pick "
                     "ON photos(stack_key, is_raw, filename)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_folder_taken "
                     "ON photos(folder, taken_at)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edits ("
            " path TEXT PRIMARY KEY, steps TEXT NOT NULL, updated_at REAL)"
        )
        # Erst jetzt, wenn die Spalte in jedem Fall existiert
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_stack ON photos(stack_key)"
        )
        # Schema 10: eigene Alben in Wimmich
        album_spalten = {row["name"]
                         for row in conn.execute("PRAGMA table_info(albums)")}
        if "immich_id" not in album_spalten:
            conn.execute("ALTER TABLE albums ADD COLUMN immich_id TEXT "
                         "NOT NULL DEFAULT ''")
            # Alles, was bisher drinstand, kam vom Server - dort ist die
            # Zeilenkennung zugleich die Immich-Kennung.
            conn.execute("UPDATE albums SET immich_id = id")
        if "local" not in album_spalten:
            conn.execute("ALTER TABLE albums ADD COLUMN local INTEGER "
                         "NOT NULL DEFAULT 0")
        if "dirty" not in album_spalten:
            conn.execute("ALTER TABLE albums ADD COLUMN dirty INTEGER "
                         "NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS album_photos (
            album_id TEXT NOT NULL, path TEXT NOT NULL DEFAULT '',
            immich_id TEXT NOT NULL DEFAULT '',
            entfernt INTEGER NOT NULL DEFAULT 0, added REAL,
            PRIMARY KEY (album_id, path, immich_id))""")
        # Index erst NACH dem Anlegen der Tabelle - sonst scheitert er
        # bei einer alten Datenbank (Lehre aus Schema 8)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_album_photos_path "
                     "ON album_photos(path)")

        # Schema 11: Indizes fuer die beiden uebrigen Sortierungen.
        # Gemessen an 60 000 Aufnahmen: „zuletzt geaendert" 49 ms ohne,
        # 0,4 ms mit Index; „Dateiname" 3,4 ms ohne, 0,3 ms mit.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_filename "
                     "ON photos(filename)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_mtime "
                     "ON photos(mtime)")

        missing = conn.execute(
            "SELECT id, folder, filename FROM photos WHERE stack_key IS NULL"
        ).fetchall()
        for row in missing:
            conn.execute(
                "UPDATE photos SET stack_key=? WHERE id=?",
                (stack_key_for(row["folder"], row["filename"]), row["id"]),
            )
        if missing:
            conn.commit()

    # -- Schreiben -----------------------------------------------------

    def upsert_file(self, path: str, folder: str, filename: str, ext: str,
                    is_raw: bool, filesize: int, mtime: float) -> tuple[int, bool]:
        """Legt einen Datensatz an oder aktualisiert ihn.

        Rückgabe: (id, needs_metadata). needs_metadata ist True, wenn die
        Datei neu ist oder sich seit dem letzten Lesen geändert hat.
        """
        conn = self.conn
        row = conn.execute(
            "SELECT id, mtime, filesize, meta_read FROM photos WHERE path = ?", (path,)
        ).fetchone()

        if row is None:
            cur = conn.execute(
                """INSERT INTO photos(path, folder, filename, ext, is_raw, stack_key,
                                      filesize, mtime, indexed_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (path, folder, filename, ext, int(is_raw),
                 stack_key_for(folder, filename), filesize, mtime, time.time()),
            )
            return int(cur.lastrowid), True

        changed = (row["mtime"] != mtime) or (row["filesize"] != filesize)
        if changed:
            conn.execute(
                """UPDATE photos SET filesize=?, mtime=?, meta_read=0, indexed_at=?
                   WHERE id=?""",
                (filesize, mtime, time.time(), row["id"]),
            )
        return int(row["id"]), changed or not row["meta_read"]

    def store_metadata(self, photo_id: int, meta: dict) -> None:
        """Schreibt ausgelesene Metadaten in den Index."""
        self.conn.execute(
            """UPDATE photos SET width=?, height=?, taken_at=?, camera=?, lens=?,
                                 color_temp=?, aperture=?, iso=?, focal_length=?,
                                 exposure_time=?, rating=?, label=?, title=?,
                                 caption=?, keywords=?, meta_read=1
               WHERE id=?""",
            (
                meta.get("width"), meta.get("height"), meta.get("taken_at"),
                meta.get("camera"), meta.get("lens"), meta.get("color_temp"),
                meta.get("aperture"), meta.get("iso"), meta.get("focal_length"),
                meta.get("exposure_time"),
                clamp_rating(meta.get("rating") or 0), meta.get("label"),
                meta.get("title"), meta.get("caption"), meta.get("keywords"),
                photo_id,
            ),
        )
        self._reindex_fts(photo_id)

    def set_rating(self, photo_id: int, rating: int) -> None:
        """Bewertung setzen. -1 bedeutet abgelehnt (Lightroom-Konvention)."""
        self.conn.execute(
            "UPDATE photos SET rating=? WHERE id=?",
            (clamp_rating(rating), photo_id),
        )
        self.conn.commit()

    def set_label(self, photo_id: int, label: str) -> None:
        self.conn.execute(
            "UPDATE photos SET label=? WHERE id=?", (label or None, photo_id)
        )
        self.conn.commit()

    def delete_missing(self, folder: str, keep_paths: set[str]) -> int:
        """Entfernt Einträge eines Ordners, deren Datei verschwunden ist."""
        conn = self.conn
        rows = conn.execute(
            "SELECT id, path FROM photos WHERE folder = ?", (folder,)
        ).fetchall()
        gone = [r["id"] for r in rows if r["path"] not in keep_paths]
        for pid in gone:
            if self.has_fts:
                self._delete_fts(pid)
            conn.execute("DELETE FROM photos WHERE id=?", (pid,))
        if gone:
            conn.commit()
        return len(gone)

    def delete_under(self, folder: str) -> int:
        """Entfernt alle Einträge eines Ordners samt Unterordnern.

        Wird beim Entfernen eines Bibliotheksordners gebraucht. Die
        DATEIEN bleiben unangetastet - hier verschwindet nur der Index.
        """
        conn = self.conn
        prefix = folder.rstrip("/\\") + os.sep
        rows = conn.execute(
            "SELECT id FROM photos WHERE folder = ? OR folder LIKE ? ESCAPE '\\'",
            (folder, _escape_like(prefix) + "%"),
        ).fetchall()
        for row in rows:
            if self.has_fts:
                self._delete_fts(row["id"])
            conn.execute("DELETE FROM photos WHERE id=?", (row["id"],))
        conn.commit()
        return len(rows)

    # -- Retuschen -----------------------------------------------------

    def save_edits(self, path: str, steps_json: str) -> None:
        """Schrittfolge sichern. Eine leere Folge löscht den Eintrag."""
        if not steps_json or steps_json in ("[]", "null"):
            self.conn.execute("DELETE FROM edits WHERE path=?", (path,))
        else:
            self.conn.execute(
                "INSERT INTO edits(path, steps, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET steps=excluded.steps, "
                "updated_at=excluded.updated_at",
                (path, steps_json, time.time()),
            )
        self.conn.commit()

    def load_edits(self, path: str) -> str | None:
        row = self.conn.execute(
            "SELECT steps FROM edits WHERE path=?", (path,)
        ).fetchone()
        return row["steps"] if row else None

    def edited_paths(self) -> set[str]:
        """Alle Pfade mit Retusche - für das Abzeichen auf der Kachel."""
        return {r["path"] for r in self.conn.execute("SELECT path FROM edits")}

    def commit(self) -> None:
        self.conn.commit()

    # -- Volltext ------------------------------------------------------

    def _delete_fts(self, photo_id: int) -> None:
        # Eigenständige FTS-Tabelle (kein content='photos'), daher normales DELETE.
        # Mit external content würde ein Löschen ohne vorherigen Eintrag den
        # Index beschädigen - deshalb bewusst die einfache Variante.
        self.conn.execute("DELETE FROM photos_fts WHERE rowid = ?", (photo_id,))

    def _reindex_fts(self, photo_id: int) -> None:
        if not self.has_fts:
            return
        self._delete_fts(photo_id)
        row = self.conn.execute(
            f"SELECT {', '.join(_FTS_COLUMNS)} FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        if row is None:
            return
        placeholders = ", ".join("?" for _ in _FTS_COLUMNS)
        self.conn.execute(
            f"INSERT INTO photos_fts(rowid, {', '.join(_FTS_COLUMNS)}) "
            f"VALUES(?, {placeholders})",
            [photo_id, *[row[c] or "" for c in _FTS_COLUMNS]],
        )

    # -- Lesen ---------------------------------------------------------

    def photos_in_folder(self, folder: str, recursive: bool = False,
                         min_rating: int = 0, order: str = "taken_at",
                         desc: bool = False,
                         stacked: bool = True, prefer_raw: bool = True,
                         show_rejects: bool = True,
                         labels: list[str] | None = None,
                         unlabeled: bool = False) -> list[sqlite3.Row]:
        if recursive:
            # Trennzeichen im Präfix, damit /Fotos/a nicht auch /Fotos/ab trifft
            prefix = folder.rstrip("/\\") + os.sep
            where = "(p.folder = ? OR p.folder LIKE ? ESCAPE '\\')"
            params: list = [folder, _escape_like(prefix) + "%"]
        else:
            where = "p.folder = ?"
            params = [folder]

        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._select(where, params, order, stacked, prefer_raw, desc=desc)

    def search(self, query: str, min_rating: int = 0, stacked: bool = True,
               prefer_raw: bool = True, show_rejects: bool = True,
               labels: list[str] | None = None, unlabeled: bool = False,
               limit: int = 5000) -> list[sqlite3.Row]:
        query = query.strip()
        if not query:
            return []

        if self.has_fts:
            try:
                where = ("p.id IN (SELECT rowid FROM photos_fts "
                         "WHERE photos_fts MATCH ?)")
                params: list = [_fts_query(query)]
                where, params = _add_filters(where, params, min_rating,
                                             show_rejects, labels, unlabeled)
                return self._select(where, params, "taken_at", stacked,
                                    prefer_raw, limit)
            except sqlite3.OperationalError:
                pass  # ungültige FTS-Syntax -> LIKE-Fallback

        like = f"%{_escape_like(query)}%"
        where = ("(p.filename LIKE ? ESCAPE '\\' OR p.folder LIKE ? ESCAPE '\\'"
                 " OR p.camera LIKE ? ESCAPE '\\' OR p.title LIKE ? ESCAPE '\\'"
                 " OR p.caption LIKE ? ESCAPE '\\' OR p.keywords LIKE ? ESCAPE '\\')")
        params = [like] * 6
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._select(where, params, "taken_at", stacked, prefer_raw, limit)

    def _select(self, where: str, params: list, order: str, stacked: bool,
                prefer_raw: bool, limit: int = 0,
                desc: bool = False) -> list[sqlite3.Row]:
        """Alles auf einmal - für kurze Listen (Suche, Alben, Personen)."""
        return self._cursor(where, params, order, stacked, prefer_raw,
                            limit, desc=desc).fetchall()

    def _cursor(self, where: str, params: list, order: str, stacked: bool,
                prefer_raw: bool, limit: int = 0, desc: bool = False):
        """Gemeinsamer Abfragebau. Liefert einen CURSOR, keine Liste.

        Damit kann der Aufrufer stückweise holen - das ist der Kern des
        endlosen Scrollens: bei 50.000 Bildern steht das erste Stück nach
        rund 7 ms bereit statt nach zweieinhalb Sekunden.

        Die Stapelbildung läuft bewusst über eine Unterabfrage und NICHT
        über Fensterfunktionen. Fensterfunktionen zwingen SQLite, erst
        das ganze Ergebnis aufzubauen; die Unterabfrage kann dem Index
        folgen und sofort die ersten Zeilen liefern. Gemessen: 750 ms
        gegen 11 ms bis zur ersten Zeile.

        desc dreht die gesamte Sortierung um (echte Sortierrichtung, per
        Klick auf den Richtungsknopf) - unabhängig davon, welche Spalte
        gewählt ist.
        """
        # Undatierte Aufnahmen ans Ende, ohne den Index auszuhebeln
        nach_datum = ("p.taken_at ASC NULLS LAST" if self.has_nulls_last
                      else "p.taken_at IS NULL, p.taken_at ASC")
        order_sql = {
            "taken_at": f"{nach_datum}, p.filename ASC",
            "filename": "p.filename ASC",
            "rating": f"p.rating DESC, {nach_datum}",
            "mtime": "p.mtime DESC",
            # Für die Ordneransicht: Ordner für Ordner, darin nach Datum
            "folder": f"p.folder ASC, {nach_datum}, p.filename ASC",
        }.get(order, f"{nach_datum}, p.filename ASC")
        if desc:
            order_sql = _reverse_order(order_sql)
        limit_sql = f" LIMIT {int(limit)}" if limit else ""

        if not stacked:
            return self.conn.execute(
                f"SELECT p.*, 1 AS stack_count, p.is_raw AS stack_has_raw, "
                f"p.path AS thumb_path, p.mtime AS thumb_mtime, "
                f"p.filesize AS thumb_size "
                f"FROM photos p WHERE {where} ORDER BY {order_sql}{limit_sql}",
                params,
            )

        # Welche Datei vertritt den Stapel? Bei Gleichstand der Dateiname,
        # damit die Auswahl zwischen zwei Durchläufen stabil bleibt.
        primary = "s.is_raw DESC" if prefer_raw else "s.is_raw ASC"
        # Die Vorschau kommt bevorzugt vom JPEG - ein NEF zu entwickeln
        # kostet ein Vielfaches.
        thumb = "ORDER BY s.is_raw ASC, s.filename LIMIT 1"
        return self.conn.execute(
            f"""SELECT p.*,
                  (SELECT COUNT(*) FROM photos s
                   WHERE s.stack_key = p.stack_key) AS stack_count,
                  (SELECT MAX(s.is_raw) FROM photos s
                   WHERE s.stack_key = p.stack_key) AS stack_has_raw,
                  COALESCE((SELECT s.path FROM photos s
                            WHERE s.stack_key = p.stack_key {thumb}),
                           p.path) AS thumb_path,
                  COALESCE((SELECT s.mtime FROM photos s
                            WHERE s.stack_key = p.stack_key {thumb}),
                           p.mtime) AS thumb_mtime,
                  COALESCE((SELECT s.filesize FROM photos s
                            WHERE s.stack_key = p.stack_key {thumb}),
                           p.filesize) AS thumb_size
                FROM photos p
                WHERE {where}
                  AND p.id = (SELECT s.id FROM photos s
                              WHERE s.stack_key = p.stack_key
                              ORDER BY {primary}, s.filename LIMIT 1)
                ORDER BY {order_sql}{limit_sql}""",
            params,
        )

    def all_photos_cursor(self, roots: list[str], min_rating: int = 0,
                          stacked: bool = True, prefer_raw: bool = True,
                          show_rejects: bool = True,
                          labels: list[str] | None = None,
                          unlabeled: bool = False,
                          order: str = "folder",
                          desc: bool = False):
        """Wie all_photos, liefert aber einen Cursor zum stückweisen Holen."""
        if not roots:
            return None
        where, params = _roots_where(roots)
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._cursor(where, params, order, stacked, prefer_raw, desc=desc)

    def count_photos(self, roots: list[str], min_rating: int = 0,
                     stacked: bool = True, show_rejects: bool = True,
                     labels: list[str] | None = None,
                     unlabeled: bool = False) -> int:
        """Anzahl vorab - für die Statuszeile, ohne alles zu laden."""
        if not roots:
            return 0
        where, params = _roots_where(roots)
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        spalte = "DISTINCT p.stack_key" if stacked else "*"
        row = self.conn.execute(
            f"SELECT COUNT({spalte}) FROM photos p WHERE {where}", params
        ).fetchone()
        return int(row[0] or 0)

    def stack_members(self, stack_key: str) -> list[sqlite3.Row]:
        """Alle Dateien eines Stapels - für Bewertungen, die alle betreffen."""
        if not stack_key:
            return []
        return self.conn.execute(
            "SELECT * FROM photos WHERE stack_key = ? ORDER BY is_raw DESC, filename",
            (stack_key,),
        ).fetchall()

    # -- Immich-Spiegel ------------------------------------------------

    def set_immich(self, photo_id: int, immich_id: str | None,
                   checksum: str | None = None) -> None:
        self.conn.execute(
            "UPDATE photos SET immich_id=?, immich_seen=?, checksum=COALESCE(?, checksum) "
            "WHERE id=?",
            (immich_id, time.time(), checksum, photo_id),
        )

    def set_checksum(self, photo_id: int, checksum: str) -> None:
        self.conn.execute("UPDATE photos SET checksum=? WHERE id=?",
                          (checksum, photo_id))

    def replace_albums(self, albums) -> None:
        """Albenliste des Servers übernehmen. Was dort weg ist, fliegt hier raus.

        EIGENE Alben bleiben dabei stehen: sie kennt der Server noch gar
        nicht (immich_id leer) oder sie gehören zu einer Zeile, deren
        Kennung anders lautet als die Immich-Kennung. Sonst hätte der
        erste Abgleich jedes selbst angelegte Album weggeräumt.
        """
        conn = self.conn
        keep = set()
        for album in albums:
            # Gibt es dazu schon eine Zeile - auch eine hier angelegte,
            # die inzwischen auf den Server geschoben wurde?
            row = conn.execute(
                "SELECT id, dirty FROM albums WHERE immich_id=? OR id=?",
                (album.id, album.id)).fetchone()
            if row:
                keep.add(row["id"])
                # Einen lokal geänderten Namen NICHT überschreiben - er
                # geht beim nächsten Abgleich auf den Server.
                if row["dirty"]:
                    conn.execute(
                        "UPDATE albums SET asset_count=?, synced_at=?, "
                        "immich_id=? WHERE id=?",
                        (album.asset_count, time.time(), album.id, row["id"]))
                else:
                    conn.execute(
                        "UPDATE albums SET name=?, asset_count=?, synced_at=?, "
                        "immich_id=? WHERE id=?",
                        (album.name, album.asset_count, time.time(),
                         album.id, row["id"]))
                continue
            keep.add(album.id)
            conn.execute(
                "INSERT INTO albums(id, name, asset_count, synced_at, "
                "immich_id, local, dirty) VALUES(?,?,?,?,?,0,0)",
                (album.id, album.name, album.asset_count, time.time(), album.id),
            )
        # Eigene Alben nie wegräumen, auch wenn der Server sie nicht kennt
        for row in conn.execute(
                "SELECT id FROM albums WHERE local=1 OR immich_id=''"):
            keep.add(row["id"])
        _prune(conn, "albums", "album_assets", "album_id", keep)
        conn.commit()

    def replace_people(self, people) -> None:
        conn = self.conn
        keep = set()
        for person in people:
            keep.add(person.id)
            conn.execute(
                "INSERT INTO people(id, name, hidden, synced_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "hidden=excluded.hidden, synced_at=excluded.synced_at",
                (person.id, person.name, int(person.hidden), time.time()),
            )
        _prune(conn, "people", "person_assets", "person_id", keep)
        conn.commit()

    def set_album_assets(self, album_id: str, immich_ids: list[str]) -> None:
        conn = self.conn
        conn.execute("DELETE FROM album_assets WHERE album_id=?", (album_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO album_assets(album_id, immich_id) VALUES(?,?)",
            [(album_id, i) for i in immich_ids],
        )
        conn.commit()

    def set_person_assets(self, person_id: str, immich_ids: list[str]) -> None:
        conn = self.conn
        conn.execute("DELETE FROM person_assets WHERE person_id=?", (person_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO person_assets(person_id, immich_id) VALUES(?,?)",
            [(person_id, i) for i in immich_ids],
        )
        conn.commit()


    # -- Bilder, die nur auf dem Server liegen --------------------------

    def upsert_remote(self, eintraege: list[dict], jetzt: float) -> int:
        """Server-Bilder in den Spiegel schreiben. Ergebnis: Anzahl."""
        if not eintraege:
            return 0
        self.conn.executemany(
            """INSERT INTO remote_assets
                   (immich_id, filename, taken_at, checksum, kind,
                    width, height, seen)
               VALUES (:immich_id, :filename, :taken_at, :checksum, :kind,
                       :width, :height, :seen)
               ON CONFLICT(immich_id) DO UPDATE SET
                   filename=excluded.filename, taken_at=excluded.taken_at,
                   checksum=excluded.checksum, kind=excluded.kind,
                   width=excluded.width, height=excluded.height,
                   seen=excluded.seen""",
            [dict(e, seen=jetzt) for e in eintraege],
        )
        return len(eintraege)

    def prune_remote(self, aelter_als: float) -> int:
        """Was der Server nicht mehr kennt, fliegt aus dem Spiegel."""
        cur = self.conn.execute(
            "DELETE FROM remote_assets WHERE seen IS NULL OR seen < ?",
            (aelter_als,))
        return cur.rowcount or 0

    def remote_only(self, order: str = "taken_at", desc: bool = True,
                    limit: int = 0) -> list[sqlite3.Row]:
        """Server-Bilder, zu denen es KEINE lokale Datei gibt.

        Bilder, die schon lokal liegen, werden ueber immich_id
        ausgeschlossen - sie erscheinen ja bereits als richtige Kachel.
        """
        richtung = "DESC" if desc else "ASC"
        spalte = "taken_at" if order == "taken_at" else "filename"
        grenze = f" LIMIT {int(limit)}" if limit else ""
        return self.conn.execute(
            f"""SELECT * FROM remote_assets r
                WHERE NOT EXISTS (SELECT 1 FROM photos p
                                  WHERE p.immich_id = r.immich_id
                                    AND p.immich_id <> '')
                ORDER BY {spalte} {richtung}{grenze}"""
        ).fetchall()

    def jahre(self, roots: list[str], min_rating: int = 0,
              stacked: bool = True, show_rejects: bool = True,
              labels: list[str] | None = None,
              unlabeled: bool = False) -> list[tuple[str, int]]:
        """(Jahr, Anzahl) fuer die Jahresleiste - aufsteigend.

        Laeuft ueber denselben Filtersatz wie die Ansicht, sonst zeigt
        die Leiste Jahre an, in denen gerade gar nichts steht.
        """
        if not roots:
            return []
        where, params = _roots_where(roots)
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        spalte = "DISTINCT p.stack_key" if stacked else "*"
        rows = self.conn.execute(
            f"""SELECT substr(p.taken_at, 1, 4) AS jahr, COUNT({spalte})
                FROM photos p
                WHERE {where} AND p.taken_at IS NOT NULL AND p.taken_at <> ''
                GROUP BY jahr ORDER BY jahr""",
            params,
        ).fetchall()
        return [(str(r[0]), int(r[1] or 0)) for r in rows if r[0]]

    def monate(self, roots: list[str], min_rating: int = 0,
               stacked: bool = True, show_rejects: bool = True,
               labels: list[str] | None = None,
               unlabeled: bool = False) -> list[tuple[str, int]]:
        """(Jahr-Monat, Anzahl) - derselbe Filtersatz wie jahre()."""
        if not roots:
            return []
        where, params = _roots_where(roots)
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        spalte = "DISTINCT p.stack_key" if stacked else "*"
        rows = self.conn.execute(
            f"""SELECT substr(p.taken_at, 1, 7) AS monat, COUNT({spalte})
                FROM photos p
                WHERE {where} AND p.taken_at IS NOT NULL AND p.taken_at <> ''
                GROUP BY monat ORDER BY monat""",
            params,
        ).fetchall()
        return [(str(r[0]), int(r[1] or 0)) for r in rows if r[0]]

    def remote_monate(self) -> list[tuple[str, int]]:
        """Dasselbe fuer die Bilder, die nur auf dem Server liegen."""
        rows = self.conn.execute(
            """SELECT substr(r.taken_at, 1, 7) AS monat, COUNT(*)
               FROM remote_assets r
               WHERE r.taken_at IS NOT NULL AND r.taken_at <> ''
                 AND NOT EXISTS (SELECT 1 FROM photos p
                                 WHERE p.immich_id = r.immich_id
                                   AND p.immich_id <> '')
               GROUP BY monat ORDER BY monat"""
        ).fetchall()
        return [(str(r[0]), int(r[1] or 0)) for r in rows if r[0]]

    def masse_pruefliste(self) -> list:
        """Alle Aufnahmen mit Pfad und eingetragener Groesse.

        Fuer den Reparaturlauf: er muss jeden Eintrag gegen die Datei
        halten koennen, auch die, die nie angesehen wurden.
        """
        return self.conn.execute(
            "SELECT path, is_raw, width, height FROM photos "
            "WHERE path IS NOT NULL AND path <> '' ORDER BY path"
        ).fetchall()

    def masse_berichtigen(self, path: str, width: int, height: int) -> None:
        """Nur Breite und Hoehe einer Aufnahme richtigstellen.

        Getrennt von store_metadata(), weil hier NICHTS anderes
        angefasst werden darf: Bewertung, Marke und Aufnahmedatum
        bleiben, wie sie sind.
        """
        self.conn.execute(
            "UPDATE photos SET width=?, height=? WHERE path=?",
            (int(width), int(height), path))

    def photos_by_paths(self, paths: list[str], order: str = "taken_at",
                        desc: bool = False, stacked: bool = True,
                        prefer_raw: bool = True, min_rating: int = 0,
                        show_rejects: bool = True,
                        labels: list[str] | None = None,
                        unlabeled: bool = False):
        """Genau diese Dateien, in der gewaehlten Sortierung.

        Fuer die vorlaeufige Sammlung: sie ist eine Liste von Pfaden,
        keine Ordner- oder Albenzugehoerigkeit.
        """
        if not paths:
            return []
        platz = ",".join("?" for _ in paths)
        where = f"p.path IN ({platz})"
        params: list = list(paths)
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._cursor(where, params, order, stacked, prefer_raw,
                            desc=desc).fetchall()

    def remote_by_ids(self, immich_ids: list[str]):
        """Serverspiegel-Zeilen zu genau diesen Kennungen."""
        if not immich_ids:
            return []
        platz = ",".join("?" for _ in immich_ids)
        return self.conn.execute(
            f"SELECT * FROM remote_assets WHERE immich_id IN ({platz})",
            list(immich_ids)).fetchall()

    def remote_jahre(self) -> list[tuple[str, int]]:
        """Dasselbe fuer die Bilder, die nur auf dem Server liegen."""
        rows = self.conn.execute(
            """SELECT substr(r.taken_at, 1, 4) AS jahr, COUNT(*)
               FROM remote_assets r
               WHERE r.taken_at IS NOT NULL AND r.taken_at <> ''
                 AND NOT EXISTS (SELECT 1 FROM photos p
                                 WHERE p.immich_id = r.immich_id
                                   AND p.immich_id <> '')
               GROUP BY jahr ORDER BY jahr"""
        ).fetchall()
        return [(str(r[0]), int(r[1] or 0)) for r in rows if r[0]]

    def forget_remote(self, immich_ids: list[str]) -> int:
        """Eintraege aus dem Serverspiegel entfernen.

        Wird gebraucht, nachdem Bilder auf dem Server geloescht wurden -
        sonst stuenden sie bis zum naechsten Abgleich noch im Baum.
        """
        if not immich_ids:
            return 0
        platzhalter = ",".join("?" for _ in immich_ids)
        cur = self.conn.execute(
            f"DELETE FROM remote_assets WHERE immich_id IN ({platzhalter})",
            list(immich_ids))
        self.conn.commit()
        return cur.rowcount or 0

    def remote_count(self) -> int:
        row = self.conn.execute(
            """SELECT COUNT(*) FROM remote_assets r
               WHERE NOT EXISTS (SELECT 1 FROM photos p
                                 WHERE p.immich_id = r.immich_id
                                   AND p.immich_id <> '')"""
        ).fetchone()
        return int(row[0] or 0)

    def remote_by_link(self, table: str, key_column: str, key: str) -> list[sqlite3.Row]:
        """Server-Bilder eines Albums oder einer Person ohne lokale Datei."""
        if table not in ("album_assets", "person_assets"):
            raise ValueError("unbekannte Tabelle")
        if key_column not in ("album_id", "person_id"):
            raise ValueError("unbekannte Spalte")
        return self.conn.execute(
            f"""SELECT r.* FROM remote_assets r
                WHERE r.immich_id IN (SELECT immich_id FROM {table}
                                      WHERE {key_column} = ?)
                  AND NOT EXISTS (SELECT 1 FROM photos p
                                  WHERE p.immich_id = r.immich_id
                                    AND p.immich_id <> '')
                ORDER BY r.taken_at DESC""",
            (key,),
        ).fetchall()

    # -- Eigene Alben --------------------------------------------------

    def create_album_local(self, name: str) -> str:
        """Album in Wimmich anlegen - noch ohne Gegenstück auf dem Server.

        Die Kennung beginnt mit „lok-", damit sie sich nie mit einer
        Immich-Kennung überschneidet. Sie bleibt auch nach dem ersten
        Abgleich erhalten; die Serverkennung kommt daneben in immich_id.
        """
        album_id = "lok-" + uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO albums(id, name, asset_count, synced_at, "
            "immich_id, local, dirty) VALUES(?,?,0,NULL,'',1,1)",
            (album_id, name.strip() or "Ohne Namen"),
        )
        self.conn.commit()
        return album_id

    def rename_album(self, album_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE albums SET name=?, dirty=1 WHERE id=?",
            (name.strip() or "Ohne Namen", album_id))
        self.conn.commit()

    def delete_album(self, album_id: str) -> None:
        """Album samt Zuordnungen aus dem Index nehmen.

        Bilder werden dabei nicht angefasst - ein Album ist nur eine
        Zusammenstellung.
        """
        for sql in ("DELETE FROM albums WHERE id=?",
                    "DELETE FROM album_assets WHERE album_id=?",
                    "DELETE FROM album_photos WHERE album_id=?"):
            self.conn.execute(sql, (album_id,))
        self.conn.commit()

    def album(self, album_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()

    def album_add(self, album_id: str, paths: list[str],
                  immich_ids: list[str] | None = None) -> int:
        """Bilder einem Album zuordnen. Ergebnis: Anzahl der Neuzugänge.

        Ein etwaiger Grabstein (früher entfernt) wird dabei wieder
        aufgehoben - sonst käme das Bild beim nächsten Abgleich sofort
        wieder heraus.
        """
        jetzt = time.time()
        eintraege = [(album_id, p, "", jetzt) for p in paths if p]
        eintraege += [(album_id, "", i, jetzt) for i in (immich_ids or []) if i]
        if not eintraege:
            return 0
        self.conn.executemany(
            "INSERT INTO album_photos(album_id, path, immich_id, entfernt, added) "
            "VALUES(?,?,?,0,?) ON CONFLICT(album_id, path, immich_id) "
            "DO UPDATE SET entfernt=0, added=excluded.added",
            eintraege)
        self.conn.execute("UPDATE albums SET dirty=1 WHERE id=?", (album_id,))
        self.conn.commit()
        return len(eintraege)

    def album_remove(self, album_id: str, paths: list[str],
                     immich_ids: list[str] | None = None) -> int:
        """Bilder aus einem Album nehmen.

        Steht das Bild im Serverspiegel, bleibt ein Grabstein
        (entfernt=1) stehen, damit der nächste Abgleich es auch auf dem
        Server aus dem Album nimmt. Sonst reicht das Löschen der Zeile.
        """
        jetzt = time.time()
        eintraege = [(album_id, p, "", jetzt) for p in paths if p]
        eintraege += [(album_id, "", i, jetzt) for i in (immich_ids or []) if i]
        if not eintraege:
            return 0
        self.conn.executemany(
            "INSERT INTO album_photos(album_id, path, immich_id, entfernt, added) "
            "VALUES(?,?,?,1,?) ON CONFLICT(album_id, path, immich_id) "
            "DO UPDATE SET entfernt=1, added=excluded.added",
            eintraege)
        self.conn.execute("UPDATE albums SET dirty=1 WHERE id=?", (album_id,))
        self.conn.commit()
        return len(eintraege)

    def album_eintraege(self, album_id: str, entfernt: bool = False) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT path, immich_id FROM album_photos "
            "WHERE album_id=? AND entfernt=?",
            (album_id, 1 if entfernt else 0)).fetchall()

    def album_dirty(self) -> list[sqlite3.Row]:
        """Alben mit lokalen Änderungen, die auf den Server sollen."""
        return self.conn.execute(
            "SELECT * FROM albums WHERE dirty=1 OR (local=1 AND immich_id='')"
        ).fetchall()

    def album_sauber(self, album_id: str, immich_id: str = "") -> None:
        if immich_id:
            self.conn.execute(
                "UPDATE albums SET immich_id=?, dirty=0, synced_at=? WHERE id=?",
                (immich_id, time.time(), album_id))
        else:
            self.conn.execute(
                "UPDATE albums SET dirty=0, synced_at=? WHERE id=?",
                (time.time(), album_id))
        # Erledigte Grabsteine wegräumen: das Bild ist auf dem Server
        # aus dem Album heraus, es gibt nichts mehr zu tun.
        self.conn.execute(
            "DELETE FROM album_photos WHERE album_id=? AND entfernt=1",
            (album_id,))
        self.conn.commit()

    def album_count(self, album_id: str) -> int:
        """Wie viele Bilder das Album in Wimmich zeigt (lokal + Server)."""
        row = self.conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT p.path AS k FROM photos p
                  WHERE (p.path IN (SELECT path FROM album_photos
                                    WHERE album_id=:a AND entfernt=0 AND path<>'')
                     OR (p.immich_id <> '' AND p.immich_id IN
                         (SELECT immich_id FROM album_assets WHERE album_id=:a)))
                    AND p.path NOT IN (SELECT path FROM album_photos
                                       WHERE album_id=:a AND entfernt=1 AND path<>'')
                 UNION
                 SELECT r.immich_id AS k FROM remote_assets r
                  WHERE (r.immich_id IN (SELECT immich_id FROM album_assets
                                         WHERE album_id=:a)
                     OR r.immich_id IN (SELECT immich_id FROM album_photos
                                        WHERE album_id=:a AND entfernt=0
                                          AND immich_id<>''))
                    AND r.immich_id NOT IN (SELECT immich_id FROM album_photos
                                            WHERE album_id=:a AND entfernt=1
                                              AND immich_id<>'')
                    AND NOT EXISTS (SELECT 1 FROM photos p2
                                    WHERE p2.immich_id = r.immich_id
                                      AND p2.immich_id <> ''))""",
            {"a": album_id}).fetchone()
        return int(row[0] or 0)

    def photos_in_album(self, album_id: str, min_rating: int = 0,
                        order: str = "taken_at", desc: bool = False,
                        stacked: bool = True, prefer_raw: bool = True,
                        show_rejects: bool = True,
                        labels: list[str] | None = None,
                        unlabeled: bool = False) -> list[sqlite3.Row]:
        """Lokale Bilder eines Albums - eigene Zuordnung UND Serverspiegel.

        Anders als photos_by_immich() setzt das nicht voraus, dass ein
        Bild schon hochgeladen ist: was Harald in Wimmich zuordnet,
        steht sofort im Album.
        """
        where = ("(p.path IN (SELECT path FROM album_photos "
                 " WHERE album_id=? AND entfernt=0 AND path<>'')"
                 " OR (p.immich_id <> '' AND p.immich_id IN "
                 "     (SELECT immich_id FROM album_assets WHERE album_id=?)))"
                 " AND p.path NOT IN (SELECT path FROM album_photos "
                 "     WHERE album_id=? AND entfernt=1 AND path<>'')")
        params: list = [album_id, album_id, album_id]
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._select(where, params, order, stacked, prefer_raw, desc=desc)

    def remote_in_album(self, album_id: str) -> list[sqlite3.Row]:
        """Serverbilder eines Albums, zu denen es keine lokale Datei gibt."""
        return self.conn.execute(
            """SELECT r.* FROM remote_assets r
               WHERE (r.immich_id IN (SELECT immich_id FROM album_assets
                                      WHERE album_id=:a)
                  OR r.immich_id IN (SELECT immich_id FROM album_photos
                                     WHERE album_id=:a AND entfernt=0
                                       AND immich_id<>''))
                 AND r.immich_id NOT IN (SELECT immich_id FROM album_photos
                                         WHERE album_id=:a AND entfernt=1
                                           AND immich_id<>'')
                 AND NOT EXISTS (SELECT 1 FROM photos p
                                 WHERE p.immich_id = r.immich_id
                                   AND p.immich_id <> '')
               ORDER BY r.taken_at DESC""",
            {"a": album_id}).fetchall()

    def album_asset_ids(self, album_id: str) -> list[str]:
        """Was der Server laut Spiegel in diesem Album hat."""
        return [str(r[0]) for r in self.conn.execute(
            "SELECT immich_id FROM album_assets WHERE album_id=?", (album_id,))]

    def set_album_immich(self, album_id: str, immich_id: str) -> None:
        """Serverkennung eintragen, ohne die Änderung als erledigt zu buchen."""
        self.conn.execute("UPDATE albums SET immich_id=? WHERE id=?",
                          (immich_id, album_id))
        self.conn.commit()

    def immich_id_fuer_pfad(self, path: str) -> str:
        row = self.conn.execute(
            "SELECT immich_id FROM photos WHERE path=?", (path,)).fetchone()
        return str(row[0] or "") if row else ""

    def album_id_fuer_immich(self, immich_id: str) -> str:
        row = self.conn.execute(
            "SELECT id FROM albums WHERE immich_id=?", (immich_id,)).fetchone()
        return str(row[0]) if row else ""

    def albums(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM albums ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def people(self, include_unnamed: bool = False) -> list[sqlite3.Row]:
        where = "" if include_unnamed else "WHERE name IS NOT NULL AND name <> ''"
        return self.conn.execute(
            f"SELECT * FROM people {where} ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def photos_by_immich(self, table: str, key_column: str, key: str,
                         min_rating: int = 0, order: str = "taken_at",
                         desc: bool = False,
                         stacked: bool = True, prefer_raw: bool = True,
                         show_rejects: bool = True,
                         labels: list[str] | None = None,
                         unlabeled: bool = False) -> list[sqlite3.Row]:
        """Lokale Bilder, die zu einem Album oder einer Person gehören.

        Die Verbindung läuft über immich_id: nur was schon abgeglichen
        ist, kann hier auftauchen. Bilder, die nur auf dem Server liegen,
        erscheinen NICHT - Wimmich zeigt die lokale Bibliothek.
        """
        if table not in ("album_assets", "person_assets"):
            raise ValueError("unbekannte Tabelle")
        if key_column not in ("album_id", "person_id"):
            raise ValueError("unbekannte Spalte")
        where = (f"p.immich_id IN (SELECT immich_id FROM {table} "
                 f"WHERE {key_column} = ?)")
        params: list = [key]
        where, params = _add_filters(where, params, min_rating,
                                     show_rejects, labels, unlabeled)
        return self._select(where, params, order, stacked, prefer_raw, desc=desc)

    def all_photos(self, roots: list[str], min_rating: int = 0,
                   stacked: bool = True, prefer_raw: bool = True,
                   show_rejects: bool = True,
                   labels: list[str] | None = None,
                   unlabeled: bool = False,
                   order: str = "folder",
                   desc: bool = False) -> list[sqlite3.Row]:
        """Alle Bilder aller Bibliotheken, auf einmal.

        Für große Bestände besser all_photos_cursor() nehmen.
        """
        cursor = self.all_photos_cursor(roots, min_rating, stacked, prefer_raw,
                                        show_rejects, labels, unlabeled, order,
                                        desc=desc)
        return cursor.fetchall() if cursor is not None else []

    def unsynced(self, limit: int = 500) -> list[sqlite3.Row]:
        """Lokale Bilder ohne Immich-Kennung - die Arbeitsliste des Abgleichs."""
        return self.conn.execute(
            "SELECT id, path, filename, is_raw, checksum FROM photos "
            "WHERE immich_id IS NULL ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def sync_counts(self) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(immich_id IS NOT NULL) AS synced "
            "FROM photos"
        ).fetchone()
        return int(row["total"] or 0), int(row["synced"] or 0)

    def pending_metadata(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, path, is_raw FROM photos WHERE meta_read = 0 LIMIT ?", (limit,)
        ).fetchall()

    def counts(self) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(meta_read = 0) AS pending FROM photos"
        ).fetchone()
        return int(row["total"] or 0), int(row["pending"] or 0)


_ASC_DESC = re.compile(r"\b(ASC|DESC)\b")


def _reverse_order(order_sql: str) -> str:
    """Dreht eine fertige ORDER-BY-Klausel um, spaltenunabhängig.

    Ein einfacher Wortaustausch ASC<->DESC statt eigener Regeln je
    Sortierschlüssel - so wirkt der Richtungsknopf auf jede Spalte
    gleich, auch auf zusammengesetzte Klauseln wie die Ordneransicht.
    """
    return _ASC_DESC.sub(lambda m: "DESC" if m.group(1) == "ASC" else "ASC",
                         order_sql)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(text: str) -> str:
    """Baut eine FTS5-Anfrage: jedes Wort als Präfixsuche, UND-verknüpft."""
    words = [w for w in text.replace('"', " ").split() if w]
    return " AND ".join(f'"{w}"*' for w in words)


def stack_key_for(folder: str, filename: str) -> str:
    """Schlüssel, der RAW und JPEG derselben Aufnahme zusammenführt.

    Gleicher Ordner plus gleicher Dateiname ohne Endung. DSC_0001.NEF und
    DSC_0001.JPG landen damit im selben Stapel, DSC_0001-2.jpg nicht.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{folder}{os.sep}{stem}".lower()


def _add_filters(where: str, params: list, min_rating: int,
                 show_rejects: bool, labels: list[str] | None,
                 unlabeled: bool = False) -> tuple[str, list]:
    """Ergänzt Bewertungs-, Ablehnungs- und Farbfilter.

    Eine Ablehnung hat keine sinnvolle Sternzahl. Sobald ein Sternfilter
    aktiv ist, fallen Ablehnungen daher immer heraus - auch wenn sie
    grundsätzlich gezeigt werden (dieselbe Regel wie in Cammello).
    """
    if min_rating > 0:
        where += " AND p.rating >= ?"
        params.append(min_rating)
    elif not show_rejects:
        where += " AND p.rating <> ?"
        params.append(REJECT)

    # Farben wirken als ODER, auch zusammen mit „ohne Farbe". Ein
    # unbekannter Markierungstext (eigener Satz in Lightroom) zählt als
    # ohne Farbe - sonst wäre er über die Leiste nicht erreichbar.
    teile = []
    if labels:
        teile.append("p.label IN (" + ", ".join("?" for _ in labels) + ")")
        params.extend(labels)
    if unlabeled:
        bekannt = _alle_label_texte()
        teile.append(
            "(p.label IS NULL OR p.label = '' OR p.label NOT IN ("
            + ", ".join("?" for _ in bekannt) + "))")
        params.extend(bekannt)
    if teile:
        where += " AND (" + " OR ".join(teile) + ")"
    return where, params


def _alle_label_texte() -> list[str]:
    from .marks import LABEL_SETS
    texte = []
    for satz in LABEL_SETS.values():
        texte.extend(satz)
    return texte


def _prune(conn, table: str, link_table: str, column: str, keep: set) -> None:
    """Einträge entfernen, die es auf dem Server nicht mehr gibt."""
    existing = {r[0] for r in conn.execute(f"SELECT id FROM {table}")}
    for gone in existing - keep:
        conn.execute(f"DELETE FROM {link_table} WHERE {column}=?", (gone,))
        conn.execute(f"DELETE FROM {table} WHERE id=?", (gone,))


def _roots_where(roots: list[str]) -> tuple[str, list]:
    """Bedingung, die alle Bibliotheksordner samt Unterordnern abdeckt."""
    parts = []
    params: list = []
    for root in roots:
        prefix = root.rstrip("/\\") + os.sep
        parts.append("(p.folder = ? OR p.folder LIKE ? ESCAPE '\\')")
        params.extend([root, _escape_like(prefix) + "%"])
    return "(" + " OR ".join(parts) + ")", params


def _supports_nulls_last() -> bool:
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("SELECT 1 ORDER BY 1 ASC NULLS LAST").fetchall()
        probe.close()
        return True
    except sqlite3.OperationalError:
        return False
