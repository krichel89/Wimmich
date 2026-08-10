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
from pathlib import Path

from .config import DB_PATH, CONFIG_DIR
from .marks import REJECT, clamp_rating

SCHEMA_VERSION = 9

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
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    asset_count INTEGER NOT NULL DEFAULT 0,
    synced_at   REAL
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
        """Albenliste des Servers übernehmen. Was dort weg ist, fliegt hier raus."""
        conn = self.conn
        keep = set()
        for album in albums:
            keep.add(album.id)
            conn.execute(
                "INSERT INTO albums(id, name, asset_count, synced_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "asset_count=excluded.asset_count, synced_at=excluded.synced_at",
                (album.id, album.name, album.asset_count, time.time()),
            )
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
