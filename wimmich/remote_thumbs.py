"""Vorschauen für Bilder, die nur auf dem Server liegen.

Grundregel des Projekts bleibt gewahrt: das ORIGINAL wird nie von selbst
geholt. Hier landen ausschließlich die kleinen Vorschaubilder, die die
Kachel braucht - und die auch nur, wenn sie tatsächlich angezeigt wird.

Der Cache liegt neben dem Kachel-Cache der lokalen Bilder und darf
jederzeit gelöscht werden; er wird bei Bedarf neu gefüllt.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .config import CACHE_DIR, Config

REMOTE_DIR = CACHE_DIR / "remote"

_sperre = threading.Lock()
_client_sperre = threading.Lock()
_ersatz_client = None
_letzte_meldung = ""


def letzte_meldung() -> str:
    """Warum die letzte Vorschau nicht kam - fuer die Diagnose."""
    return _letzte_meldung


def _client_bei_bedarf():
    """Eigener Client, falls das Fenster keinen mitgibt.

    Frueher hing alles an einem Client, der EINMAL beim Start gebaut
    wurde. War der Server da gerade nicht erreichbar, blieben alle
    Serverkacheln fuer den Rest der Sitzung leer. Jetzt wird bei Bedarf
    einer aufgebaut und behalten.
    """
    global _ersatz_client, _letzte_meldung
    with _client_sperre:
        if _ersatz_client is not None:
            return _ersatz_client
        from .immich import ImmichClient, ImmichError
        cfg = Config()
        url, key = cfg["immich_url"], cfg["immich_key"]
        if not url or not key:
            _letzte_meldung = "Immich ist nicht eingerichtet"
            return None
        client = ImmichClient(url, key)
        try:
            client.connect()
        except ImmichError as exc:
            _letzte_meldung = f"Verbindung fehlgeschlagen: {exc}"
            return None
        _ersatz_client = client
        return client


def cache_path(immich_id: str, gross: bool = False) -> Path:
    """Ablageort einer Vorschau. Die Kennung ist schon eindeutig."""
    sauber = "".join(z for z in immich_id if z.isalnum() or z in "-_")
    endung = "_p.jpg" if gross else "_t.jpg"
    return REMOTE_DIR / (sauber + endung)


def cached(immich_id: str, gross: bool = False) -> bytes | None:
    """Vorschau aus dem Cache, ohne den Server zu fragen."""
    pfad = cache_path(immich_id, gross)
    try:
        return pfad.read_bytes() if pfad.exists() else None
    except OSError:
        return None


def fetch(client, immich_id: str, gross: bool = False) -> bytes | None:
    """Vorschau liefern - aus dem Cache, sonst einmal vom Server holen.

    Liefert None, wenn der Server sie nicht hergibt. Das ist kein Fehler:
    die Kachel zeigt dann einen Platzhalter.
    """
    global _letzte_meldung
    vorhanden = cached(immich_id, gross)
    if vorhanden:
        return vorhanden

    if client is None:
        client = _client_bei_bedarf()
    if client is None:
        return None

    try:
        daten = client.thumbnail(immich_id, gross=gross)
    except Exception as exc:                     # Netz, Zeitgrenze, alles
        _letzte_meldung = f"{immich_id}: {exc}"
        return None
    if not daten:
        _letzte_meldung = f"{immich_id}: Server lieferte keine Vorschau"
        return None

    with _sperre:
        try:
            REMOTE_DIR.mkdir(parents=True, exist_ok=True)
            ziel = cache_path(immich_id, gross)
            vorlaeufig = ziel.with_suffix(".part")
            vorlaeufig.write_bytes(daten)
            vorlaeufig.replace(ziel)
        except OSError:
            pass          # Ohne Cache geht es auch, nur langsamer
    return daten


def clear() -> int:
    """Cache leeren. Ergebnis: Anzahl gelöschter Dateien."""
    if not REMOTE_DIR.exists():
        return 0
    anzahl = 0
    for datei in REMOTE_DIR.glob("*.jpg"):
        try:
            datei.unlink()
            anzahl += 1
        except OSError:
            pass
    return anzahl
