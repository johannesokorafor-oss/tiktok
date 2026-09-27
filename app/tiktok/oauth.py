"""TikTok OAuth 2.0 (v2) authorization-code flow.

Documented endpoints used (TikTok for Developers, verified 2026-09-26):
  * authorize: https://www.tiktok.com/v2/auth/authorize/
        query: client_key, scope, response_type=code, redirect_uri, state
  * token:     POST https://open.tiktokapis.com/v2/oauth/token/
        form:  client_key, client_secret, code, grant_type=authorization_code,
               redirect_uri  (or grant_type=refresh_token + refresh_token)
  * revoke:    POST https://open.tiktokapis.com/v2/oauth/revoke/

Access tokens live ~24 h; refresh tokens ~365 d and TikTok rotates the
refresh token on every refresh, so the new value is always persisted.
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx

from app.config import Settings
from app.tiktok.tokens import TokenSet, TokenStore


class OAuthError(RuntimeError):
    pass


class StateStore:
    """CSRF state values for the local auth callback (single process, in memory)."""

    def __init__(self, ttl: float = 600.0) -> None:
        self._states: dict[str, float] = {}
        self.ttl = ttl

    def issue(self) -> str:
        self._gc()
        state = secrets.token_urlsafe(24)
        self._states[state] = time.time() + self.ttl
        return state

    def validate(self, state: Optional[str]) -> bool:
        self._gc()
        if not state:
            return False
        exp = self._states.pop(state, None)
        return exp is not None and exp >= time.time()

    def _gc(self) -> None:
        now = time.time()
        for k, v in list(self._states.items()):
            if v < now:
                self._states.pop(k, None)


@dataclass
class AuthUrl:
    url: str
    state: str


class TikTokOAuth:
    def __init__(self, settings: Settings, store: Optional[TokenStore] = None,
                 state_store: Optional[StateStore] = None) -> None:
        self.settings = settings
        self.store = store or TokenStore(settings.token_path)
        self.states = state_store or StateStore()

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.settings.tiktok_client_key and self.settings.tiktok_client_secret)

    def authorize_url(self) -> AuthUrl:
        if not self.settings.tiktok_client_key:
            raise OAuthError("TIKTOK_CLIENT_KEY is not configured")
        state = self.states.issue()
        params = {
            "client_key": self.settings.tiktok_client_key,
            "scope": self.settings.tiktok_scopes,
            "response_type": "code",
            "redirect_uri": self.settings.tiktok_redirect_uri,
            "state": state,
        }
        base = self.settings.tiktok_auth_base.rstrip("/") + "/v2/auth/authorize/"
        return AuthUrl(url=base + "?" + urllib.parse.urlencode(params), state=state)

    # ------------------------------------------------------------------
    def _token_request(self, data: dict) -> dict:
        url = self.settings.tiktok_api_base.rstrip("/") + "/v2/oauth/token/"
        payload = {
            "client_key": self.settings.tiktok_client_key or "",
            "client_secret": self.settings.tiktok_client_secret or "",
            **data,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Cache-Control": "no-cache"}
        try:
            with httpx.Client(timeout=30.0) as c:
                resp = c.post(url, data=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthError(f"token request failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise OAuthError(f"token endpoint returned non-JSON (HTTP {resp.status_code})")
        if resp.status_code >= 400 or body.get("error"):
            raise OAuthError(
                f"TikTok OAuth error: {body.get('error', resp.status_code)} - "
                f"{body.get('error_description', '')}".strip())
        return body

    def _persist(self, body: dict, previous: Optional[TokenSet] = None) -> TokenSet:
        now = time.time()
        tokens = TokenSet(
            access_token=body.get("access_token", ""),
            refresh_token=body.get("refresh_token", "") or (previous.refresh_token if previous else ""),
            open_id=body.get("open_id", "") or (previous.open_id if previous else ""),
            scope=body.get("scope", "") or (previous.scope if previous else ""),
            expires_at=now + float(body.get("expires_in", 86400)),
            refresh_expires_at=now + float(body.get("refresh_expires_in", 365 * 86400)),
        )
        if not tokens.access_token:
            raise OAuthError("TikTok did not return an access_token")
        self.store.save(tokens)
        return tokens

    # ------------------------------------------------------------------
    def exchange_code(self, code: str, state: Optional[str] = None,
                      *, verify_state: bool = True) -> TokenSet:
        if verify_state and not self.states.validate(state):
            raise OAuthError("OAuth state validation failed (possible CSRF or expired link)")
        body = self._token_request({
            "code": urllib.parse.unquote(code),
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.tiktok_redirect_uri,
        })
        return self._persist(body)

    def refresh(self, tokens: Optional[TokenSet] = None) -> TokenSet:
        tokens = tokens or self.store.load()
        if not tokens.refresh_token:
            raise OAuthError("no refresh token stored - run the authentication flow again")
        body = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        })
        return self._persist(body, previous=tokens)

    def valid_tokens(self) -> TokenSet:
        """Return a non-expired token set, refreshing if needed."""
        tokens = self.store.load()
        if not tokens.has_access and not tokens.refresh_token:
            raise OAuthError("not authenticated with TikTok - open the dashboard and connect")
        if tokens.is_expired():
            if not tokens.refresh_valid():
                raise OAuthError("TikTok refresh token expired - re-authentication required")
            tokens = self.refresh(tokens)
        return tokens

    def revoke(self) -> bool:
        tokens = self.store.load()
        if not tokens.access_token:
            return False
        url = self.settings.tiktok_api_base.rstrip("/") + "/v2/oauth/revoke/"
        try:
            with httpx.Client(timeout=20.0) as c:
                c.post(url, data={"client_key": self.settings.tiktok_client_key or "",
                                  "client_secret": self.settings.tiktok_client_secret or "",
                                  "token": tokens.access_token},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError:
            pass
        self.store.clear()
        return True

    def status(self) -> dict:
        tokens = self.store.load()
        info = tokens.public_dict()
        info["client_configured"] = self.is_configured()
        info["redirect_uri"] = self.settings.tiktok_redirect_uri
        info["scopes"] = self.settings.tiktok_scopes
        return info
