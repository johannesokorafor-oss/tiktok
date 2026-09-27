"""Instagram OAuth - "Business Login for Instagram" (Instagram API with Instagram Login).

Documented endpoints used (Meta for Developers, verified 2026-09-27):

  authorize        GET  https://www.instagram.com/oauth/authorize
                        ?client_id=&redirect_uri=&response_type=code&scope=&state=
  short-lived      POST https://api.instagram.com/oauth/access_token
                        (multipart/form: client_id, client_secret,
                         grant_type=authorization_code, redirect_uri, code)
                        -> {"data":[{"access_token","user_id","permissions"}]}
                        (Meta also returns the flat form; both are handled)
  long-lived       GET  https://graph.instagram.com/access_token
                        ?grant_type=ig_exchange_token&client_secret=&access_token=
                        -> 60 day token
  refresh          GET  https://graph.instagram.com/refresh_access_token
                        ?grant_type=ig_refresh_token&access_token=
                        Preconditions documented by Meta: the long-lived token
                        must be **at least 24 hours old** and not expired.
                        There is no grace period - an expired token cannot be
                        refreshed and the user must authorise again.

Scopes: ``instagram_business_basic`` and ``instagram_business_content_publish``
(configurable via INSTAGRAM_SCOPES). Requires an Instagram **Professional**
account (Business or Creator); personal accounts cannot use this API.

Tokens are stored separately from TikTok tokens and are never logged.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Optional

import httpx

from app.config import InstagramLoginMode, Settings
from app.tiktok.oauth import StateStore          # reuse the CSRF state helper
from app.tiktok.tokens import TokenStore

log = logging.getLogger(__name__)

#: a long-lived token may only be refreshed once it is this old (Meta rule)
MIN_REFRESH_AGE_SECONDS = 24 * 3600


class InstagramOAuthError(RuntimeError):
    pass


@dataclass
class InstagramTokens:
    access_token: str = ""
    user_id: str = ""
    permissions: str = ""
    #: epoch seconds
    issued_at: float = 0.0
    expires_at: float = 0.0
    long_lived: bool = False

    @property
    def has_access(self) -> bool:
        return bool(self.access_token)

    def is_expired(self, skew: float = 3600.0) -> bool:
        return (not self.access_token) or (self.expires_at > 0
                                           and time.time() >= self.expires_at - skew)

    def refreshable(self) -> bool:
        """Meta: long-lived, at least 24 h old, not yet expired."""
        if not (self.long_lived and self.access_token):
            return False
        if self.expires_at and time.time() >= self.expires_at:
            return False
        return (time.time() - self.issued_at) >= MIN_REFRESH_AGE_SECONDS

    def public_dict(self) -> dict:
        return {
            "authenticated": self.has_access,
            "user_id": self.user_id,
            "permissions": self.permissions,
            "long_lived": self.long_lived,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())) if self.expires_at else 0,
            "expired": self.is_expired(skew=0),
            "refreshable_now": self.refreshable(),
        }


class InstagramTokenStore(TokenStore):
    """Same on-disk handling as the TikTok store, different file and shape."""

    def load(self) -> InstagramTokens:  # type: ignore[override]
        import json
        import os
        with self._lock:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    base = InstagramTokens()
                    return InstagramTokens(**{k: data.get(k, getattr(base, k))
                                              for k in base.__dict__})
                except (json.JSONDecodeError, TypeError):
                    pass
            env = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
            if env:
                return InstagramTokens(access_token=env,
                                       user_id=os.getenv("INSTAGRAM_USER_ID", ""),
                                       issued_at=time.time(), long_lived=True,
                                       expires_at=time.time() + 60 * 86400)
            return InstagramTokens()

    def save(self, tokens: InstagramTokens) -> None:  # type: ignore[override]
        import json
        import os
        import stat
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


@dataclass
class AuthUrl:
    url: str
    state: str


class InstagramOAuth:
    def __init__(self, settings: Settings, store: Optional[InstagramTokenStore] = None,
                 state_store: Optional[StateStore] = None) -> None:
        self.settings = settings
        self.store = store or InstagramTokenStore(settings.state_dir / "instagram_tokens.json")
        self.states = state_store or StateStore()

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.settings.instagram_client_id and self.settings.instagram_client_secret)

    @property
    def scopes(self) -> list[str]:
        return [s.strip() for s in self.settings.instagram_scopes.split(",") if s.strip()]

    def missing_scopes(self, granted: str = "") -> list[str]:
        have = {s.strip() for s in (granted or "").split(",") if s.strip()}
        return [s for s in self.scopes if s not in have] if have else []

    def authorize_url(self) -> AuthUrl:
        if not self.settings.instagram_client_id:
            raise InstagramOAuthError("INSTAGRAM_CLIENT_ID is not configured")
        state = self.states.issue()
        params = {
            "client_id": self.settings.instagram_client_id,
            "redirect_uri": self.settings.instagram_redirect_uri,
            "response_type": "code",
            "scope": ",".join(self.scopes),
            "state": state,
        }
        base = self.settings.instagram_auth_base.rstrip("/") + "/oauth/authorize"
        return AuthUrl(url=base + "?" + urllib.parse.urlencode(params), state=state)

    # ------------------------------------------------------------------
    def _short_lived(self, code: str) -> dict:
        url = self.settings.instagram_token_base.rstrip("/") + "/oauth/access_token"
        data = {
            "client_id": self.settings.instagram_client_id or "",
            "client_secret": self.settings.instagram_client_secret or "",
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.instagram_redirect_uri,
            "code": urllib.parse.unquote(code),
        }
        try:
            with httpx.Client(timeout=30.0) as c:
                resp = c.post(url, data=data)
                body = resp.json()
        except httpx.HTTPError as exc:
            raise InstagramOAuthError(f"token request failed: {exc}") from exc
        except ValueError:
            raise InstagramOAuthError("Instagram token endpoint returned non-JSON")
        if isinstance(body, dict) and body.get("data"):
            body = body["data"][0]
        if not isinstance(body, dict) or not body.get("access_token"):
            raise InstagramOAuthError(f"Instagram OAuth error: {str(body)[:300]}")
        return body

    def _exchange_long_lived(self, short_token: str) -> dict:
        url = self.settings.instagram_graph_base.rstrip("/") + "/access_token"
        params = {"grant_type": "ig_exchange_token",
                  "client_secret": self.settings.instagram_client_secret or "",
                  "access_token": short_token}
        try:
            with httpx.Client(timeout=30.0) as c:
                resp = c.get(url, params=params)
                body = resp.json()
        except httpx.HTTPError as exc:
            raise InstagramOAuthError(f"long-lived token exchange failed: {exc}") from exc
        except ValueError:
            raise InstagramOAuthError("long-lived token endpoint returned non-JSON")
        if not body.get("access_token"):
            raise InstagramOAuthError(f"long-lived exchange failed: {str(body)[:300]}")
        return body

    def exchange_code(self, code: str, state: Optional[str] = None,
                      *, verify_state: bool = True) -> InstagramTokens:
        if verify_state and not self.states.validate(state):
            raise InstagramOAuthError(
                "Instagram OAuth state validation failed (possible CSRF or expired link)")
        short = self._short_lived(code)
        tokens = InstagramTokens(
            access_token=short["access_token"],
            user_id=str(short.get("user_id", "")),
            permissions=short.get("permissions", ",".join(self.scopes)),
            issued_at=time.time(),
            expires_at=time.time() + 3600,
            long_lived=False,
        )
        try:
            longlived = self._exchange_long_lived(tokens.access_token)
            tokens.access_token = longlived["access_token"]
            tokens.expires_at = time.time() + float(longlived.get("expires_in", 60 * 86400))
            tokens.long_lived = True
        except InstagramOAuthError as exc:
            # keep the short-lived token so the user can still act, but say so
            log.warning("Instagram long-lived exchange failed: %s", exc)
        self.store.save(tokens)
        return tokens

    def refresh(self, tokens: Optional[InstagramTokens] = None) -> InstagramTokens:
        tokens = tokens or self.store.load()
        if not tokens.access_token:
            raise InstagramOAuthError("no Instagram token stored - connect the account first")
        if not tokens.long_lived:
            raise InstagramOAuthError(
                "only long-lived Instagram tokens can be refreshed - reconnect the account")
        age = time.time() - tokens.issued_at
        if age < MIN_REFRESH_AGE_SECONDS:
            raise InstagramOAuthError(
                f"Meta requires the long-lived token to be at least 24 h old before it can be "
                f"refreshed (currently {age / 3600:.1f} h)")
        if tokens.expires_at and time.time() >= tokens.expires_at:
            raise InstagramOAuthError(
                "the Instagram long-lived token has expired and cannot be refreshed - "
                "reconnect the account")
        url = self.settings.instagram_graph_base.rstrip("/") + "/refresh_access_token"
        try:
            with httpx.Client(timeout=30.0) as c:
                resp = c.get(url, params={"grant_type": "ig_refresh_token",
                                          "access_token": tokens.access_token})
                body = resp.json()
        except httpx.HTTPError as exc:
            raise InstagramOAuthError(f"refresh failed: {exc}") from exc
        except ValueError:
            raise InstagramOAuthError("refresh endpoint returned non-JSON")
        if not body.get("access_token"):
            raise InstagramOAuthError(f"refresh failed: {str(body)[:300]}")
        tokens.access_token = body["access_token"]
        tokens.issued_at = time.time()
        tokens.expires_at = time.time() + float(body.get("expires_in", 60 * 86400))
        tokens.long_lived = True
        self.store.save(tokens)
        return tokens

    def valid_tokens(self) -> InstagramTokens:
        tokens = self.store.load()
        if not tokens.has_access:
            raise InstagramOAuthError(
                "Instagram is not connected - open the dashboard and click Connect Instagram")
        if tokens.is_expired() and tokens.refreshable():
            tokens = self.refresh(tokens)
        elif tokens.is_expired(skew=0):
            raise InstagramOAuthError(
                "the Instagram access token has expired - reconnect the account")
        return tokens

    def logout(self) -> None:
        self.store.clear()

    def status(self) -> dict:
        tokens = self.store.load()
        info = tokens.public_dict()
        info.update({
            "enabled": self.settings.instagram_enabled,
            "client_configured": self.is_configured(),
            "login_mode": self.settings.instagram_login_mode.value,
            "redirect_uri": self.settings.instagram_redirect_uri,
            "scopes": self.scopes,
            "missing_scopes": self.missing_scopes(tokens.permissions),
            "resumable_supported": (self.settings.instagram_login_mode
                                    == InstagramLoginMode.FACEBOOK_LOGIN),
        })
        return info
