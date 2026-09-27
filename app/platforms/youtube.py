"""YouTube - real API upload, always private (UPLOAD_PRIVATE).

Verified against the current official documentation
(developers.google.com/youtube/v3/docs/videos/insert, checked 2026-09-27):

* Endpoint ``videos.insert`` with ``part=snippet,status``; media uploads go to
  ``https://www.googleapis.com/upload/youtube/v3/videos``.
* **Resumable upload protocol**

  1. ``POST .../videos?uploadType=resumable&part=snippet,status``
     headers ``Authorization: Bearer``, ``Content-Type: application/json``,
     ``X-Upload-Content-Type``, ``X-Upload-Content-Length``;
     body = the video resource (snippet + status).
     → HTTP 200 with the session URI in the ``Location`` header.
  2. ``PUT <session URI>`` with ``Content-Length`` and
     ``Content-Range: bytes a-b/total``; intermediate chunks must be a
     multiple of 256 KB. → 308 while incomplete, 200/201 with the video
     resource when finished.
  3. After an interruption: ``PUT <session URI>`` with
     ``Content-Range: bytes */total`` → 308 plus a ``Range`` header telling
     you how many bytes arrived; continue from there. Session URIs are valid
     for one week.
* ``status.privacyStatus`` ∈ {private, public, unlisted}. **This application
  only ever sends ``private``** and refuses any other configured value.
* Scope: ``https://www.googleapis.com/auth/youtube.upload``.
* Documented restriction used here as a safety net: *"All videos uploaded via
  the videos.insert endpoint from unverified API projects created after
  28 July 2020 will be restricted to private viewing mode."* The application
  never tries to work around that audit.

No thumbnail is uploaded or generated - you set it manually in YouTube Studio.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.config import PlatformMode, PlatformStatus
from app.platforms.base import (PlatformAuthError, PlatformDiagnosis, PlatformError,
                                PlatformProvider, PlatformResult, PublishingNotAllowed)
from app.platforms.validate import AssetCheck, _probe

log = logging.getLogger(__name__)

#: 256 KB - every intermediate chunk must be a multiple of this
CHUNK_UNIT = 256 * 1024
PUBLIC_PRIVACY = {"public", "unlisted"}
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 500


def validate_youtube(path: Path, settings) -> AssetCheck:
    """YouTube accepts a very wide range; check the documented hard limits."""
    info, check = _probe(path, settings, "youtube")
    if info is None:
        return check
    size = path.stat().st_size
    if size > 256 * 1024 ** 3:
        check.ok = False
        check.errors.append("file exceeds YouTube's 256 GB maximum")
    if info.duration > 12 * 3600:
        check.ok = False
        check.errors.append("duration exceeds YouTube's 12 hour maximum")
    if info.duration < 1:
        check.ok = False
        check.errors.append("video is shorter than one second")
    if info.duration > 15 * 60:
        check.warnings.append("videos longer than 15 minutes require a verified YouTube channel")
    return check


@dataclass
class YouTubeTokens:
    access_token: str = ""
    refresh_token: str = ""
    scope: str = ""
    expires_at: float = 0.0

    def is_expired(self, skew: float = 120.0) -> bool:
        return (not self.access_token) or (self.expires_at
                                           and time.time() >= self.expires_at - skew)

    def public_dict(self) -> dict:
        return {"authenticated": bool(self.access_token),
                "refresh_token_present": bool(self.refresh_token),
                "expires_in_seconds": max(0, int(self.expires_at - time.time()))
                if self.expires_at else 0,
                "scope": self.scope}


class YouTubeProvider(PlatformProvider):
    name = "youtube"
    supports_api_upload = True
    api_summary = ("Official YouTube Data API v3 videos.insert (resumable upload) with "
                   "status.privacyStatus=private. Never publishes; thumbnails are manual.")

    # ---------------------------------------------------------------- config
    def token_path(self) -> Path:
        return self.settings.state_dir / "youtube_tokens.json"

    def load_tokens(self) -> YouTubeTokens:
        path = self.token_path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                base = YouTubeTokens()
                return YouTubeTokens(**{k: data.get(k, getattr(base, k)) for k in base.__dict__})
            except (json.JSONDecodeError, TypeError):
                pass
        access = (self.settings.youtube_access_token or "").strip()
        refresh = (self.settings.youtube_refresh_token or "").strip()
        if access or refresh:
            return YouTubeTokens(access_token=access, refresh_token=refresh,
                                 scope=self.settings.youtube_scopes,
                                 expires_at=time.time() + 60 if access else 0.0)
        return YouTubeTokens()

    def save_tokens(self, tokens: YouTubeTokens) -> None:
        path = self.token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
        try:
            import os
            import stat
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def disconnect(self) -> None:
        if self.token_path().exists():
            self.token_path().unlink()

    def is_configured(self) -> bool:
        return bool(self.settings.youtube_client_id and self.settings.youtube_client_secret)

    def _privacy(self) -> str:
        value = (self.settings.youtube_privacy_status or "private").strip().lower()
        if value in PUBLIC_PRIVACY:
            raise PublishingNotAllowed(
                f"YOUTUBE_PRIVACY_STATUS='{value}' would make the video visible. This "
                "application only uploads private videos; change the visibility yourself in "
                "YouTube Studio after reviewing it.")
        if value != "private":
            raise PublishingNotAllowed(f"unsupported YOUTUBE_PRIVACY_STATUS='{value}'")
        return value

    # ---------------------------------------------------------------- oauth
    def authorize_url(self, state: str) -> str:
        import urllib.parse
        params = {"client_id": self.settings.youtube_client_id or "",
                  "redirect_uri": self.settings.youtube_redirect_uri,
                  "response_type": "code",
                  "scope": self.settings.youtube_scopes,
                  "access_type": "offline",
                  "prompt": "consent",
                  "include_granted_scopes": "true",
                  "state": state}
        return (self.settings.youtube_auth_base.rstrip("/") + "/o/oauth2/v2/auth?"
                + urllib.parse.urlencode(params))

    def _token_request(self, data: Dict[str, str]) -> dict:
        url = self.settings.youtube_token_url
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise PlatformError(f"youtube token request failed: {exc}", retryable=True)
        if resp.status_code >= 400:
            raise PlatformAuthError(
                f"Google token request failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def exchange_code(self, code: str) -> YouTubeTokens:
        body = self._token_request({
            "code": code,
            "client_id": self.settings.youtube_client_id or "",
            "client_secret": self.settings.youtube_client_secret or "",
            "redirect_uri": self.settings.youtube_redirect_uri,
            "grant_type": "authorization_code"})
        tokens = YouTubeTokens(access_token=body.get("access_token", ""),
                               refresh_token=body.get("refresh_token", ""),
                               scope=body.get("scope", self.settings.youtube_scopes),
                               expires_at=time.time() + float(body.get("expires_in", 3600)))
        if not tokens.access_token:
            raise PlatformAuthError("Google returned no access token")
        self.save_tokens(tokens)
        return tokens

    def refresh(self, tokens: Optional[YouTubeTokens] = None) -> YouTubeTokens:
        tokens = tokens or self.load_tokens()
        if not tokens.refresh_token:
            raise PlatformAuthError(
                "no YouTube refresh token stored - reconnect the account (the consent must be "
                "requested with access_type=offline)")
        body = self._token_request({
            "refresh_token": tokens.refresh_token,
            "client_id": self.settings.youtube_client_id or "",
            "client_secret": self.settings.youtube_client_secret or "",
            "grant_type": "refresh_token"})
        tokens.access_token = body.get("access_token", "")
        tokens.expires_at = time.time() + float(body.get("expires_in", 3600))
        tokens.scope = body.get("scope", tokens.scope)
        if not tokens.access_token:
            raise PlatformAuthError("Google refresh returned no access token")
        self.save_tokens(tokens)
        return tokens

    def authenticate(self) -> bool:
        tokens = self.load_tokens()
        if not tokens.access_token and not tokens.refresh_token:
            raise PlatformAuthError(
                "YouTube is not connected - run the OAuth flow (Connect YouTube).")
        if tokens.is_expired():
            tokens = self.refresh(tokens)
        return bool(tokens.access_token)

    def _bearer(self) -> str:
        tokens = self.load_tokens()
        if tokens.is_expired():
            tokens = self.refresh(tokens)
        if not tokens.access_token:
            raise PlatformAuthError("no usable YouTube access token")
        return tokens.access_token

    # ---------------------------------------------------------------- http
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = 0
        last: Optional[PlatformError] = None
        while attempts <= self.settings.youtube_max_retries:
            attempts += 1
            headers = {"Authorization": f"Bearer {self._bearer()}", **kwargs.pop("headers", {})}
            try:
                with httpx.Client(timeout=kwargs.pop("timeout", 120.0)) as client:
                    resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last = PlatformError(f"youtube network error: {exc}", retryable=True)
            else:
                if resp.status_code in (401, 403):
                    raise PlatformAuthError(
                        f"YouTube rejected the request (HTTP {resp.status_code}). Check the "
                        f"{UPLOAD_SCOPE} scope and the channel permissions. {resp.text[:200]}",
                        status=resp.status_code)
                if resp.status_code < 400 or resp.status_code == 308:
                    return resp
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = PlatformError(f"youtube HTTP {resp.status_code}: {resp.text[:300]}",
                                     retryable=retryable, status=resp.status_code)
                if not retryable:
                    raise last
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                    continue
            backoff = min(60.0, self.settings.youtube_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying youtube %s in %.1fs: %s", method, backoff, last)
            time.sleep(backoff)
        raise last or PlatformError("youtube request failed")

    # ---------------------------------------------------------------- api
    def get_account(self) -> dict:
        resp = self._request("GET", f"{self.settings.youtube_api_base}/channels",
                             params={"part": "snippet,status", "mine": "true"})
        return resp.json()

    def build_body(self, plan) -> dict:
        title = (getattr(plan, "tiktok_title", "") or "Untitled").strip()[:MAX_TITLE]
        description = (getattr(plan, "caption", "") or "").strip()[:MAX_DESCRIPTION]
        tags: List[str] = []
        budget = MAX_TAGS_CHARS
        for tag in (getattr(plan, "hashtags", []) or []):
            clean = tag.lstrip("#").strip()
            if not clean or len(clean) + 1 > budget:
                continue
            tags.append(clean)
            budget -= len(clean) + 1
        body: Dict[str, Any] = {
            "snippet": {"title": title, "description": description,
                        "categoryId": self.settings.youtube_category_id},
            "status": {"privacyStatus": self._privacy(),
                       "selfDeclaredMadeForKids": bool(self.settings.youtube_made_for_kids)},
        }
        if tags:
            body["snippet"]["tags"] = tags
        language = getattr(plan, "language", "")
        if language:
            body["snippet"]["defaultLanguage"] = language
            body["snippet"]["defaultAudioLanguage"] = language
        return body

    def start_session(self, video: Path, body: dict) -> str:
        url = (f"{self.settings.youtube_upload_base}/youtube/v3/videos"
               f"?uploadType=resumable&part=snippet,status")
        resp = self._request("POST", url, content=json.dumps(body), headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(video.stat().st_size)})
        session = resp.headers.get("Location") or resp.headers.get("location")
        if not session:
            raise PlatformError("YouTube returned no resumable session URI")
        return session

    def _session_offset(self, session_uri: str, size: int) -> int:
        resp = self._request("PUT", session_uri, content=b"", headers={
            "Content-Range": f"bytes */{size}", "Content-Length": "0"})
        if resp.status_code in (200, 201):
            return size
        rng = resp.headers.get("Range") or resp.headers.get("range")
        if rng and "-" in rng:
            return int(rng.split("-")[-1]) + 1
        return 0

    def upload_file(self, session_uri: str, video: Path,
                    progress: Optional[Callable[[int, int], None]] = None) -> dict:
        size = video.stat().st_size
        chunk = max(CHUNK_UNIT, (self.settings.youtube_chunk_size // CHUNK_UNIT) * CHUNK_UNIT)
        offset = 0
        attempts = 0
        while offset < size:
            length = min(chunk, size - offset)
            with video.open("rb") as fh:
                fh.seek(offset)
                payload = fh.read(length)
            try:
                resp = self._request("PUT", session_uri, content=payload, timeout=1800.0,
                                     headers={"Content-Length": str(len(payload)),
                                              "Content-Range":
                                                  f"bytes {offset}-{offset + len(payload) - 1}/{size}"})
            except PlatformError as exc:
                attempts += 1
                if attempts > self.settings.youtube_max_retries or not exc.retryable:
                    raise
                offset = self._session_offset(session_uri, size)      # resume
                log.warning("youtube upload interrupted, resuming at %s", offset)
                continue
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code == 308:
                rng = resp.headers.get("Range") or resp.headers.get("range")
                offset = (int(rng.split("-")[-1]) + 1) if rng and "-" in rng \
                    else offset + len(payload)
                if progress:
                    progress(offset, size)
                continue
            raise PlatformError(f"unexpected upload status {resp.status_code}")
        # everything sent but no final body yet - ask the session
        resp = self._request("PUT", session_uri, content=b"",
                             headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"})
        if resp.status_code in (200, 201):
            return resp.json()
        raise PlatformError("youtube upload finished without a video resource")

    def get_video(self, video_id: str) -> dict:
        resp = self._request("GET", f"{self.settings.youtube_api_base}/videos",
                             params={"part": "status,snippet,processingDetails", "id": video_id})
        items = resp.json().get("items") or []
        return items[0] if items else {}

    # ---------------------------------------------------------------- base API
    def validate(self, video: Path) -> Dict[str, Any]:
        return validate_youtube(video, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        """Fallback manifest when the API is not configured."""
        out_dir.mkdir(parents=True, exist_ok=True)
        check = self.validate(video)
        body = {"snippet": {"title": getattr(plan, "tiktok_title", ""),
                            "description": getattr(plan, "caption", "")},
                "status": {"privacyStatus": "private"}}
        payload = {"platform": "youtube", "mode": PlatformMode.PREPARE_ONLY.value,
                   "video_path": str(video), "planned_request": body, "validation": check,
                   "thumbnail": "selected manually by the user in YouTube Studio",
                   "auto_publish": False}
        path = out_dir / "youtube.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return PlatformResult("youtube", PlatformMode.PREPARE_ONLY,
                              PlatformStatus.READY_FOR_MANUAL_UPLOAD, artifacts=[str(path)],
                              validation=check,
                              message="YouTube API not configured - upload manually.")

    def upload(self, video: Path, plan, out_dir: Path,
               on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        def emit(event: str, message: str = "", **data: Any) -> None:
            if on_event:
                on_event(event, message, data)

        result = PlatformResult("youtube", PlatformMode.UPLOAD_PRIVATE, PlatformStatus.UPLOADING)
        check = self.validate(video)
        result.validation = check
        if not check["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(check["errors"])
            return result
        try:
            body = self.build_body(plan)                 # raises if not private
            self.authenticate()
            emit("YOUTUBE_UPLOAD_STARTED", f"{video.name} ({video.stat().st_size} bytes)")

            session = self.start_session(video, body)
            emit("YOUTUBE_SESSION_CREATED", "resumable session opened")
            uploaded = self.upload_file(
                session, video,
                progress=lambda sent, total: emit("YOUTUBE_UPLOAD_PROGRESS", f"{sent}/{total}")
                if sent == total else None)

            video_id = uploaded.get("id", "")
            privacy = ((uploaded.get("status") or {}).get("privacyStatus")
                       or body["status"]["privacyStatus"])
            result.external_id = video_id
            result.url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
            result.metadata = {"privacy_status": privacy,
                               "upload_status": (uploaded.get("status") or {}).get("uploadStatus"),
                               "snippet": {k: (uploaded.get("snippet") or {}).get(k)
                                           for k in ("title", "publishedAt", "channelId")},
                               "thumbnail": "manual - set it in YouTube Studio"}
            if privacy in PUBLIC_PRIVACY:
                result.status = PlatformStatus.FAILED
                result.error = (f"YouTube reports privacyStatus='{privacy}' - the video is not "
                                "private. Set it back to private in YouTube Studio.")
                emit("YOUTUBE_PRIVACY_WARNING", result.error)
                return result
            result.status = PlatformStatus.UPLOADED_PRIVATE
            result.message = (f"Uploaded to YouTube as private (privacyStatus={privacy}). "
                              "Nothing was published; set the thumbnail and visibility yourself "
                              "in YouTube Studio.")
            emit("YOUTUBE_UPLOADED_PRIVATE", f"{video_id} privacy={privacy}")

            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "youtube.json"
            path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            result.artifacts.append(str(path))
        except (PlatformAuthError, PublishingNotAllowed) as exc:
            result.status = (PlatformStatus.AUTH_REQUIRED if isinstance(exc, PlatformAuthError)
                             else PlatformStatus.FAILED)
            result.error = str(exc)
            emit("YOUTUBE_FAILED", str(exc))
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
            emit("YOUTUBE_FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected YouTube failure")
            result.status = PlatformStatus.FAILED
            result.error = f"unexpected {exc.__class__.__name__}: {exc}"
            emit("YOUTUBE_FAILED", result.error)
        finally:
            result.completed_at = time.time()
        return result

    def get_status(self, external_id: str) -> PlatformResult:
        result = PlatformResult("youtube", self.effective_mode, PlatformStatus.PROCESSING,
                                external_id=external_id)
        try:
            item = self.get_video(external_id)
            status = item.get("status") or {}
            result.metadata = {"status": status,
                               "processing": item.get("processingDetails")}
            result.url = f"https://www.youtube.com/watch?v={external_id}"
            if status.get("privacyStatus") in PUBLIC_PRIVACY:
                result.status = PlatformStatus.FAILED
                result.error = f"video is {status.get('privacyStatus')}, not private"
            elif status.get("uploadStatus") == "processed":
                result.status = PlatformStatus.UPLOADED_PRIVATE
            elif status.get("uploadStatus") in {"failed", "rejected", "deleted"}:
                result.status = PlatformStatus.FAILED
                result.error = (status.get("failureReason") or status.get("rejectionReason")
                                or status.get("uploadStatus"))
        except PlatformError as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
        return result

    # ---------------------------------------------------------------- diag
    def diagnose(self) -> PlatformDiagnosis:
        tokens = self.load_tokens()
        diag = PlatformDiagnosis(
            platform="youtube", enabled=self.enabled, mode=self.effective_mode,
            configured=self.is_configured(), authenticated=bool(tokens.access_token
                                                                or tokens.refresh_token),
            capability="Official videos.insert resumable upload, privacyStatus=private",
            required_access=f"Google Cloud project with YouTube Data API v3 + scope "
                            f"{UPLOAD_SCOPE}")
        project_state = (self.settings.youtube_api_project_audited or "unknown").lower()
        base = (f"API project: {project_state} | Automatic public publishing: DISABLED | "
                f"privacyStatus={self.settings.youtube_privacy_status}")
        if not self.enabled:
            diag.detail = "disabled (YOUTUBE_ENABLED=false) | " + base
            return diag
        if not self.is_configured():
            diag.detail = ("missing YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET | " + base)
            return diag
        if not diag.authenticated:
            diag.detail = "not connected - run Connect YouTube | " + base
            return diag
        try:
            channels = self.get_account().get("items") or []
            if channels:
                diag.account = (channels[0].get("snippet") or {}).get("title", "")
            diag.detail = (f"connected as {diag.account or 'unknown channel'} | " + base)
        except (PlatformAuthError, PlatformError) as exc:
            diag.last_error = str(exc)
            diag.detail = f"{exc} | {base}"
        if project_state != "audited":
            diag.detail += (" | note: videos uploaded by unverified API projects created after "
                            "28 July 2020 stay private until the project passes YouTube's "
                            "compliance audit - this application never works around that")
        return diag
