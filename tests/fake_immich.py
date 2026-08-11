"""Nachbau eines Immich-Servers zum Prüfen des Clients.

Antwortet nach der offiziellen OpenAPI-Spezifikation. Zwei Betriebsarten:

  modern  Endpunkte in Mehrzahl (/assets, /albums, /people) und die
          Gerätefelder werden mit 400 abgelehnt
  legacy  Endpunkte in Einzahl (/asset/upload, /album) und die
          Gerätefelder sind Pflicht

Damit lässt sich prüfen, ob Wimmich wirklich serverunabhängig ist.
"""

from __future__ import annotations

import base64
import email.parser
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {
    "mode": "modern",
    "assets": {},        # checksum -> asset id
    "uploads": [],       # (filename, hat_sidecar)
    "albums": [{"id": "alb-1", "albumName": "Berlinale 2026", "assetCount": 2},
               {"id": "alb-2", "albumName": "Cannes", "assetCount": 0}],
    "people": [{"id": "per-1", "name": "Anna Beispiel", "isHidden": False},
               {"id": "per-2", "name": "", "isHidden": False}],
    "requests": [],
    "api_key": "geheim",
    # Bilddaten je Kennung: {"thumb": bytes, "original": bytes, "name": str}
    "bilder": {},
    "geloescht": [],     # Kennungen, die ueber DELETE weggeraeumt wurden
    "papierkorb": True,  # False = Server kennt force=false nicht (HTTP 400)
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- Hilfen --------------------------------------------------------

    def _send(self, status, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get("x-api-key") == STATE["api_key"]

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _path(self):
        return self.path.split("?")[0]

    def _legacy(self):
        return STATE["mode"] == "legacy"

    # -- GET -----------------------------------------------------------

    def do_GET(self):
        path = self._path()
        STATE["requests"].append(("GET", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})

        if path == "/api/server/ping":
            return self._send(200, {"res": "pong"})
        if path == "/api/server/version":
            return self._send(200, {"major": 2, "minor": 5, "patch": 6,
                                    "prerelease": 0})
        if path == "/api/users/me":
            return self._send(200, {"email": "harald@example.de", "name": "Harald"})

        if path == "/api/albums":
            if self._legacy():
                return self._send(404, {"message": "Not found"})
            return self._send(200, STATE["albums"])
        if path == "/api/album" and self._legacy():
            return self._send(200, STATE["albums"])

        for prefix in ("/api/albums/", "/api/album/"):
            if path.startswith(prefix):
                if prefix == "/api/albums/" and self._legacy():
                    return self._send(404, {"message": "Not found"})
                album_id = path[len(prefix):]
                assets = [{"id": f"asset-{album_id}-{i}"} for i in range(2)]
                return self._send(200, {"id": album_id, "assets": assets})

        if path in ("/api/people", "/api/person"):
            if path == "/api/people" and self._legacy():
                return self._send(404, {"message": "Not found"})
            return self._send(200, {"people": STATE["people"], "total": 2,
                                    "hidden": 0, "hasNextPage": False})

        # Vorschau und Original. Der Weg in Mehrzahl ist der heutige,
        # der in Einzahl der alte - genau wie beim Rest.
        if path.startswith("/api/assets/") or path.startswith("/api/asset/"):
            teile = path.split("/")
            if path.endswith("/thumbnail") and len(teile) == 5:
                return self._bild(teile[3], "thumb")
            if path.endswith("/original") and len(teile) == 5:
                return self._bild(teile[3], "original")
            if teile[2] == "asset" and teile[3] in ("thumbnail", "file"):
                return self._bild(teile[4],
                                  "thumb" if teile[3] == "thumbnail" else "original")

        return self._send(404, {"message": "Not found"})

    def _bild(self, asset_id: str, art: str):
        eintrag = STATE["bilder"].get(asset_id)
        if not eintrag or asset_id in STATE["geloescht"]:
            return self._send(404, {"message": "Not found"})
        daten = eintrag.get(art) or b""
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_DELETE(self):
        path = self._path()
        STATE["requests"].append(("DELETE", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})
        body = self._read_body()
        plural = path == "/api/assets"
        if path not in ("/api/assets", "/api/asset") or plural == self._legacy():
            return self._send(404, {"message": "Not found"})
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, {"message": "kein JSON"})
        if not data.get("force") and not STATE["papierkorb"]:
            # Aeltere Fassungen ohne Papierkorb kennen force=false nicht
            return self._send(400, {"message": "force must be true"})
        STATE["geloescht"].extend(data.get("ids") or [])
        return self._send(204)

    # -- POST/PUT ------------------------------------------------------

    def do_POST(self):
        path = self._path()
        STATE["requests"].append(("POST", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})

        body = self._read_body()

        if path in ("/api/assets/bulk-upload-check", "/api/asset/bulk-upload-check"):
            plural = path.startswith("/api/assets")
            if plural == self._legacy():
                return self._send(404, {"message": "Not found"})
            data = json.loads(body)
            results = []
            for item in data["assets"]:
                known = STATE["assets"].get(item["checksum"])
                if known:
                    results.append({"id": item["id"], "action": "reject",
                                    "reason": "duplicate", "assetId": known})
                else:
                    results.append({"id": item["id"], "action": "accept"})
            return self._send(200, {"results": results})

        if path in ("/api/assets", "/api/asset/upload"):
            plural = path == "/api/assets"
            if plural == self._legacy():
                return self._send(404, {"message": "Not found"})
            return self._handle_upload(body)

        if path == "/api/search/metadata":
            data = json.loads(body)
            person = (data.get("personIds") or ["?"])[0]
            items = [{"id": f"asset-{person}-{i}"} for i in range(3)]
            return self._send(200, {"albums": {"items": [], "total": 0, "count": 0},
                                    "assets": {"items": items, "total": 3,
                                               "count": 3, "nextPage": None}})

        return self._send(404, {"message": "Not found"})

    def do_PUT(self):
        path = self._path()
        STATE["requests"].append(("PUT", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})
        self._read_body()
        if "/assets" in path or "/asset" in path:
            return self._send(200, [{"id": "x", "success": True}])
        return self._send(404, {"message": "Not found"})

    # -- Upload --------------------------------------------------------

    def _handle_upload(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        parser = email.parser.BytesParser()
        message = parser.parsebytes(
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        fields, files = {}, {}
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = part.get("Content-Disposition") or ""
            name = _param(disp, "name")
            filename = _param(disp, "filename")
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = (filename, payload)
            else:
                fields[name] = payload.decode("utf-8", "replace")

        if self._legacy():
            # Alte Server verlangen die Gerätefelder
            if "deviceAssetId" not in fields or "deviceId" not in fields:
                return self._send(400, {"message": "deviceAssetId should not be empty"})
        else:
            # Neue Server kennen sie nicht mehr und lehnen sie ab
            if "deviceAssetId" in fields or "deviceId" in fields:
                return self._send(400, {"message": "property deviceAssetId should not exist"})

        for required in ("fileCreatedAt", "fileModifiedAt"):
            if required not in fields:
                return self._send(400, {"message": f"{required} missing"})
        if "assetData" not in files:
            return self._send(400, {"message": "assetData missing"})

        blob = files["assetData"][1]
        checksum = base64.b64encode(hashlib.sha1(blob).digest()).decode()
        if checksum in STATE["assets"]:
            return self._send(200, {"id": STATE["assets"][checksum],
                                    "status": "duplicate", "duplicate": True})

        asset_id = f"asset-{len(STATE['assets']) + 1}"
        STATE["assets"][checksum] = asset_id
        STATE["uploads"].append((files["assetData"][0], "sidecarData" in files))
        return self._send(201, {"id": asset_id, "status": "created",
                                "duplicate": False})


def _param(header: str, key: str) -> str:
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if chunk.startswith(key + "="):
            return chunk[len(key) + 1:].strip('"')
    return ""


def start(port: int = 0):
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"
