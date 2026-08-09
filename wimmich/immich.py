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

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    # -- Grundlagen ----------------------------------------------------

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 body: dict | None = None, raw: bytes | None = None,
                 content_type: str | None = None,
                 extra_headers: dict | None = None) -> tuple[int, bytes]:
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
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
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

    def connect(self) -> ServerInfo:
        """Erreichbarkeit prüfen und die Eigenheiten des Servers feststellen."""
        status, payload = self._request("GET", "/server/ping")
        if status == 404:
            # Sehr alte Server hatten /server-info/ping
            status, payload = self._request("GET", "/server-info/ping")
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

        files = [("assetData", target.name, target.read_bytes())]
        if sidecar and Path(sidecar).exists():
            files.append(("sidecarData", Path(sidecar).name,
                          Path(sidecar).read_bytes()))

        headers = {"x-immich-checksum": checksum} if checksum else None
        status, payload = self._post_multipart(fields, files, headers)

        if status == 400 and self.info.device_fields:
            # Neuere Server kennen die Gerätefelder nicht mehr - ohne wiederholen.
            self.info.device_fields = False
            fields.pop("deviceAssetId", None)
            fields.pop("deviceId", None)
            status, payload = self._post_multipart(fields, files, headers)

        if status >= 400:
            raise ImmichError(_error_text(status, payload), status)

        try:
            data = json.loads(payload)
        except ValueError:
            return None, False
        return data.get("id"), bool(data.get("duplicate"))

    def _post_multipart(self, fields: dict, files: list, headers: dict | None):
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        for name, filename, blob in files:
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            body += blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")

        return self._request(
            "POST", self._path("/assets", "/asset/upload"),
            raw=bytes(body),
            content_type=f"multipart/form-data; boundary={boundary}",
            extra_headers=headers,
        )

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
            for entry in batch:
                if entry.get("id"):
                    result.append(Person(
                        id=entry["id"],
                        name=entry.get("name") or "",
                        hidden=bool(entry.get("isHidden")),
                    ))
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


def _error_text(status: int, payload: bytes) -> str:
    text = ""
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            text = str(data.get("message") or data.get("error") or "")
    except (ValueError, TypeError):
        text = payload[:200].decode("utf-8", errors="replace") if payload else ""
    return f"HTTP {status}" + (f": {text}" if text else "")
