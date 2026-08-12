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
    # Inhalt je Album - so laesst sich pruefen, was Wimmich wirklich schiebt
    "album_inhalt": {"alb-1": ["asset-alb-1-0", "asset-alb-1-1"], "alb-2": []},
    "people": [{"id": "per-1", "name": "Anna Beispiel", "isHidden": False},
               {"id": "per-2", "name": "", "isHidden": False}],
    "requests": [],
    "api_key": "geheim",
    # Bilddaten je Kennung: {"thumb": bytes, "original": bytes, "name": str}
    "bilder": {},
    "geloescht": [],     # Kennungen, die ueber DELETE weggeraeumt wurden
    "papierkorb": True,  # False = Server kennt force=false nicht (HTTP 400)
    # Serversuche: Kennung -> Ort, und die Antwort der klugen Suche
    "orte": {"asset-o-1": "Cannes", "asset-o-2": "Cannes",
             "asset-o-3": "Berlin"},
    "kluge_treffer": ["asset-k-1", "asset-k-2"],
    "vorschlaege_fehlen": False,   # True = Server kennt den Endpunkt nicht
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

        if path == "/api/search/suggestions":
            if STATE["vorschlaege_fehlen"]:
                return self._send(404, {"message": "Not found"})
            typ = _query(self.path).get("type", [""])[0]
            if typ != "city":
                return self._send(200, [])
            # Reihenfolge wie bei Immich: alphabetisch, ohne Doppelte
            return self._send(200, sorted(set(STATE["orte"].values())))

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
                if album_id not in STATE["album_inhalt"]:
                    return self._send(404, {"message": "Not found"})
                assets = [{"id": i} for i in STATE["album_inhalt"][album_id]]
                name = next((a["albumName"] for a in STATE["albums"]
                             if a["id"] == album_id), "")
                return self._send(200, {"id": album_id, "albumName": name,
                                        "assets": assets})

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

        album_id = self._album_aus_pfad(path)
        if album_id:
            data = json.loads(body or b"{}")
            inhalt = STATE["album_inhalt"].setdefault(album_id, [])
            antwort = []
            for kennung in data.get("ids") or []:
                drin = kennung in inhalt
                if drin:
                    inhalt.remove(kennung)
                antwort.append({"id": kennung, "success": drin})
            return self._send(200, antwort)

        for prefix in ("/api/albums/", "/api/album/"):
            if path.startswith(prefix):
                weg = path[len(prefix):]
                STATE["albums"] = [a for a in STATE["albums"] if a["id"] != weg]
                STATE["album_inhalt"].pop(weg, None)
                return self._send(204)

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

        if path in ("/api/albums", "/api/album"):
            plural = path == "/api/albums"
            if plural == self._legacy():
                return self._send(404, {"message": "Not found"})
            data = json.loads(body or b"{}")
            album_id = f"alb-{len(STATE['albums']) + 1}"
            STATE["albums"].append({"id": album_id,
                                    "albumName": data.get("albumName", ""),
                                    "assetCount": len(data.get("assetIds") or [])})
            STATE["album_inhalt"][album_id] = list(data.get("assetIds") or [])
            return self._send(201, {"id": album_id,
                                    "albumName": data.get("albumName", "")})

        if path == "/api/search/metadata":
            data = json.loads(body)
            if data.get("city"):
                items = [{"id": k} for k, ort in STATE["orte"].items()
                         if ort == data["city"]]
            else:
                person = (data.get("personIds") or ["?"])[0]
                items = [{"id": f"asset-{person}-{i}"} for i in range(3)]
            return self._send(200, {"albums": {"items": [], "total": 0, "count": 0},
                                    "assets": {"items": items, "total": len(items),
                                               "count": len(items),
                                               "nextPage": None}})

        if path == "/api/search/smart":
            data = json.loads(body)
            if not data.get("query"):
                return self._send(400, {"message": "query missing"})
            items = [{"id": k} for k in STATE["kluge_treffer"]]
            return self._send(200, {"albums": {"items": [], "total": 0, "count": 0},
                                    "assets": {"items": items, "total": len(items),
                                               "count": len(items),
                                               "nextPage": None}})

        return self._send(404, {"message": "Not found"})

    def do_PUT(self):
        path = self._path()
        STATE["requests"].append(("PUT", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})
        body = self._read_body()
        album_id = self._album_aus_pfad(path)
        if album_id:
            data = json.loads(body or b"{}")
            inhalt = STATE["album_inhalt"].setdefault(album_id, [])
            antwort = []
            for kennung in data.get("ids") or []:
                neu = kennung not in inhalt
                if neu:
                    inhalt.append(kennung)
                antwort.append({"id": kennung, "success": neu})
            return self._send(200, antwort)
        if "/assets" in path or "/asset" in path:
            return self._send(200, [{"id": "x", "success": True}])
        return self._send(404, {"message": "Not found"})

    def do_PATCH(self):
        path = self._path()
        STATE["requests"].append(("PATCH", path))
        if not self._auth_ok():
            return self._send(401, {"message": "Invalid API key"})
        data = json.loads(self._read_body() or b"{}")
        for prefix in ("/api/albums/", "/api/album/"):
            if path.startswith(prefix):
                album_id = path[len(prefix):]
                for album in STATE["albums"]:
                    if album["id"] == album_id:
                        album["albumName"] = data.get("albumName",
                                                      album["albumName"])
                        return self._send(200, album)
                return self._send(404, {"message": "Not found"})
        return self._send(404, {"message": "Not found"})

    def _album_aus_pfad(self, path: str) -> str:
        """„/api/albums/<id>/assets" -> <id>, sonst leer."""
        for prefix in ("/api/albums/", "/api/album/"):
            if path.startswith(prefix) and path.endswith("/assets"):
                return path[len(prefix):-len("/assets")]
        return ""

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


def _query(pfad: str) -> dict:
    """Abfrageteil einer URL als Woerterbuch."""
    import urllib.parse
    return urllib.parse.parse_qs(urllib.parse.urlparse(pfad).query)


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
