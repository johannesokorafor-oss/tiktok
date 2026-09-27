"""Local fake HTTP servers for the optional platform APIs (tests only).

Speaks the documented request/response shapes of Vimeo, Dailymotion,
SoundCloud and Patreon so the adapters can be exercised without network
access. Every server records the requests it received so tests can assert that
nothing forbidden (public visibility, Patreon v1, publish calls) ever happens.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List


class FakeState:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        # vimeo
        self.vimeo_videos: Dict[str, dict] = {}
        self.vimeo_offsets: Dict[str, int] = {}
        self.vimeo_break_after: int = 0      # bytes after which a PATCH fails once
        self.vimeo_broken: bool = False
        self.vimeo_privacy_override: str = ""
        self.vimeo_unauthorized: bool = False
        # dailymotion
        self.dm_videos: Dict[str, dict] = {}
        self.dm_token_fail: bool = False
        self.dm_visibility_override: str = ""
        # soundcloud
        self.sc_tracks: Dict[str, dict] = {}
        self.sc_token_calls: List[dict] = []
        self.sc_sharing_override: str = ""
        # patreon
        self.patreon_v1_calls: List[str] = []

    def record(self, method: str, path: str, body: Any = None, headers: Any = None) -> None:
        self.requests.append({"method": method, "path": path, "body": body,
                              "headers": dict(headers or {})})

    def paths(self) -> List[str]:
        return [r["path"] for r in self.requests]


STATE = FakeState()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------------
    def _send(self, payload, status: int = 200, headers: Dict[str, str] | None = None):
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._read()
        try:
            return json.loads(raw.decode())
        except Exception:
            return {"_raw_len": len(raw)}

    def _form_body(self) -> dict:
        raw = self._read()
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            out: Dict[str, Any] = {"_multipart": True, "_size": len(raw)}
            for part in raw.split(b"--"):
                if b'name="' in part:
                    name = part.split(b'name="')[1].split(b'"')[0].decode()
                    value = part.split(b"\r\n\r\n", 1)[-1].rsplit(b"\r\n", 1)[0]
                    out[name] = (f"<{len(value)} bytes>" if len(value) > 200
                                 else value.decode("utf-8", "replace"))
            return out
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

    # ------------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        STATE.record("GET", path, headers=self.headers)

        if "/api/oauth2/api/" in path:            # Patreon v1 - forbidden
            STATE.patreon_v1_calls.append(path)
            return self._send({"error": "v1 retired"}, 410)
        if path == "/me" and "vimeo" in self.headers.get("Authorization", "") + path:
            pass
        if path == "/me":
            if STATE.vimeo_unauthorized:
                return self._send({"error": "unauthorized"}, 401)
            auth = self.headers.get("Authorization", "")
            if auth.startswith("OAuth "):          # soundcloud /me
                return self._send({"id": 12345, "username": "test_user",
                                   "permalink_url": "https://soundcloud.com/test_user"})
            return self._send({"uri": "/users/1", "name": "Test Vimeo User",
                               "account": "basic",
                               "upload_quota": {"space": {"free": 5_000_000_000}}})
        if path.startswith("/videos/"):            # vimeo video read
            vid = path.split("/videos/")[1]
            video = STATE.vimeo_videos.get(vid)
            if not video:
                return self._send({"error": "not found"}, 404)
            if STATE.vimeo_privacy_override:
                video["privacy"]["view"] = STATE.vimeo_privacy_override
            return self._send(video)
        if path.startswith("/v2/videos/"):         # dailymotion video read
            vid = path.split("/v2/videos/")[1]
            video = STATE.dm_videos.get(vid, {})
            if STATE.dm_visibility_override:
                video = {**video, "visibility": STATE.dm_visibility_override}
            return self._send(video or {"error": "not found"}, 200 if video else 404)
        if path == "/identity":                    # patreon v2 identity
            return self._send({"data": {"id": "1", "type": "user",
                                        "attributes": {"full_name": "Test Creator",
                                                       "url": "https://patreon.com/test"}}})
        if path.startswith("/progress"):
            return self._send({"progress": 100})
        return self._send({"error": f"unknown GET {path}"}, 404)

    # ------------------------------------------------------------------ HEAD
    def do_HEAD(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        STATE.record("HEAD", path, headers=self.headers)
        if path.startswith("/tus/"):
            vid = path.split("/tus/")[1]
            offset = STATE.vimeo_offsets.get(vid, 0)
            length = (STATE.vimeo_videos.get(vid, {}).get("upload", {}) or {}).get("size", 0)
            self.send_response(200)
            self.send_header("Upload-Offset", str(offset))
            self.send_header("Upload-Length", str(length))
            self.send_header("Tus-Resumable", "1.0.0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------ PATCH
    def do_PATCH(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        raw = self._read()
        STATE.record("PATCH", path, body={"bytes": len(raw)}, headers=self.headers)
        if not path.startswith("/tus/"):
            return self._send({"error": "unknown"}, 404)
        vid = path.split("/tus/")[1]
        offset = int(self.headers.get("Upload-Offset", 0) or 0)
        size = (STATE.vimeo_videos.get(vid, {}).get("upload", {}) or {}).get("size", 0)
        if STATE.vimeo_break_after and not STATE.vimeo_broken and offset + len(raw) > STATE.vimeo_break_after:
            # simulate an interrupted transfer: accept only part of the chunk
            STATE.vimeo_broken = True
            accepted = max(0, STATE.vimeo_break_after - offset)
            STATE.vimeo_offsets[vid] = offset + accepted
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        new_offset = min(size, offset + len(raw)) if size else offset + len(raw)
        STATE.vimeo_offsets[vid] = new_offset
        self.send_response(204)
        self.send_header("Upload-Offset", str(new_offset))
        self.send_header("Tus-Resumable", "1.0.0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------ POST
    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if "/api/oauth2/api/" in path:             # Patreon v1 - forbidden
            STATE.patreon_v1_calls.append(path)
            STATE.record("POST", path)
            return self._send({"error": "v1 retired"}, 410)

        if path == "/me/videos":                   # vimeo create
            body = self._json_body()
            STATE.record("POST", path, body=body, headers=self.headers)
            vid = str(1000 + len(STATE.vimeo_videos))
            size = int(body.get("upload", {}).get("size", 0) or 0)
            video = {
                "uri": f"/videos/{vid}",
                "link": f"https://vimeo.com/{vid}",
                "name": body.get("name"),
                "privacy": dict(body.get("privacy") or {"view": "nobody"}),
                "status": "uploading",
                "transcode": {"status": "complete"},
                "upload": {"approach": "tus", "size": size,
                           "upload_link": f"http://127.0.0.1:{self.server.server_port}/tus/{vid}",
                           "status": "in_progress"},
            }
            STATE.vimeo_videos[vid] = video
            STATE.vimeo_offsets[vid] = 0
            return self._send(video)

        if path == "/oauth/token":                 # dailymotion token
            body = self._form_body()
            STATE.record("POST", path, body=body)
            if STATE.dm_token_fail:
                return self._send({"error": "invalid_client"}, 401)
            return self._send({"access_token": "dm-token", "expires_in": 3600,
                               "scope": body.get("scope", "")})

        if path == "/v2/files/upload_sessions":    # dailymotion upload session
            STATE.record("POST", path, headers=self.headers)
            port = self.server.server_port
            return self._send({"upload_url": f"http://127.0.0.1:{port}/upload?uuid=abc",
                               "progress_url": f"http://127.0.0.1:{port}/progress?uuid=abc"})

        if path == "/upload":                      # dailymotion file upload
            body = self._form_body()
            STATE.record("POST", path, body=body)
            return self._send({"url": "http://upload-01.dailymotion.test/files/xyz.mp4",
                               "name": "xyz.mp4"})

        if "/v2/profiles/" in path and path.endswith("/videos"):   # dailymotion create video
            body = self._json_body()
            STATE.record("POST", path, body=body, headers=self.headers)
            if body.get("visibility") == "public":
                return self._send({"error": "public refused by fake"}, 400)
            vid = f"x{len(STATE.dm_videos) + 1}"
            video = {"id": vid, "title": body.get("title"),
                     "visibility": body.get("visibility"), "status": "processing",
                     "url": f"https://www.dailymotion.com/video/{vid}", "private": True}
            STATE.dm_videos[vid] = video
            return self._send(video)

        if path == "/oauth/token/soundcloud" or path == "/oauth/token2":
            return self._send({"error": "unused"}, 404)

        if path.endswith("/oauth/token") and path != "/oauth/token":   # soundcloud token
            body = self._form_body()
            STATE.sc_token_calls.append(body)
            STATE.record("POST", path, body=body)
            return self._send({"access_token": "sc-access", "refresh_token": "sc-refresh-new",
                               "expires_in": 3600, "scope": ""})

        if path == "/tracks":                      # soundcloud upload
            body = self._form_body()
            STATE.record("POST", path, body=body, headers=self.headers)
            sharing = body.get("track[sharing]", "")
            if STATE.sc_sharing_override:
                sharing = STATE.sc_sharing_override
            tid = 900 + len(STATE.sc_tracks)
            track = {"id": tid, "urn": f"soundcloud:tracks:{tid}",
                     "title": body.get("track[title]"), "sharing": sharing or "public",
                     "permalink_url": f"https://soundcloud.com/test_user/{tid}",
                     "state": "processing"}
            STATE.sc_tracks[str(tid)] = track
            return self._send(track, 201)

        body = self._json_body()
        STATE.record("POST", path, body=body)
        return self._send({"error": f"unknown POST {path}"}, 404)


class FakePlatformServer:
    def __init__(self) -> None:
        global STATE
        STATE = FakeState()
        self.state = STATE
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "FakePlatformServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def configure(self, settings) -> None:
        """Point every optional platform at this fake server."""
        settings.vimeo_api_base = self.base_url
        settings.vimeo_auth_base = self.base_url
        settings.dailymotion_api_base = self.base_url
        settings.soundcloud_api_base = self.base_url
        settings.soundcloud_auth_base = self.base_url + "/sc"
        settings.patreon_api_base = self.base_url
        settings.patreon_oauth_base = self.base_url
