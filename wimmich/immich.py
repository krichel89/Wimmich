"""Immich-Client.

Gebaut gegen die offizielle OpenAPI-Spezifikation von Immich
(open-api/immich-openapi-specs.json im Projekt-Repository).

Bewusst serverunabhängig:

  * Nur die Standardauthentifizierung über den Kopfzeilen-Schlüssel
    ``x-api-key``. Das ist seit jeher unverändert.
  * Die Endpunktnamen wurden im Laufe der Zeit von Einzahl auf Mehrzahl
    umgestellt (``/asset/upload`` → ``/assets``, ``/album`` → ``/albums``).
    Welche Fassung ein Server spricht, wird EINMAL beim Verbinden
    ausprobiert und gemerkt - nicht an einer Versionsnummer festgemacht,
    denn die Zuordnung Version→Pfad ist nirgends verlässlich dokumentiert.
  * Felder, die neuere Server nicht mehr verlangen, ältere aber schon
    (``deviceAssetId``, ``deviceId``), werden mitgeschickt und beim
    ersten Ablehnen weggelassen.

Nichts davon setzt eine bestimmte Immich-Version voraus.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "Wimmich"
TIMEOUT = 60
# Vorschauen duerfen NICHT in die grosse Zeitgrenze laufen: sie werden
# beim Blaettern geholt, und vier Wege a 60 s waeren vier Minuten, in
# denen das Fenster steht.
VORSCHAU_TIMEOUT = 8
# Vorabpruefung: nur die Frage „antwortet da ueberhaupt jemand?".
# Sie laeuft beim Start und vor jedem Serverzugriff und darf deshalb
# nicht lange dauern - nach vier Sekunden ist der Server fuer unsere
# Zwecke weg.
PING_TIMEOUT = 4

# Dateiendung -> Inhaltstyp, wie Immich es selbst fuehrt
# (server/src/utils/mime-types.ts). Der Server prueft den mitgeschickten
# Typ; "application/octet-stream" fuer alles fuehrt dazu, dass RAW-Dateien
# abgelehnt werden, obwohl die Endung unterstuetzt waere.
CONTENT_TYPES = {
    ".3fr": "image/3fr", ".ari": "image/ari", ".arw": "image/arw",
    ".cap": "image/cap", ".cin": "image/cin", ".cr2": "image/cr2",
    ".cr3": "image/cr3", ".crw": "image/crw", ".dcr": "image/dcr",
    ".dng": "image/dng", ".erf": "image/erf", ".fff": "image/fff",
    ".iiq": "image/iiq", ".k25": "image/k25", ".kdc": "image/kdc",
    ".mrw": "image/mrw", ".nef": "image/nef", ".nrw": "image/nrw",
    ".orf": "image/orf", ".ori": "image/ori", ".pef": "image/pef",
    ".psd": "image/psd", ".raf": "image/raf", ".raw": "image/raw",
    ".rw2": "image/rw2", ".rwl": "image/rwl", ".sr2": "image/sr2",
    ".srf": "image/srf", ".srw": "image/srw", ".x3f": "image/x3f",
    ".avif": "image/avif", ".bmp": "image/bmp", ".gif": "image/gif",
    ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".jpe": "image/jpeg",
    ".insp": "image/jpeg", ".mpo": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".hif": "image/hif", ".jp2": "image/jp2", ".jxl": "image/jxl",
    ".svg": "image/svg", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".xmp": "application/xml",
}


def content_type_for(filename: str) -> str:
    """Inhaltstyp anhand der Endung; unbekanntes bleibt octet-stream."""
    endung = Path(filename).suffix.lower()
    return CONTENT_TYPES.get(endung, "application/octet-stream")

def _meckert_geraetefelder(payload: bytes | None) -> bool:
    """Bemaengelt der Server die Geraetefelder als UEBERFLUESSIG?"""
    text = (payload or b"").decode("utf-8", "replace").lower()
    return ("deviceassetid" in text or "deviceid" in text) and (
        "should not exist" in text or "unexpected" in text
        or "not allowed" in text
        or ("property" in text and "exist" in text))


def _verlangt_geraetefelder(payload: bytes | None) -> bool:
    """Verlangt der Server die Geraetefelder?"""
    text = (payload or b"").decode("utf-8", "replace").lower()
    return ("deviceassetid" in text or "deviceid" in text) and (
        "must be" in text or "should not be empty" in text
        or "required" in text)


def ist_dauerhafter_fehler(meldung: str) -> bool:
    """Fehler, die sich beim naechsten Lauf NICHT von selbst erledigen.

    Ein nicht unterstuetzter Dateityp bleibt es auch morgen noch. Solche
    Dateien immer wieder anzubieten haelt den Abgleich nur auf.
    """
    text = (meldung or "").lower()
    return "unsupported file type" in text or "nicht unterstützt" in text



class _MultipartStrom:
    """Multipart-Rumpf, der die Dateien erst beim Absenden liest.

    urllib nimmt fuer `data` alles, was `read(n)` kann - dann muss aber
    die Laenge selbst mitgeschickt werden, sonst greift http.client zu
    „Transfer-Encoding: chunked", und darauf reagieren nicht alle
    Server freundlich. Deshalb rechnet __len__ die Gesamtlaenge vorher
    aus: Textteile plus Dateigroessen plus Trennzeilen.
    """

    BLOCK = 1024 * 256

    def __init__(self, boundary: str, felder: dict, dateien: list) -> None:
        self._teile: list = []          # bytes ODER (Pfad, Groesse)
        for name, wert in felder.items():
            self._teile.append((
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{wert}\r\n").encode("utf-8"))
        for name, dateiname, pfad in dateien:
            self._teile.append((
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{dateiname}"\r\n'
                f"Content-Type: {content_type_for(dateiname)}\r\n\r\n"
            ).encode("utf-8"))
            self._teile.append((str(pfad), Path(pfad).stat().st_size))
            self._teile.append(b"\r\n")
        self._teile.append(f"--{boundary}--\r\n".encode("utf-8"))

        self._laenge = sum(
            teil[1] if isinstance(teil, tuple) else len(teil)
            for teil in self._teile)
        self._i = 0
        self._offen = None
        self._rest = b""

    def __len__(self) -> int:
        return self._laenge

    def read(self, menge: int = -1) -> bytes:
        """Naechstes Stueck. menge<0 heisst: alles - das meiden wir."""
        if menge is None or menge < 0:
            menge = self.BLOCK
        while len(self._rest) < menge and self._i < len(self._teile):
            teil = self._teile[self._i]
            if isinstance(teil, tuple):
                if self._offen is None:
                    self._offen = open(teil[0], "rb")   # noqa: SIM115
                stueck = self._offen.read(max(menge - len(self._rest),
                                              self.BLOCK))
                if stueck:
                    self._rest += stueck
                    continue
                self._offen.close()
                self._offen = None
                self._i += 1
            else:
                self._rest += teil
                self._i += 1
        ergebnis, self._rest = self._rest[:menge], self._rest[menge:]
        return ergebnis

    def close(self) -> None:
        if self._offen is not None:
            self._offen.close()
            self._offen = None


class ImmichError(RuntimeError):
    """Fehler beim Reden mit dem Server."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ServerInfo:
    version: str = ""
    user: str = ""
    plural_paths: bool = True     # /assets statt /asset
    device_fields: bool = True    # deviceAssetId/deviceId mitschicken


@dataclass
class Album:
    id: str
    name: str
    asset_count: int = 0


@dataclass
class Person:
    id: str
    name: str
    hidden: bool = False


@dataclass
class SyncResult:
    checked: int = 0
    uploaded: int = 0
    already_there: int = 0
    failed: int = 0
    skipped: int = 0          # vom Server grundsätzlich abgelehnte Dateitypen
    errors: list[str] = field(default_factory=list)


def file_checksum(path: str | Path) -> str:
    """SHA-1 der Datei, Base64-kodiert.

    Genau das Format, das Immich für die Dublettenprüfung erwartet
    (Spezifikation: "Base64 or hex encoded SHA1 hash").
    """
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def normalise_base_url(url: str) -> str:
    """Macht aus allem, was Leute eintippen, eine brauchbare Basis.

    'fotos.example.de', 'https://fotos.example.de/', '.../api' und
    '.../api/' führen alle zum selben Ergebnis.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    if not url.endswith("/api"):
        url += "/api"
    return url


class ImmichClient:
    """Schmaler Client. Kein SDK, keine Zusatzabhängigkeit."""

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = normalise_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self.info = ServerInfo()
        self._connected = False
        # Fuer die Diagnose: warum die letzte Vorschau nicht kam und
        # welche Wege dabei probiert wurden.
        self.letzter_vorschaufehler = ""
        self.letzte_vorschauversuche: list[str] = []

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    # -- Grundlagen ----------------------------------------------------

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 body: dict | None = None, raw: object = None,   # bytes ODER ein Strom mit read()
                 content_type: str | None = None,
                 extra_headers: dict | None = None,
                 timeout: float | None = None) -> tuple[int, bytes]:
        if not self.configured:
            raise ImmichError("Server oder Schlüssel fehlt")

        url = self.base_url + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)

        data = raw
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)

        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request,
                                        timeout=timeout or TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise ImmichError(f"Server nicht erreichbar: {exc.reason}") from exc
        except OSError as exc:
            raise ImmichError(f"Verbindungsfehler: {exc}") from exc

    def _json(self, method: str, path: str, **kwargs):
        status, payload = self._request(method, path, **kwargs)
        if status == 401 or status == 403:
            raise ImmichError("Schlüssel abgelehnt - Rechte prüfen", status)
        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return None

    # -- Verbinden -----------------------------------------------------

    def erreichbar(self, timeout: float = PING_TIMEOUT) -> tuple[bool, str]:
        """Kurze Vorabpruefung: antwortet der Server?

        Getrennt von connect(), weil das Verbinden die volle Zeitgrenze
        von 60 s haben darf - diese Frage aber nicht. Sie wird gestellt,
        BEVOR irgendetwas Langes angefangen wird, damit das Fenster
        nicht steht.
        """
        if not self.configured:
            return False, "Immich ist nicht eingerichtet"
        try:
            self._request("GET", "/server/ping", timeout=timeout)
        except ImmichError as exc:
            if getattr(exc, "status", 0) == 404:
                return True, ""     # sehr alter Server, aber er antwortet
            return False, str(exc)
        return True, ""

    def connect(self, timeout: float | None = None) -> ServerInfo:
        """Erreichbarkeit prüfen und die Eigenheiten des Servers feststellen."""
        status, payload = self._request("GET", "/server/ping", timeout=timeout)
        if status == 404:
            # Sehr alte Server hatten /server-info/ping
            status, payload = self._request("GET", "/server-info/ping",
                                            timeout=timeout)
        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)

        self.info = ServerInfo()

        for path in ("/server/version", "/server-info/version"):
            try:
                data = self._json("GET", path)
            except ImmichError:
                continue
            if isinstance(data, dict) and "major" in data:
                self.info.version = (
                    f"{data['major']}.{data['minor']}.{data['patch']}"
                )
                break

        try:
            me = self._json("GET", "/users/me")
        except ImmichError:
            me = None
        if isinstance(me, dict):
            self.info.user = me.get("email") or me.get("name") or ""

        # Mehrzahl oder Einzahl? Einmal ausprobieren statt raten.
        status, _ = self._request("GET", "/albums", query={"shared": "false"})
        self.info.plural_paths = status != 404

        self._connected = True
        return self.info

    def _path(self, plural: str, legacy: str) -> str:
        return plural if self.info.plural_paths else legacy

    # -- Vorhandensein prüfen ------------------------------------------

    def check_uploaded(self, items: list[tuple[str, str]]) -> dict[str, str | None]:
        """Prüft anhand der Prüfsumme, was schon auf dem Server liegt.

        items: Liste aus (eigene Kennung, Prüfsumme).
        Ergebnis: Kennung -> Asset-Kennung des Servers, oder None,
        wenn die Datei dort noch fehlt.
        """
        if not items:
            return {}
        payload = {"assets": [{"id": ident, "checksum": checksum}
                              for ident, checksum in items]}
        data = self._json(
            "POST",
            self._path("/assets/bulk-upload-check", "/asset/bulk-upload-check"),
            body=payload,
        )
        result: dict[str, str | None] = {}
        for entry in (data or {}).get("results", []):
            # 'reject' mit Grund 'duplicate' heißt: liegt schon da.
            if entry.get("action") == "reject" and entry.get("reason") == "duplicate":
                result[entry["id"]] = entry.get("assetId")
            else:
                result[entry["id"]] = None
        return result

    # -- Hochladen -----------------------------------------------------

    def upload(self, path: str, *, checksum: str | None = None,
               sidecar: str | None = None) -> tuple[str | None, bool]:
        """Datei hochladen.

        Ergebnis: (Asset-Kennung, war_schon_da). Ein XMP-Sidecar wird
        mitgeschickt, wenn er existiert - so kommen Bewertung und
        Farbmarkierung gleich mit auf den Server.
        """
        target = Path(path)
        stat = target.stat()
        created = _iso(stat.st_mtime)

        fields = {
            "fileCreatedAt": created,
            "fileModifiedAt": created,
            "isFavorite": "false",
            "filename": target.name,
        }
        if self.info.device_fields:
            # Ältere Server verlangen diese beiden Felder.
            fields["deviceAssetId"] = f"{target.name}-{int(stat.st_mtime)}"
            fields["deviceId"] = "wimmich"

        # Nur die PFADE - gelesen wird erst beim Absenden, Stueck fuer
        # Stueck. Ein 100-MB-RAW liegt so nie im Speicher.
        files = [("assetData", target.name, str(target))]
        if sidecar and Path(sidecar).exists():
            files.append(("sidecarData", Path(sidecar).name, str(sidecar)))

        headers = {"x-immich-checksum": checksum} if checksum else None
        status, payload = self._post_multipart(fields, files, headers)

        # Nur dann ohne Gerätefelder wiederholen, wenn der Server GENAU DIESE
        # Felder bemängelt. Vorher genügte irgendein 400er - eine Ablehnung
        # wegen eines nicht unterstützten Dateityps schaltete die Felder
        # dauerhaft ab, und danach scheiterte JEDER weitere Upload an
        # „deviceAssetId must be a string". Die meisten Immich-Fassungen
        # VERLANGEN die Felder.
        if status == 400 and self.info.device_fields and _meckert_geraetefelder(payload):
            self.info.device_fields = False
            fields.pop("deviceAssetId", None)
            fields.pop("deviceId", None)
            status, payload = self._post_multipart(fields, files, headers)
        elif status == 400 and not self.info.device_fields and _verlangt_geraetefelder(payload):
            # Umgekehrter Fall: der Server will sie doch haben.
            self.info.device_fields = True
            stat_neu = target.stat()
            fields["deviceAssetId"] = f"{target.name}-{int(stat_neu.st_mtime)}"
            fields["deviceId"] = "wimmich"
            status, payload = self._post_multipart(fields, files, headers)

        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)

        try:
            data = json.loads(payload)
        except ValueError:
            return None, False
        return data.get("id"), bool(data.get("duplicate"))

    def _post_multipart(self, fields: dict, dateien: list, headers: dict | None):
        """Hochladen, ohne die Datei in den Speicher zu holen.

        `dateien` ist [(Feldname, Dateiname, Pfad), …]. Frueher stand
        hier ein bytearray, in das die ganze Aufnahme kopiert wurde -
        bei einem 100-MB-RAW belegte allein der Rumpf gut 200 MB
        (einmal das bytearray, einmal die bytes()-Kopie beim Absenden).
        Jetzt wird der Rumpf stueckweise von der Platte gelesen.
        """
        boundary = uuid.uuid4().hex
        strom = _MultipartStrom(boundary, fields, dateien)
        return self._request(
            "POST", self._path("/assets", "/asset/upload"),
            raw=strom,
            content_type=f"multipart/form-data; boundary={boundary}",
            extra_headers={**(headers or {}),
                           "Content-Length": str(len(strom))},
        )

    # -- Bilder, die (nur) auf dem Server liegen ------------------------

    def list_assets(self, seite: int = 1, groesse: int = 250) -> tuple[list[dict], bool]:
        """Eine Seite aller Server-Bilder. Ergebnis: (Eintraege, gibt_es_mehr).

        Laeuft ueber POST /search/metadata - denselben Weg, den Wimmich
        schon fuer die Bilder einer Person benutzt. Ein reines
        GET /assets gibt es in neueren Immich-Fassungen nicht mehr.
        """
        data = self._json(
            "POST", self._path("/search/metadata", "/search/metadata"),
            body={"page": seite, "size": groesse, "withExif": True},
        ) or {}
        eintraege = (data.get("assets") or {})
        elemente = eintraege.get("items") or []
        weiter = bool(eintraege.get("nextPage"))
        return elemente, weiter

    def thumbnail(self, asset_id: str, gross: bool = False) -> bytes | None:
        """Vorschaubild vom Server. Keine Ausnahme bei Misserfolg."""
        groesse = "preview" if gross else "thumbnail"
        # Mehrere Schreibweisen, weil sich der Weg zwischen den
        # Immich-Fassungen geaendert hat. Ein Fehlschlag darf NICHT die
        # restlichen Versuche verhindern - genau daran scheiterten
        # aeltere Server bisher stumm.
        versuche = [
            (f"/assets/{asset_id}/thumbnail", {"size": groesse}),
            (f"/assets/{asset_id}/thumbnail", None),
            (f"/asset/thumbnail/{asset_id}", {"format": "JPEG"}),
            (f"/asset/thumbnail/{asset_id}", None),
        ]
        if gross:
            versuche.insert(2, (f"/assets/{asset_id}/original", None))
        letzter = None
        self.letzte_vorschauversuche = []
        for pfad, abfrage in versuche:
            try:
                # Accept MUSS hier auf Bilddaten stehen. Der Vorgabewert
                # ist application/json - ein Server, der sich daran haelt,
                # lehnt einen Bildabruf damit ab.
                status, payload = self._request(
                    "GET", pfad, query=abfrage,
                    extra_headers={"Accept": "image/*, */*"},
                    timeout=VORSCHAU_TIMEOUT)
            except ImmichError as exc:
                self.letzte_vorschauversuche.append(f"{pfad}: {exc}")
                letzter = exc
                # Ist der Server gar nicht erreichbar, helfen die
                # anderen Schreibweisen auch nicht - sie kosten nur
                # dieselbe Wartezeit noch dreimal.
                if "nicht erreichbar" in str(exc) or "Verbindungsfehler" in str(exc):
                    break
                continue
            # Den ANTWORTTEXT mitschreiben, nicht nur Status und Groesse.
            # Bei 403 standen dort 117 Bytes, die das fehlende Recht
            # sofort benannt haetten - sie wurden bisher weggeworfen und
            # haben sechs Fassungen Fehlersuche gekostet.
            grund = ""
            if status >= 400:
                grund = _antworttext(payload)
            self.letzte_vorschauversuche.append(
                f"{pfad}: HTTP {status}, {len(payload or b'')} B"
                + (f" – {grund}" if grund else ""))
            if status < 400 and _sieht_nach_bild_aus(payload):
                return payload
            if status < 400 and payload:
                # 200 mit JSON oder einer Anmeldeseite: der Server hat
                # geantwortet, aber kein Bild geliefert. Das als Vorschau
                # weiterzureichen ergaebe eine kaputte Kachel.
                letzter = ImmichError(
                    f"Antwort ist kein Bild ({len(payload)} B)", status)
                continue
            letzter = ImmichError(_error_text(status, payload), status)
        if letzter is not None:
            self.letzter_vorschaufehler = str(letzter)
        return None

    def delete_assets(self, asset_ids: list[str], endgueltig: bool = False) -> int:
        """Bilder auf dem Server loeschen.

        Vorgabe ist der PAPIERKORB von Immich (force=false): dort laesst
        sich das Bild zurueckholen, hier gibt es kein Rueckgaengig. Nur
        wenn der Server damit nichts anfangen kann (aeltere Fassungen
        ohne Papierkorb, HTTP 400), wird endgueltig geloescht.

        Rueckgabe: Anzahl der uebergebenen Kennungen bei Erfolg.
        """
        if not asset_ids:
            return 0
        koerper = {"ids": list(asset_ids), "force": bool(endgueltig)}
        letzter: ImmichError | None = None
        for pfad in ("/assets", "/asset"):
            status, payload = self._request("DELETE", pfad, body=koerper)
            if status < 400:
                return len(asset_ids)
            if status == 400 and not endgueltig:
                # Server kennt den Papierkorb nicht -> endgueltig fragen
                return self.delete_assets(asset_ids, endgueltig=True)
            letzter = ImmichError(_error_text(status, payload), status)
        raise letzter or ImmichError("Löschen fehlgeschlagen")

    def download_original(self, asset_id: str) -> bytes | None:
        """Originaldatei vom Server holen - nur auf ausdruecklichen Wunsch."""
        for pfad in (f"/assets/{asset_id}/original", f"/asset/file/{asset_id}"):
            try:
                status, payload = self._request("GET", pfad)
            except ImmichError:
                return None
            if status < 400 and payload:
                return payload
        return None

    # -- Alben ---------------------------------------------------------


    def albums(self) -> list[Album]:
        data = self._json("GET", self._path("/albums", "/album")) or []
        return [
            Album(id=a["id"], name=a.get("albumName", ""),
                  asset_count=int(a.get("assetCount") or 0))
            for a in data if a.get("id")
        ]

    def album_assets(self, album_id: str) -> list[dict]:
        """Alle Bilder eines Albums."""
        data = self._json(
            "GET", self._path(f"/albums/{album_id}", f"/album/{album_id}")
        ) or {}
        return data.get("assets") or []

    def create_album(self, name: str, asset_ids: list[str] | None = None) -> str:
        data = self._json(
            "POST", self._path("/albums", "/album"),
            body={"albumName": name, "assetIds": asset_ids or []},
        ) or {}
        return data.get("id", "")

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> int:
        """Bilder einem Album hinzufügen. Ergebnis: Anzahl der Neuzugänge."""
        if not asset_ids:
            return 0
        data = self._json(
            "PUT",
            self._path(f"/albums/{album_id}/assets", f"/album/{album_id}/assets"),
            body={"ids": asset_ids},
        ) or []
        return sum(1 for entry in data if entry.get("success"))

    def rename_album(self, album_id: str, name: str) -> bool:
        """Albumnamen auf dem Server ändern."""
        status, payload = self._request(
            "PATCH", self._path(f"/albums/{album_id}", f"/album/{album_id}"),
            body={"albumName": name})
        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)
        return True

    def remove_from_album(self, album_id: str, asset_ids: list[str]) -> int:
        """Bilder aus einem Album nehmen - das Bild selbst bleibt bestehen."""
        if not asset_ids:
            return 0
        data = self._json(
            "DELETE",
            self._path(f"/albums/{album_id}/assets", f"/album/{album_id}/assets"),
            body={"ids": asset_ids},
        ) or []
        return sum(1 for entry in data if entry.get("success"))

    def delete_album(self, album_id: str) -> bool:
        """Album auf dem Server löschen. Die Bilder bleiben erhalten."""
        status, payload = self._request(
            "DELETE", self._path(f"/albums/{album_id}", f"/album/{album_id}"))
        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)
        return True

    # -- Personen ------------------------------------------------------

    def people(self, with_hidden: bool = False) -> list[Person]:
        """Gesichter/Personen des Servers, seitenweise geholt."""
        result: list[Person] = []
        page = 1
        while True:
            data = self._json(
                "GET", self._path("/people", "/person"),
                query={"page": page, "size": 500,
                       "withHidden": "true" if with_hidden else "false"},
            ) or {}
            batch = data.get("people") or []
            result.extend(
                Person(id=entry["id"],
                       name=entry.get("name") or "",
                       hidden=bool(entry.get("isHidden")))
                for entry in batch if entry.get("id"))
            if not data.get("hasNextPage") or not batch:
                break
            page += 1
            if page > 50:      # Notbremse gegen falsch gesetztes hasNextPage
                break
        return result

    def assets_of_person(self, person_id: str, limit: int = 1000) -> list[dict]:
        """Bilder einer Person über die Metadatensuche."""
        assets: list[dict] = []
        page = 1
        while len(assets) < limit:
            data = self._json(
                "POST", self._path("/search/metadata", "/search/metadata"),
                body={"personIds": [person_id], "page": page, "size": 250},
            ) or {}
            block = (data.get("assets") or {})
            items = block.get("items") or []
            assets.extend(items)
            if not block.get("nextPage") or not items:
                break
            page += 1
        return assets[:limit]


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _sieht_nach_bild_aus(payload: bytes | None) -> bool:
    """Sind das wirklich Bilddaten?

    Ein Server kann mit HTTP 200 antworten und trotzdem kein Bild
    schicken - JSON-Fehler, Anmeldeseite, Platzhalter eines Proxys. Das
    ungeprueft als Vorschau zu nehmen ergibt eine kaputte Kachel und
    verdeckt die eigentliche Ursache.
    """
    if not payload or len(payload) < 32:
        return False
    return (payload[:3] == b"\xff\xd8\xff"                    # JPEG
            or payload[:8] == b"\x89PNG\r\n\x1a\n"             # PNG
            or payload[:6] in (b"GIF87a", b"GIF89a")
            or payload[:2] in (b"II", b"MM")                   # TIFF
            or (payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"))


def _antworttext(payload: bytes | None) -> str:
    """Klartext aus einer Fehlerantwort - ohne die Statuszeile davor."""
    if not payload:
        return ""
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            wert = data.get("message") or data.get("error") or ""
            if isinstance(wert, list):
                wert = "; ".join(str(w) for w in wert)
            return str(wert)[:200]
    except (ValueError, TypeError):
        pass
    return payload[:200].decode("utf-8", errors="replace").strip()


def _error_text(status: int, payload: bytes) -> str:
    text = ""
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            text = str(data.get("message") or data.get("error") or "")
    except (ValueError, TypeError):
        text = payload[:200].decode("utf-8", errors="replace") if payload else ""
    return f"HTTP {status}" + (f": {text}" if text else "")
