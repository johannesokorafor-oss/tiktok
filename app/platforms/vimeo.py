"""Vimeo - real API upload, always private (UPLOAD_PRIVATE).

Verified against the current official documentation (developer.vimeo.com,
"Working with Video Uploads", checked 2026-09-27):

* Before you begin: the app needs **upload access**, which must be requested
  from Vimeo, plus an access token with the **upload** and **edit** scopes.
* Create the video:  ``POST https://api.vimeo.com/me/videos``
      headers: ``Authorization: bearer <token>``,
               ``Content-Type: application/json``,
               ``Accept: application/vnd.vimeo.*+json;version=3.4``
      body:    ``{"upload": {"approach": "tus", "size": <bytes>},
                  "name": ..., "description": ...,
                  "privacy": {"view": "nobody"}}``
      response: ``uri``, ``link`` and ``upload.upload_link``
* Upload the file: ``PATCH {upload.upload_link}`` with
      ``Tus-Resumable: 1.0.0``, ``Upload-Offset: <offset>``,
      ``Content-Type: application/offset+octet-stream``
  Resume by re-reading the offset (``HEAD`` on the same link) and sending only
  the remaining bytes.
* Verify: ``HEAD {upload.upload_link}`` returns ``Upload-Length`` and
  ``Upload-Offset``; they are equal when the transfer is complete.

``privacy.view`` documented values: anybody, contacts, disable, nobody,
password, unlisted, users. This application only ever sends **nobody** (or
whatever non-public value you configure) - it never makes a video public.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import PlatformMode, PlatformStatus, Settings
from app.platforms.base import (PlatformAuthError, PlatformDiagnosis, PlatformError,
                                PlatformProvider, PlatformResult, PublishingNotAllowed)
from app.platforms.validate import validate_vimeo

log = logging.getLogger(__name__)

#: privacy.view values that would make the video publicly visible - refused
PUBLIC_PRIVACY_VALUES = {"anybody", "unlisted", "users"}
ACCEPT_HEADER = "application/vnd.vimeo.*+json;version=3.4"


class VimeoProvider(PlatformProvider):
    name = "vimeo"
    supports_api_upload = True
    api_summary = ("Official Vimeo API upload (tus resumable). Requires upload access approval "
                   "for the app/account and a token with the upload+edit scopes. The video is "
                   "created with privacy.view=nobody and is never made public.")

    # ---------------------------------------------------------------- config
    def token(self) -> str:
        token = (self.settings.vimeo_access_token or "").strip()
        if not token:
            raise PlatformAuthError(
                "VIMEO_ACCESS_TOKEN is not set. Create a token with the 'upload' and 'edit' "
                "scopes in the Vimeo developer app, or complete the OAuth flow.")
        return token

    def is_configured(self) -> bool:
        return bool((self.settings.vimeo_access_token or "").strip())

    def authenticate(self) -> bool:
        self.token()
        return True

    def disconnect(self) -> None:
        self.settings.vimeo_access_token = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"bearer {self.token()}",
                "Content-Type": "application/json",
                "Accept": ACCEPT_HEADER}

    def _privacy_view(self) -> str:
        value = (self.settings.vimeo_privacy_view or "nobody").strip().lower()
        if value in PUBLIC_PRIVACY_VALUES:
            raise PublishingNotAllowed(
                f"VIMEO_PRIVACY_VIEW='{value}' would make the video publicly viewable. "
                "This application only performs private uploads (nobody/password/disable).")
        return value

    # ---------------------------------------------------------------- http
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = 0
        last: Optional[PlatformError] = None
        while attempts <= self.settings.vimeo_max_retries:
            attempts += 1
            try:
                with httpx.Client(timeout=kwargs.pop("timeout", 120.0)) as client:
                    resp = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last = PlatformError(f"vimeo network error: {exc}", retryable=True)
            else:
                if resp.status_code in (401, 403):
                    raise PlatformAuthError(
                        f"Vimeo rejected the credentials (HTTP {resp.status_code}). Check the "
                        "token scopes (upload, edit) and whether your app has been granted "
                        "upload access. {}".format(resp.text[:200]), status=resp.status_code)
                if resp.status_code < 400:
                    return resp
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = PlatformError(f"vimeo HTTP {resp.status_code}: {resp.text[:300]}",
                                     retryable=retryable, status=resp.status_code)
                if not retryable:
                    raise last
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                    continue
            backoff = min(60.0, self.settings.vimeo_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying vimeo %s in %.1fs: %s", method, backoff, last)
            time.sleep(backoff)
        raise last or PlatformError("vimeo request failed")

    # ---------------------------------------------------------------- api
    def get_account(self) -> dict:
        resp = self._request("GET", f"{self.settings.vimeo_api_base}/me",
                             headers=self._headers(),
                             params={"fields": "uri,name,account,upload_quota"})
        return resp.json()

    def create_video(self, video: Path, name: str, description: str) -> dict:
        body = {
            "upload": {"approach": "tus", "size": str(video.stat().st_size)},
            "name": name[:128] or video.stem,
            "description": description[:5000],
            "privacy": {"view": self._privacy_view(), "embed": "private", "download": False},
        }
        resp = self._request("POST", f"{self.settings.vimeo_api_base}/me/videos",
                             headers=self._headers(), content=json.dumps(body))
        data = resp.json()
        upload = data.get("upload") or {}
        if upload.get("approach") != "tus" or not upload.get("upload_link"):
            raise PlatformError(f"Vimeo did not return a tus upload link: {str(data)[:300]}")
        return data

    def _tus_offset(self, upload_link: str) -> int:
        resp = self._request("HEAD", upload_link,
                             headers={"Tus-Resumable": "1.0.0", "Accept": ACCEPT_HEADER})
        return int(resp.headers.get("Upload-Offset", 0) or 0)

    def upload_file(self, upload_link: str, video: Path,
                    progress: Optional[Callable[[int, int], None]] = None) -> int:
        """PATCH the file with tus, resuming from the server-reported offset."""
        size = video.stat().st_size
        offset = 0
        attempts = 0
        while offset < size:
            chunk_size = max(1, min(self.settings.vimeo_chunk_size, size - offset))
            with video.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(chunk_size)
            try:
                resp = self._request(
                    "PATCH", upload_link, content=chunk, timeout=1800.0,
                    headers={"Tus-Resumable": "1.0.0",
                             "Upload-Offset": str(offset),
                             "Content-Type": "application/offset+octet-stream"})
                new_offset = int(resp.headers.get("Upload-Offset", offset + len(chunk)))
            except PlatformError as exc:
                attempts += 1
                if attempts > self.settings.vimeo_max_retries or not exc.retryable:
                    raise
                new_offset = self._tus_offset(upload_link)      # resume
                log.warning("vimeo upload interrupted, resuming at offset %s", new_offset)
            if new_offset <= offset:
                # no progress -> ask the server where it stands, then stop looping
                probe_offset = self._tus_offset(upload_link)
                if probe_offset <= offset:
                    raise PlatformError("vimeo upload made no progress", retryable=False)
                new_offset = probe_offset
            offset = new_offset
            if progress:
                progress(offset, size)
        final = self._tus_offset(upload_link)
        if final != size:
            raise PlatformError(f"vimeo upload incomplete: {final}/{size} bytes transferred")
        return final

    def get_video(self, uri: str) -> dict:
        resp = self._request("GET", f"{self.settings.vimeo_api_base}{uri}",
                             headers=self._headers(),
                             params={"fields": "uri,link,name,privacy,status,transcode,upload"})
        return resp.json()

    # ---------------------------------------------------------------- base API
    def validate(self, video: Path) -> Dict[str, Any]:
        return validate_vimeo(video, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        """Fallback when the API cannot be used - writes a small manifest only."""
        out_dir.mkdir(parents=True, exist_ok=True)
        check = self.validate(video)
        payload = {"platform": "vimeo", "mode": PlatformMode.PREPARE_ONLY.value,
                   "video_path": str(video), "title": getattr(plan, "tiktok_title", ""),
                   "description": getattr(plan, "caption", ""),
                   "privacy": self.settings.vimeo_privacy_view,
                   "validation": check, "auto_publish": False}
        path = out_dir / "vimeo.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return PlatformResult("vimeo", PlatformMode.PREPARE_ONLY,
                              PlatformStatus.READY_FOR_MANUAL_UPLOAD,
                              artifacts=[str(path)], validation=check,
                              message="Vimeo API not configured - upload the file manually.")

    def upload(self, video: Path, plan, out_dir: Path,
               on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        def emit(event: str, message: str = "", **data: Any) -> None:
            if on_event:
                on_event(event, message, data)

        result = PlatformResult("vimeo", PlatformMode.UPLOAD_PRIVATE, PlatformStatus.UPLOADING)
        check = self.validate(video)
        result.validation = check
        if not check["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(check["errors"])
            return result
        try:
            self.authenticate()
            title = (getattr(plan, "tiktok_title", "") or video.stem)
            description = getattr(plan, "caption", "") or ""
            emit("VIMEO_UPLOAD_STARTED", f"{video.name} ({video.stat().st_size} bytes)")

            created = self.create_video(video, title, description)
            uri = created.get("uri", "")
            result.external_id = uri
            result.url = created.get("link")
            emit("VIMEO_VIDEO_CREATED", uri, uri=uri)

            self.upload_file(created["upload"]["upload_link"], video,
                             progress=lambda sent, total: emit(
                                 "VIMEO_UPLOAD_PROGRESS", f"{sent}/{total}")
                             if sent == total else None)

            info = self.get_video(uri)
            privacy = (info.get("privacy") or {}).get("view", "")
            result.metadata = {"privacy": info.get("privacy"), "status": info.get("status"),
                               "transcode": info.get("transcode"), "name": info.get("name")}
            result.url = info.get("link") or result.url
            if privacy in PUBLIC_PRIVACY_VALUES:
                result.status = PlatformStatus.FAILED
                result.error = (f"Vimeo reports privacy.view='{privacy}' - the video is not "
                                "private. Check your account's default privacy settings.")
                emit("VIMEO_PRIVACY_WARNING", result.error)
                return result
            result.status = PlatformStatus.UPLOADED_PRIVATE
            result.message = (f"Uploaded to Vimeo as private (privacy.view={privacy}). "
                              "Nothing was published.")
            emit("VIMEO_UPLOADED_PRIVATE", f"{uri} privacy={privacy}")

            path = out_dir / "vimeo.json"
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            result.artifacts.append(str(path))
        except (PlatformAuthError, PublishingNotAllowed) as exc:
            result.status = PlatformStatus.AUTH_REQUIRED if isinstance(exc, PlatformAuthError) \
                else PlatformStatus.FAILED
            result.error = str(exc)
            emit("VIMEO_FAILED", str(exc))
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
            emit("VIMEO_FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001 - never kill the job
            log.exception("unexpected Vimeo failure")
            result.status = PlatformStatus.FAILED
            result.error = f"unexpected {exc.__class__.__name__}: {exc}"
            emit("VIMEO_FAILED", result.error)
        finally:
            result.completed_at = time.time()
        return result

    def get_status(self, external_id: str) -> PlatformResult:
        result = PlatformResult("vimeo", self.effective_mode, PlatformStatus.PROCESSING,
                                external_id=external_id)
        try:
            info = self.get_video(external_id)
            result.metadata = {"privacy": info.get("privacy"), "status": info.get("status"),
                               "transcode": info.get("transcode")}
            result.url = info.get("link")
            state = (info.get("transcode") or {}).get("status") or info.get("status")
            if state in {"complete", "available"}:
                result.status = PlatformStatus.UPLOADED_PRIVATE
            elif state in {"error", "quota_exceeded", "total_cap_exceeded"}:
                result.status = PlatformStatus.FAILED
                result.error = f"Vimeo status: {state}"
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
        return result

    # ---------------------------------------------------------------- diag
    def diagnose(self) -> PlatformDiagnosis:
        configured = self.is_configured()
        diag = PlatformDiagnosis(
            platform="vimeo", enabled=self.enabled, mode=self.effective_mode,
            configured=configured, authenticated=False,
            capability="Official API upload (tus), always private",
            required_access="Vimeo upload access approval + token scopes: upload, edit")
        if not self.enabled:
            diag.detail = "disabled (VIMEO_ENABLED=false)"
            return diag
        if not configured:
            diag.detail = ("credentials missing: set VIMEO_ACCESS_TOKEN (scopes upload+edit). "
                           "Vimeo API upload access may require approval for this "
                           "application/account.")
            return diag
        try:
            account = self.get_account()
            diag.authenticated = True
            diag.account = account.get("name", "")
            quota = (account.get("upload_quota") or {}).get("space", {})
            free = quota.get("free")
            diag.detail = (f"connected as {diag.account}; "
                           f"privacy.view={self.settings.vimeo_privacy_view}"
                           + (f"; free quota {free} bytes" if free is not None else ""))
        except PlatformAuthError as exc:
            diag.detail = (f"{exc} — Vimeo API upload access may require approval for this "
                           "application/account.")
            diag.last_error = str(exc)
        except PlatformError as exc:
            diag.detail = f"could not reach Vimeo: {exc}"
            diag.last_error = str(exc)
        return diag
