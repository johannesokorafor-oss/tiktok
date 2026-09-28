"""A local fake of the documented Meta/Instagram endpoints, for tests only.

Speaks the real request/response shapes of:

  POST /oauth/access_token                     (api.instagram.com)
  GET  /access_token                           (graph.instagram.com)
  GET  /refresh_access_token                   (graph.instagram.com)
  GET  /{version}/me
  GET  /{version}/{ig-user-id}/content_publishing_limit
  POST /{version}/{ig-user-id}/media           (container / resumable session)
  POST /ig-api-upload/{version}/{container_id} (binary upload)
  GET  /{version}/{container_id}               (status_code)
  POST /{version}/{ig-user-id}/media_publish   -> records an ILLEGAL call

`publish_calls` must always stay empty: the application is upload-only.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List


class FakeInstagramState:
    def __init__(self) -> None:
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.publish_calls: List[dict] = []          # must stay empty
        self.requests: List[tuple[str, str, dict]] = []
        self.status_sequence: List[str] = ["IN_PROGRESS", "FINISHED"]
        self.next_container = 1
        self.fail_container_creation = False
        self.upload_failures = 0
        self.rate_limit_once = False
        self.account_type = "BUSINESS"

    def reset(self) -> None:
        self.__init__()


STATE = FakeInstagramState()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    # ------------------------------------------------------------------
    def _json(self, payload, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self) -> dict:
        query = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(raw.decode())
            except ValueError:
                return {}
        if "multipart/form-data" in ctype:
            # crude but sufficient for the fields the client sends
            out = {}
            for part in raw.split(b"--"):
                if b'name="' in part:
                    name = part.split(b'name="')[1].split(b'"')[0].decode()
                    value = part.split(b"\r\n\r\n", 1)[-1].rsplit(b"\r\n", 1)[0].decode(
                        "utf-8", "replace")
                    out[name] = value
            return out
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        params = self._params()
        STATE.requests.append(("GET", path, params))

        if path.endswith("/refresh_access_token"):
            if params.get("grant_type") != "ig_refresh_token":
                return self._json({"error": {"message": "bad grant_type", "code": 100}}, 400)
            return self._json({"access_token": "IGQrefreshed", "token_type": "bearer",
                               "expires_in": 5184000})
        if path.endswith("/access_token"):
            if params.get("grant_type") != "ig_exchange_token":
                return self._json({"error": {"message": "bad grant_type", "code": 100}}, 400)
            return self._json({"access_token": "IGQlonglived", "token_type": "bearer",
                               "expires_in": 5184000})
        if path.endswith("/me"):
            return self._json({"id": "17841400000000000", "username": "test_creator",
                               "account_type": STATE.account_type, "media_count": 12})
        if path.endswith("/content_publishing_limit"):
            return self._json({"data": [{"quota_usage": 3,
                                         "config": {"quota_total": 100,
                                                    "quota_duration": 86400}}]})
        # container status: /{version}/{container_id}
        container_id = path.rstrip("/").split("/")[-1]
        container = STATE.containers.get(container_id)
        if container is not None:
            if STATE.rate_limit_once:
                STATE.rate_limit_once = False
                return self._json({"error": {"message": "rate limited", "code": 4}}, 429)
            seq = container.setdefault("sequence", list(STATE.status_sequence))
            status = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else "FINISHED")
            container["status_code"] = status
            return self._json({"id": container_id, "status_code": status,
                               "status": f"fake status {status}"})
        return self._json({"error": {"message": f"unknown path {path}", "code": 100}}, 404)

    # ------------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        STATE.requests.append(("POST", path, form))

        if path.endswith("/oauth/access_token"):
            if form.get("grant_type") != "authorization_code":
                return self._json({"error_message": "bad grant_type"}, 400)
            return self._json({"data": [{"access_token": "IGQshortlived",
                                         "user_id": "17841400000000000",
                                         "permissions": "instagram_business_basic,"
                                                        "instagram_business_content_publish"}]})

        if path.endswith("/media_publish"):
            # the application must NEVER reach this endpoint
            STATE.publish_calls.append({"path": path, "form": form})
            return self._json({"id": "ILLEGAL_PUBLISH"})

        if path.endswith("/media"):
            if STATE.fail_container_creation:
                return self._json({"error": {"message": "Invalid parameter", "code": 100}}, 400)
            cid = f"1789{STATE.next_container:04d}"
            STATE.next_container += 1
            STATE.containers[cid] = {"form": form, "uploaded": 0,
                                     "sequence": list(STATE.status_sequence)}
            return self._json({"id": cid})

        if "/ig-api-upload/" in path:
            cid = path.rstrip("/").split("/")[-1]
            if STATE.upload_failures > 0:
                STATE.upload_failures -= 1
                return self._json({"error": {"message": "transient", "code": 2}}, 500)
            container = STATE.containers.setdefault(cid, {"sequence": list(STATE.status_sequence)})
            container["uploaded"] = int(self.headers.get("file_size", 0) or 0)
            container["auth_header"] = self.headers.get("Authorization", "")
            container["offset"] = self.headers.get("offset")
            return self._json({"success": True, "message": "Upload successful."})

        return self._json({"error": {"message": f"unknown path {path}", "code": 100}}, 404)


class FakeInstagramServer:
    """Context manager that starts the fake on a free localhost port."""

    def __init__(self) -> None:
        self.state = STATE
        self.state.reset()
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "FakeInstagramServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def configure(self, settings) -> None:
        """Point every Instagram host at this fake server."""
        settings.instagram_graph_base = self.base_url
        settings.instagram_facebook_graph_base = self.base_url
        settings.instagram_rupload_base = self.base_url
        settings.instagram_token_base = self.base_url
        settings.instagram_auth_base = self.base_url
