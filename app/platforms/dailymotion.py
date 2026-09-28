"""Dailymotion - API v2 only, real upload, visibility=private (UPLOAD_PRIVATE).

Verified against the current official documentation (developers.dailymotion.com
"Upload videos" + API reference, checked 2026-09-27):

1. Authenticate — OAuth 2.0 token with the **video.manage** scope
   (``POST https://api.dailymotion.com/oauth/token``).
2. ``POST /v2/files/upload_sessions`` (no body) →
   ``{"upload_url": ..., "progress_url": ...}``
3. ``POST {upload_url}`` with ``multipart/form-data`` and the ``file`` field →
   the response contains the ingest URL of the uploaded file.
4. ``POST /v2/profiles/{profile_id}/videos`` with
   ``{"title", "category", "visibility", "is_for_kids",
     "source": {"file_url": <url from step 3>}}``

**Honest wording:** step 4 *creates the video object on your channel* — the
Dailymotion documentation calls this "create and publish the video". It is not
a TikTok-style hidden draft. What this application controls is
``visibility=private``, i.e. who may watch it. The video object exists on your
channel from that moment; only you (or people with the private URL) can watch
it. ``visibility=public`` is refused by this application.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import PlatformMode, PlatformStatus
from app.platforms.base import (PlatformAuthError, PlatformDiagnosis, PlatformError,
                                PlatformProvider, PlatformResult, PublishingNotAllowed)
from app.platforms.validate import validate_dailymotion

log = logging.getLogger(__name__)

PUBLIC_VISIBILITIES = {"public"}
ALLOWED_VISIBILITIES = {"private", "password"}


class DailymotionProvider(PlatformProvider):
    name = "dailymotion"
    supports_api_upload = True
    api_summary = ("Official Dailymotion API v2 upload (upload session -> file -> video object) "
                   "with visibility=private. The video object is created on your channel; "
                   "private controls who can watch it. Never public.")

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._token: Optional[str] = None
        self._token_expires: float = 0.0

    # ---------------------------------------------------------------- auth
    def is_configured(self) -> bool:
        s = self.settings
        return bool(s.dailymotion_client_id and s.dailymotion_client_secret
                    and s.dailymotion_profile_id)

    def _visibility(self) -> str:
        value = (self.settings.dailymotion_visibility or "private").strip().lower()
        if value in PUBLIC_VISIBILITIES:
            raise PublishingNotAllowed(
                "DAILYMOTION_VISIBILITY='public' is refused: this application never publishes "
                "publicly. Use 'private' or 'password'.")
        if value not in ALLOWED_VISIBILITIES:
            raise PublishingNotAllowed(
                f"unsupported DAILYMOTION_VISIBILITY='{value}'; allowed: "
                f"{sorted(ALLOWED_VISIBILITIES)}")
        return value

    def authenticate(self) -> bool:
        self.token()
        return True

    def token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        s = self.settings
        if not (s.dailymotion_client_id and s.dailymotion_client_secret):
            raise PlatformAuthError(
                "DAILYMOTION_CLIENT_ID / DAILYMOTION_CLIENT_SECRET are not configured "
                "(create an API key in Dailymotion Studio -> Organization -> API keys).")
        data = {"client_id": s.dailymotion_client_id,
                "client_secret": s.dailymotion_client_secret,
                "scope": s.dailymotion_scope}
        if s.dailymotion_username and s.dailymotion_password:
            data.update({"grant_type": "password", "username": s.dailymotion_username,
                         "password": s.dailymotion_password})
        else:
            data["grant_type"] = "client_credentials"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(f"{s.dailymotion_api_base}/oauth/token", data=data)
        except httpx.HTTPError as exc:
            raise PlatformError(f"dailymotion token request failed: {exc}", retryable=True)
        if resp.status_code >= 400:
            raise PlatformAuthError(
                f"Dailymotion token request failed (HTTP {resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        self._token = body.get("access_token")
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        if not self._token:
            raise PlatformAuthError(f"Dailymotion returned no access token: {str(body)[:200]}")
        return self._token

    def disconnect(self) -> None:
        self._token = None
        self._token_expires = 0.0

    # ---------------------------------------------------------------- http
    def _request(self, method: str, url: str, *, auth: bool = True, **kwargs) -> httpx.Response:
        attempts = 0
        last: Optional[PlatformError] = None
        while attempts <= self.settings.dailymotion_max_retries:
            attempts += 1
            headers = dict(kwargs.pop("headers", {}))
            if auth:
                headers["Authorization"] = f"Bearer {self.token()}"
            try:
                with httpx.Client(timeout=kwargs.pop("timeout", 120.0)) as client:
                    resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last = PlatformError(f"dailymotion network error: {exc}", retryable=True)
            else:
                if resp.status_code in (401, 403):
                    raise PlatformAuthError(
                        f"Dailymotion rejected the credentials (HTTP {resp.status_code}). "
                        f"Check the API key and the '{self.settings.dailymotion_scope}' scope. "
                        f"{resp.text[:200]}", status=resp.status_code)
                if resp.status_code < 400:
                    return resp
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = PlatformError(f"dailymotion HTTP {resp.status_code}: {resp.text[:300]}",
                                     retryable=retryable, status=resp.status_code)
                if not retryable:
                    raise last
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                    continue
            backoff = min(60.0, self.settings.dailymotion_backoff_base_seconds
                          * (2 ** (attempts - 1)))
            log.warning("retrying dailymotion %s in %.1fs: %s", method, backoff, last)
            time.sleep(backoff)
        raise last or PlatformError("dailymotion request failed")

    # ---------------------------------------------------------------- api
    def create_upload_session(self) -> dict:
        resp = self._request("POST", f"{self.settings.dailymotion_api_base}"
                                     f"/v2/files/upload_sessions")
        body = resp.json()
        if not body.get("upload_url"):
            raise PlatformError(f"no upload_url in the session response: {str(body)[:200]}")
        return body

    def upload_file(self, upload_url: str, video: Path) -> dict:
        with video.open("rb") as fh:
            files = {"file": (video.name, fh, "video/mp4")}
            resp = self._request("POST", upload_url, auth=False, files=files, timeout=3600.0)
        body = resp.json()
        file_url = body.get("url") or body.get("file_url")
        if not file_url:
            raise PlatformError(f"upload response contained no file url: {str(body)[:200]}")
        body["file_url"] = file_url
        return body

    def get_progress(self, progress_url: str) -> dict:
        try:
            resp = self._request("GET", progress_url, auth=False, timeout=30.0)
            return resp.json()
        except PlatformError:
            return {}

    def create_video(self, file_url: str, title: str, description: str,
                     tags: Optional[list] = None) -> dict:
        s = self.settings
        payload = {
            "title": (title or "Untitled")[:255],
            "description": (description or "")[:3000],
            "category": s.dailymotion_category,
            "visibility": self._visibility(),
            "is_for_kids": bool(s.dailymotion_is_for_kids),
            "source": {"file_url": file_url},
        }
        if tags:
            payload["tags"] = [t.lstrip("#") for t in tags][:10]
        resp = self._request(
            "POST", f"{s.dailymotion_api_base}/v2/profiles/{s.dailymotion_profile_id}/videos",
            json=payload, headers={"Content-Type": "application/json",
                                   "Accept": "application/json"})
        return resp.json()

    def get_video(self, video_id: str) -> dict:
        resp = self._request("GET", f"{self.settings.dailymotion_api_base}/v2/videos/{video_id}",
                             params={"fields": "id,title,visibility,status,url,private"})
        return resp.json()

    # ---------------------------------------------------------------- base API
    def validate(self, video: Path) -> Dict[str, Any]:
        return validate_dailymotion(video, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        check = self.validate(video)
        payload = {"platform": "dailymotion", "mode": PlatformMode.PREPARE_ONLY.value,
                   "video_path": str(video), "title": getattr(plan, "tiktok_title", ""),
                   "description": getattr(plan, "caption", ""),
                   "visibility": self.settings.dailymotion_visibility,
                   "validation": check, "auto_publish": False}
        path = out_dir / "dailymotion.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return PlatformResult("dailymotion", PlatformMode.PREPARE_ONLY,
                              PlatformStatus.READY_FOR_MANUAL_UPLOAD,
                              artifacts=[str(path)], validation=check,
                              message="Dailymotion API not configured - upload manually.")

    def upload(self, video: Path, plan, out_dir: Path,
               on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        def emit(event: str, message: str = "", **data: Any) -> None:
            if on_event:
                on_event(event, message, data)

        result = PlatformResult("dailymotion", PlatformMode.UPLOAD_PRIVATE,
                                PlatformStatus.UPLOADING)
        check = self.validate(video)
        result.validation = check
        if not check["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(check["errors"])
            return result
        if not self.settings.dailymotion_profile_id:
            result.status = PlatformStatus.NOT_CONFIGURED
            result.error = "DAILYMOTION_PROFILE_ID is not configured"
            return result
        try:
            self._visibility()          # refuse public before doing any work
            self.authenticate()
            emit("DAILYMOTION_UPLOAD_STARTED", video.name)

            session = self.create_upload_session()
            uploaded = self.upload_file(session["upload_url"], video)
            emit("DAILYMOTION_FILE_UPLOADED", uploaded.get("file_url", ""))
            if session.get("progress_url"):
                result.metadata["progress"] = self.get_progress(session["progress_url"])

            created = self.create_video(uploaded["file_url"],
                                        getattr(plan, "tiktok_title", "") or video.stem,
                                        getattr(plan, "caption", ""),
                                        getattr(plan, "hashtags", None))
            video_id = str(created.get("id", ""))
            result.external_id = video_id
            result.url = created.get("url") or f"https://www.dailymotion.com/video/{video_id}"

            info = self.get_video(video_id) if video_id else {}
            visibility = info.get("visibility") or created.get("visibility") or "unknown"
            result.metadata.update({"created": created, "video": info,
                                    "visibility": visibility})
            if visibility in PUBLIC_VISIBILITIES:
                result.status = PlatformStatus.FAILED
                result.error = (f"Dailymotion reports visibility='{visibility}' - the video is "
                                "public, which this application never intends.")
                emit("DAILYMOTION_VISIBILITY_WARNING", result.error)
                return result
            result.status = PlatformStatus.UPLOADED_PRIVATE
            result.message = (f"Video object created on your Dailymotion channel with "
                              f"visibility='{visibility}'. It is not publicly listed; note that "
                              "Dailymotion has no TikTok-style draft state.")
            emit("DAILYMOTION_UPLOADED_PRIVATE", f"{video_id} visibility={visibility}")

            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "dailymotion.json"
            path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            result.artifacts.append(str(path))
        except (PlatformAuthError, PublishingNotAllowed) as exc:
            result.status = (PlatformStatus.AUTH_REQUIRED if isinstance(exc, PlatformAuthError)
                             else PlatformStatus.FAILED)
            result.error = str(exc)
            emit("DAILYMOTION_FAILED", str(exc))
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
            emit("DAILYMOTION_FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected Dailymotion failure")
            result.status = PlatformStatus.FAILED
            result.error = f"unexpected {exc.__class__.__name__}: {exc}"
            emit("DAILYMOTION_FAILED", result.error)
        finally:
            result.completed_at = time.time()
        return result

    def get_status(self, external_id: str) -> PlatformResult:
        result = PlatformResult("dailymotion", self.effective_mode, PlatformStatus.PROCESSING,
                                external_id=external_id)
        try:
            info = self.get_video(external_id)
            result.metadata = info
            result.url = info.get("url")
            if info.get("status") in {"published", "processing", "ready"}:
                result.status = (PlatformStatus.UPLOADED_PRIVATE
                                 if info.get("visibility") in ALLOWED_VISIBILITIES
                                 else PlatformStatus.FAILED)
            elif info.get("status") in {"rejected", "deleted", "encoding_error"}:
                result.status = PlatformStatus.FAILED
                result.error = f"Dailymotion status: {info.get('status')}"
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
        return result

    # ---------------------------------------------------------------- diag
    def diagnose(self) -> PlatformDiagnosis:
        s = self.settings
        diag = PlatformDiagnosis(
            platform="dailymotion", enabled=self.enabled, mode=self.effective_mode,
            configured=self.is_configured(), authenticated=False,
            capability="Official API v2 upload, visibility=private",
            required_access=f"API key (private) + scope {s.dailymotion_scope} + profile id")
        if not self.enabled:
            diag.detail = "disabled (DAILYMOTION_ENABLED=false)"
            return diag
        missing = [n for n, v in (("DAILYMOTION_CLIENT_ID", s.dailymotion_client_id),
                                  ("DAILYMOTION_CLIENT_SECRET", s.dailymotion_client_secret),
                                  ("DAILYMOTION_PROFILE_ID", s.dailymotion_profile_id))
                   if not v]
        if missing:
            diag.detail = "missing: " + ", ".join(missing)
            return diag
        try:
            self.token()
            diag.authenticated = True
            diag.account = s.dailymotion_profile_id or ""
            diag.detail = (f"token acquired; profile={s.dailymotion_profile_id}; "
                           f"visibility={s.dailymotion_visibility} (public is refused)")
        except PlatformAuthError as exc:
            diag.detail = str(exc)
            diag.last_error = str(exc)
        except PlatformError as exc:
            diag.detail = f"could not reach Dailymotion: {exc}"
            diag.last_error = str(exc)
        return diag
