"""Patreon - PREPARE_ONLY (API v2 documents no post creation for third parties).

Verified against the current official documentation (docs.patreon.com,
checked 2026-09-27):

* **API v1 is retiring on 7 October 2026** (extensions until 20 January 2027).
  New v1 clients can no longer be created. This application therefore uses
  **API v2 only** and never calls a ``/api/oauth2/api/...`` (v1) endpoint.
* The documented v2 endpoints are:
  ``GET /api/oauth2/v2/identity``, ``GET /api/oauth2/v2/campaigns``,
  ``GET /api/oauth2/v2/campaigns/{id}``, ``GET .../members``,
  ``GET /api/oauth2/v2/members/{id}``, ``GET .../campaigns/{id}/posts``,
  ``GET /api/oauth2/v2/posts/{id}`` and ``POST /api/oauth2/v2/lives``.
* There is **no documented endpoint to create a post or upload post media**.
  The ``Media`` resource is described with read attributes only.

Consequently this provider prepares a complete upload package for the Patreon
post editor and never pretends to upload. If Patreon documents post creation
later, only :meth:`PatreonProvider.detect_post_creation_support` and an
``upload()`` implementation need to change.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import PlatformMode, PlatformStatus
from app.platforms.base import (PlatformAuthError, PlatformDiagnosis, PlatformError,
                                PlatformProvider, PlatformResult)
from app.platforms.validate import validate_patreon

log = logging.getLogger(__name__)

#: v1 path fragments that must never appear in a request from this app
V1_FORBIDDEN = ("/api/oauth2/api/", "api.patreon.com/oauth2/api")

POST_CREATION_SUPPORTED = False
POST_CREATION_NOTE = (
    "Patreon API v2 documents no post-creation or post-media-upload endpoint for third-party "
    "clients (only reads plus POST /api/oauth2/v2/lives). Posting is therefore manual.")


class PatreonProvider(PlatformProvider):
    name = "patreon"
    supports_api_upload = False
    api_summary = ("Patreon API v2 (v1 retires 2026-10-07 and is never used). No documented "
                   "post creation/media upload -> PREPARE_ONLY package for the post editor.")

    # ---------------------------------------------------------------- config
    @property
    def effective_mode(self) -> PlatformMode:
        if self.detect_post_creation_support():
            return self.mode
        return PlatformMode.PREPARE_ONLY

    def is_configured(self) -> bool:
        return bool(self.settings.patreon_client_id and self.settings.patreon_client_secret)

    @staticmethod
    def detect_post_creation_support() -> bool:
        """Whether the current documented v2 API can create posts. Today: no."""
        return POST_CREATION_SUPPORTED

    def _assert_v2(self, url: str) -> None:
        for fragment in V1_FORBIDDEN:
            if fragment in url:
                raise PlatformError(
                    f"refusing to call the retired Patreon API v1 endpoint: {url}")

    def authenticate(self) -> bool:
        token = (getattr(self.settings, "patreon_access_token", "") or "").strip() \
            if hasattr(self.settings, "patreon_access_token") else ""
        import os
        token = token or os.getenv("PATREON_ACCESS_TOKEN", "").strip()
        if not token:
            raise PlatformAuthError(
                "PATREON_ACCESS_TOKEN is not set (creator access token from the v2 client).")
        return True

    def identity(self) -> dict:
        """GET /api/oauth2/v2/identity - the only thing this provider reads."""
        import os
        token = os.getenv("PATREON_ACCESS_TOKEN", "").strip()
        if not token:
            raise PlatformAuthError("PATREON_ACCESS_TOKEN is not set")
        url = f"{self.settings.patreon_api_base}/identity"
        self._assert_v2(url)
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {token}"},
                                  params={"fields[user]": "full_name,url"})
        except httpx.HTTPError as exc:
            raise PlatformError(f"patreon network error: {exc}", retryable=True)
        if resp.status_code in (401, 403):
            raise PlatformAuthError(f"Patreon rejected the token (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise PlatformError(f"patreon HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ---------------------------------------------------------------- work
    def validate(self, video: Path) -> Dict[str, Any]:
        return validate_patreon(video, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        """Write patreon_post.txt + patreon_metadata.json + patreon_video.mp4."""
        result = PlatformResult("patreon", PlatformMode.PREPARE_ONLY,
                                PlatformStatus.READY_FOR_MANUAL_UPLOAD)
        check = self.validate(video)
        result.validation = check
        if not check["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(check["errors"])
            return result

        out_dir.mkdir(parents=True, exist_ok=True)
        title = (getattr(plan, "tiktok_title", "") or video.stem).strip()
        body = (getattr(plan, "caption", "") or "").strip()
        tags = [t.lstrip("#") for t in (getattr(plan, "hashtags", []) or [])]

        video_copy = out_dir / "patreon_video.mp4"
        if video_copy.resolve() != video.resolve():
            shutil.copy2(video, video_copy)

        post_txt = out_dir / "patreon_post.txt"
        post_txt.write_text(
            f"TITLE\n{title}\n\nBODY\n{body}\n\nTAGS\n{' '.join('#' + t for t in tags)}\n\n"
            f"ACCESS RECOMMENDATION\n{self.settings.patreon_access_recommendation}\n\n"
            "HOW TO POST (manual - Patreon API v2 has no post-creation endpoint)\n"
            "1. Open patreon.com and create a new post (Video).\n"
            f"2. Upload {video_copy.name} (Patreon accepts .mov/.mp4/.mpeg/.ogg, max 5 GB;\n"
            "   a direct file upload appears as a download link, Patreon Video hosts a player).\n"
            "3. Paste the title and body above.\n"
            f"4. Set the access level ({self.settings.patreon_access_recommendation}) and post.\n"
            "5. The cover/thumbnail is chosen by you in Patreon - never generated here.\n",
            encoding="utf-8")

        meta = {
            "platform": "patreon",
            "mode": PlatformMode.PREPARE_ONLY.value,
            "status": PlatformStatus.READY_FOR_MANUAL_UPLOAD.value,
            "api_version": "v2",
            "api_v1_used": False,
            "post_creation_api": "NOT DOCUMENTED IN CURRENT V2",
            "post_creation_note": POST_CREATION_NOTE,
            "title": title,
            "body": body,
            "tags": tags,
            "access_recommendation": self.settings.patreon_access_recommendation,
            "video_path": str(video_copy),
            "post_text_path": str(post_txt),
            "validation": check,
            "auto_publish": False,
            "api_calls_made": 0,
            "cover": "selected manually by the user in Patreon",
            "prepared_at": time.time(),
        }
        meta_path = out_dir / "patreon_metadata.json"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        result.artifacts = [str(video_copy), str(post_txt), str(meta_path)]
        result.metadata = meta
        result.message = POST_CREATION_NOTE
        return result

    # ---------------------------------------------------------------- diag
    def diagnose(self) -> PlatformDiagnosis:
        diag = PlatformDiagnosis(
            platform="patreon", enabled=self.enabled, mode=self.effective_mode,
            configured=self.is_configured(), authenticated=False,
            capability="Post creation API: NOT DOCUMENTED IN CURRENT V2 -> manual upload",
            required_access="Patreon API v2 client (v1 retires 2026-10-07 and is never used)")
        if not self.enabled:
            diag.detail = "disabled (PATREON_ENABLED=false)"
            return diag
        diag.detail = (f"API v2 only | post creation: NOT DOCUMENTED | mode: "
                       f"{self.effective_mode.value} | manual upload REQUIRED")
        try:
            import os
            if os.getenv("PATREON_ACCESS_TOKEN", "").strip():
                identity = self.identity()
                attrs = (identity.get("data") or {}).get("attributes") or {}
                diag.authenticated = True
                diag.account = attrs.get("full_name", "")
                diag.detail += f" | connected as {diag.account or 'unknown'}"
        except (PlatformAuthError, PlatformError) as exc:
            diag.last_error = str(exc)
            diag.detail += f" | identity check: {exc}"
        return diag
