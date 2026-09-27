"""Official TikTok OAuth 2.0 flow (authorization code).

Endpoints per https://developers.tiktok.com/doc/oauth-user-access-token-management:

* authorize:  https://www.tiktok.com/v2/auth/authorize/
* token:      https://open.tiktokapis.com/v2/oauth/token/   (form-encoded)
* revoke:     https://open.tiktokapis.com/v2/oauth/revoke/

`run_local_auth_flow` spins up a temporary local HTTP server on the
redirect URI, opens the browser, captures the ``code`` and exchanges it.
"""

from __future__ import annotations

import http.server
import logging
import secrets
import threading
import urllib.parse
import webbrowser

import requests

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"

DEFAULT_SCOPES = "user.info.basic,video.upload"

log = logging.getLogger("tta.oauth")


class OAuthError(RuntimeError):
    pass


def make_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(client_key: str, redirect_uri: str,
                        scopes: str = DEFAULT_SCOPES, state: str | None = None
                        ) -> tuple[str, str]:
    """Return (url, state)."""
    state = state or make_state()
    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state


def exchange_code(client_key: str, client_secret: str, code: str,
                  redirect_uri: str, session: requests.Session | None = None) -> dict:
    session = session or requests.Session()
    resp = session.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    payload = _parse_token_response(resp)
    return payload


def refresh_access_token(client_key: str, client_secret: str, refresh_token: str,
                         session: requests.Session | None = None) -> dict:
    session = session or requests.Session()
    resp = session.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return _parse_token_response(resp)


def revoke_token(client_key: str, client_secret: str, token: str,
                 session: requests.Session | None = None) -> None:
    session = session or requests.Session()
    session.post(
        REVOKE_URL,
        data={"client_key": client_key, "client_secret": client_secret,
              "token": token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )


def _parse_token_response(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OAuthError(f"token endpoint returned non-JSON (HTTP {resp.status_code})") from exc
    if not resp.ok or payload.get("error"):
        # never include token material in the error
        err = payload.get("error", f"http_{resp.status_code}")
        desc = payload.get("error_description", "")
        raise OAuthError(f"token request failed: {err} {desc}".strip())
    if "access_token" not in payload:
        raise OAuthError("token response missing access_token")
    return payload


# ----------------------------------------------------------------------
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "tta-oauth"
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in type(self).result
        body = (
            "<html><body style='font-family:sans-serif'>"
            + ("<h2>TikTok authorization received.</h2>You can close this tab."
               if ok else "<h2>Authorization failed.</h2>Check the terminal.")
            + "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence, avoid leaking query strings
        pass


def run_local_auth_flow(client_key: str, client_secret: str, redirect_uri: str,
                        scopes: str = DEFAULT_SCOPES, timeout: float = 300.0,
                        open_browser: bool = True) -> dict:
    """Interactive local flow: local HTTP listener + browser."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    url, state = build_authorize_url(client_key, redirect_uri, scopes)
    _CallbackHandler.result = {}
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    server.timeout = 1.0

    print("\nOpen this URL in your browser to authorize the app:\n")
    print(url + "\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import time as _time
    deadline = _time.monotonic() + timeout
    try:
        while _time.monotonic() < deadline and "code" not in _CallbackHandler.result \
                and "error" not in _CallbackHandler.result:
            server.handle_request()
    finally:
        server.server_close()

    result = _CallbackHandler.result
    if "error" in result:
        raise OAuthError(f"authorization denied: {result.get('error_description') or result['error']}")
    if "code" not in result:
        raise OAuthError("timed out waiting for the OAuth redirect")
    if result.get("state") != state:
        raise OAuthError("OAuth state mismatch - possible CSRF, aborting")
    return exchange_code(client_key, client_secret, result["code"], redirect_uri)
