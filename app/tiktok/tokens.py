"""Local token storage for TikTok OAuth credentials.

Tokens are written to state/tiktok_tokens.json with restrictive permissions
and are never logged.  Values may also be seeded from the environment
(TIKTOK_ACCESS_TOKEN / TIKTOK_REFRESH_TOKEN) for headless setups.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    open_id: str = ""
    scope: str = ""
    expires_at: float = 0.0            # epoch seconds
    refresh_expires_at: float = 0.0

    @property
    def has_access(self) -> bool:
        return bool(self.access_token)

    def is_expired(self, skew: float = 300.0) -> bool:
        return (not self.access_token) or time.time() >= (self.expires_at - skew)

    def refresh_valid(self) -> bool:
        return bool(self.refresh_token) and (self.refresh_expires_at == 0
                                             or time.time() < self.refresh_expires_at)

    def public_dict(self) -> dict:
        """Safe for the dashboard/API: never exposes token material."""
        return {
            "authenticated": self.has_access,
            "open_id": (self.open_id[:6] + "…") if self.open_id else "",
            "scope": self.scope,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())) if self.expires_at else 0,
            "access_token_expired": self.is_expired(skew=0),
            "refresh_token_present": bool(self.refresh_token),
        }


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> TokenSet:
        with self._lock:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    return TokenSet(**{k: data.get(k, getattr(TokenSet(), k))
                                       for k in TokenSet().__dict__})
                except (json.JSONDecodeError, TypeError):
                    pass
            env_access = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
            env_refresh = os.getenv("TIKTOK_REFRESH_TOKEN", "").strip()
            if env_access or env_refresh:
                # environment-seeded tokens: assume near expiry so they get refreshed
                return TokenSet(access_token=env_access, refresh_token=env_refresh,
                                open_id=os.getenv("TIKTOK_OPEN_ID", ""),
                                scope=os.getenv("TIKTOK_SCOPES", ""),
                                expires_at=time.time() + 60 if env_access else 0.0)
            return TokenSet()

    def save(self, tokens: TokenSet) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows/ACL differences are non-fatal

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()
