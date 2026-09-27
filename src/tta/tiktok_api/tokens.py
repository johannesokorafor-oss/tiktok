"""Secure local token storage.

Tokens are stored in a local JSON file (default ``data/tiktok_tokens.json``)
with 0600 permissions where the OS supports it.  Tokens are never logged.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path


class TokenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def load(self) -> dict:
        with self._lock:
            if not self.path.is_file():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}

    def save(self, tokens: dict) -> None:
        """Persist token payload; adds absolute expiry timestamps.

        If the new payload lacks a refresh_token (some refresh responses may
        omit it), the previously stored one is preserved so the user is not
        silently logged out.
        """
        now = time.time()
        data = dict(tokens)
        existing = self.load()
        if not data.get("refresh_token") and existing.get("refresh_token"):
            data["refresh_token"] = existing["refresh_token"]
            if "refresh_expires_at" in existing and "refresh_expires_in" not in data:
                data["refresh_expires_at"] = existing["refresh_expires_at"]
        if "expires_in" in data and "expires_at" not in data:
            data["expires_at"] = now + float(data["expires_in"])
        if "refresh_expires_in" in data and "refresh_expires_at" not in data:
            data["refresh_expires_at"] = now + float(data["refresh_expires_in"])
        data["saved_at"] = now
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass  # e.g. some Windows filesystems

    def clear(self) -> None:
        with self._lock:
            if self.path.is_file():
                self.path.unlink()

    # ------------------------------------------------------------------
    @property
    def access_token(self) -> str:
        return self.load().get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self.load().get("refresh_token", "")

    def is_access_valid(self, margin_seconds: float = 120.0) -> bool:
        data = self.load()
        if not data.get("access_token"):
            return False
        expires_at = data.get("expires_at")
        if expires_at is None:
            return True
        return time.time() < float(expires_at) - margin_seconds

    def is_refresh_valid(self, margin_seconds: float = 3600.0) -> bool:
        data = self.load()
        if not data.get("refresh_token"):
            return False
        expires_at = data.get("refresh_expires_at")
        if expires_at is None:
            return True
        return time.time() < float(expires_at) - margin_seconds

    def status(self) -> dict:
        """Safe (non-secret) summary for diagnostics/dashboard."""
        data = self.load()
        return {
            "authenticated": bool(data.get("access_token")),
            "access_valid": self.is_access_valid(),
            "refresh_valid": self.is_refresh_valid(),
            "open_id": data.get("open_id", ""),
            "scope": data.get("scope", ""),
            "expires_at": data.get("expires_at"),
        }
