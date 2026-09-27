"""Instagram Content Publishing API client - UPLOAD ONLY.

Documented flow (Meta for Developers, verified 2026-09-27):

  1. create container   POST /{ig-user-id}/media
                        media_type=REELS + video_url=<public https url>
                        (or upload_type=resumable for a local file, see below)
                        -> {"id": "<IG_CONTAINER_ID>"}
  2. upload bytes       POST https://rupload.facebook.com/ig-api-upload/{version}/{container_id}
                        headers: Authorization: OAuth <token>, offset, file_size
                        (resumable sessions are documented for apps using
                        **Facebook Login for Business** only)
  3. poll status        GET /{container-id}?fields=status_code,status
                        -> IN_PROGRESS | FINISHED | PUBLISHED | EXPIRED | ERROR
  4. publish            POST /{ig-user-id}/media_publish   <-- NEVER CALLED HERE

**This client deliberately implements no publish call.** ``media_publish`` is
not reachable from any code path in this application; see
``PublishingDisabledError``.

Containers expire 24 hours after creation (Meta documentation), after which the
status turns EXPIRED and the media must be staged again.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import InstagramLoginMode, Settings
from app.instagram.oauth import InstagramOAuth, InstagramOAuthError
from app.tiktok.client import RateLimiter

log = logging.getLogger(__name__)

#: documented container states
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_FINISHED = "FINISHED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_EXPIRED = "EXPIRED"
STATUS_ERROR = "ERROR"
TERMINAL_STATUSES = {STATUS_FINISHED, STATUS_PUBLISHED, STATUS_EXPIRED, STATUS_ERROR}


class InstagramAPIError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[Any] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class PublishingDisabledError(RuntimeError):
    """Raised if anything ever tries to publish. Publishing is disabled by design."""


@dataclass
class ContainerInfo:
    container_id: str
    created_at: float
    status_code: str = STATUS_IN_PROGRESS
    status: str = ""
    uploaded_bytes: int = 0
    method: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)

    def remaining_seconds(self, ttl_hours: float = 24.0) -> float:
        return max(0.0, ttl_hours * 3600 - self.age_seconds())

    def as_dict(self, ttl_hours: float = 24.0) -> dict:
        return {"container_id": self.container_id, "created_at": self.created_at,
                "status_code": self.status_code, "status": self.status,
                "uploaded_bytes": self.uploaded_bytes, "upload_method": self.method,
                "age_seconds": int(self.age_seconds()),
                "expires_in_seconds": int(self.remaining_seconds(ttl_hours)),
                "expires_at": self.created_at + ttl_hours * 3600,
                "raw": self.raw}


class InstagramClient:
    """Thin, honest wrapper around the documented endpoints. Never publishes."""

    def __init__(self, settings: Settings, oauth: Optional[InstagramOAuth] = None,
                 client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.oauth = oauth or InstagramOAuth(settings)
        self._client = client
        self._owns_client = client is None
        # Meta documents publishing quota via /content_publishing_limit; the
        # client paces requests conservatively and reads the real quota.
        self.api_limiter = RateLimiter(60)

    # ------------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=120.0)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    @property
    def graph_base(self) -> str:
        base = (self.settings.instagram_facebook_graph_base
                if self.settings.instagram_login_mode == InstagramLoginMode.FACEBOOK_LOGIN
                else self.settings.instagram_graph_base)
        return f"{base.rstrip('/')}/{self.settings.instagram_api_version}"

    @property
    def rupload_base(self) -> str:
        return (f"{self.settings.instagram_rupload_base.rstrip('/')}/ig-api-upload/"
                f"{self.settings.instagram_api_version}")

    def supports_resumable(self) -> bool:
        """Meta documents resumable uploads for the Facebook Login configuration only."""
        return self.settings.instagram_login_mode == InstagramLoginMode.FACEBOOK_LOGIN

    def _token(self) -> str:
        return self.oauth.valid_tokens().access_token

    # ------------------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs) -> dict:
        attempts = 0
        last: Optional[InstagramAPIError] = None
        self.api_limiter.acquire()
        while attempts <= self.settings.instagram_max_retries:
            attempts += 1
            try:
                resp = self._http().request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last = InstagramAPIError(f"network error calling {url}: {exc}", retryable=True)
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                err = body.get("error") if isinstance(body, dict) else None
                if resp.status_code < 400 and not err:
                    return body if isinstance(body, dict) else {"data": body}
                code = (err or {}).get("code")
                message = (err or {}).get("message", resp.text[:200])
                # 4 = application request limit, 17 = user request limit,
                # 613 = calls-per-second limit, 2 = transient Meta error
                retryable = (resp.status_code == 429 or resp.status_code >= 500
                             or code in {1, 2, 4, 17, 613})
                last = InstagramAPIError(
                    f"Instagram API {method} {url.split('?')[0]} failed: "
                    f"HTTP {resp.status_code} code={code} message={message}",
                    status=resp.status_code, code=code, retryable=retryable)
                if not retryable:
                    raise last
            backoff = min(60.0, self.settings.instagram_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying Instagram request in %.1fs (attempt %d): %s",
                        backoff, attempts, last)
            time.sleep(backoff)
        raise last or InstagramAPIError(f"Instagram API {method} {url} failed")

    # ------------------------------------------------------------------
    def get_account(self) -> dict:
        """GET /me - confirms the token and the professional account type."""
        url = f"{self.graph_base}/me"
        return self._request("GET", url, params={
            "fields": "id,username,account_type,media_count",
            "access_token": self._token()})

    def publishing_limit(self) -> dict:
        """GET /{ig-user-id}/content_publishing_limit - the real, current quota."""
        account = self.get_account()
        url = f"{self.graph_base}/{account['id']}/content_publishing_limit"
        body = self._request("GET", url, params={
            "fields": "config,quota_usage", "access_token": self._token()})
        data = (body.get("data") or [{}])[0] if isinstance(body.get("data"), list) else body
        return data

    # ------------------------------------------------------------------
    def create_container(self, *, ig_user_id: str, caption: str = "",
                         video_url: str = "", resumable: bool = False,
                         share_to_feed: Optional[bool] = None,
                         extra: Optional[dict] = None) -> ContainerInfo:
        """POST /{ig-user-id}/media with media_type=REELS.

        Exactly one media source is used: ``video_url`` (public HTTPS) or a
        resumable session (local file). No cover_url and no thumb_offset are
        ever sent - the creator picks the cover in Instagram.
        """
        if resumable and not self.supports_resumable():
            raise InstagramAPIError(
                "Meta documents upload_type=resumable for the Facebook Login configuration "
                "only. With Instagram Login a public HTTPS video_url is required "
                "(set INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE or "
                "INSTAGRAM_LOGIN_MODE=FACEBOOK_LOGIN).")
        payload: Dict[str, Any] = {"media_type": "REELS", "access_token": self._token()}
        if caption:
            payload["caption"] = caption
        if share_to_feed is None:
            share_to_feed = self.settings.instagram_share_to_feed
        payload["share_to_feed"] = "true" if share_to_feed else "false"
        if resumable:
            payload["upload_type"] = "resumable"
        elif video_url:
            payload["video_url"] = video_url
        else:
            raise InstagramAPIError("either video_url or resumable upload must be used")
        if extra:
            payload.update(extra)

        url = f"{self.graph_base}/{ig_user_id}/media"
        body = self._request("POST", url, data=payload)
        container_id = str(body.get("id", ""))
        if not container_id:
            raise InstagramAPIError(f"container creation returned no id: {body}")
        return ContainerInfo(container_id=container_id, created_at=time.time(),
                             method="resumable" if resumable else "video_url", raw=body)

    def upload_video(self, container: ContainerInfo, video: Path,
                     *, progress: Optional[Callable[[int, int], None]] = None) -> ContainerInfo:
        """POST the local file to rupload.facebook.com (resumable session)."""
        if not self.supports_resumable():
            raise InstagramAPIError(
                "resumable local upload is not available for the Instagram Login "
                "configuration (Meta documentation)")
        size = video.stat().st_size
        url = f"{self.rupload_base}/{container.container_id}"
        headers = {
            "Authorization": f"OAuth {self._token()}",
            "offset": "0",
            "file_size": str(size),
        }
        attempts = 0
        last: Optional[InstagramAPIError] = None
        while attempts <= self.settings.instagram_max_retries:
            attempts += 1
            try:
                with video.open("rb") as fh:
                    resp = self._http().post(url, headers=headers, content=fh.read(),
                                             timeout=1800.0)
                if resp.status_code < 400:
                    container.uploaded_bytes = size
                    if progress:
                        progress(size, size)
                    return container
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = InstagramAPIError(
                    f"resumable upload failed: HTTP {resp.status_code} {resp.text[:200]}",
                    status=resp.status_code, retryable=retryable)
                if not retryable:
                    raise last
            except httpx.HTTPError as exc:
                last = InstagramAPIError(f"resumable upload network error: {exc}", retryable=True)
            # resume from the byte count Meta already has
            offset = self._resume_offset(container)
            headers["offset"] = str(offset)
            backoff = min(60.0, self.settings.instagram_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying Instagram upload in %.1fs from offset %s", backoff, offset)
            time.sleep(backoff)
        raise last or InstagramAPIError("resumable upload failed")

    def _resume_offset(self, container: ContainerInfo) -> int:
        """Ask the upload session how many bytes it already has (best effort)."""
        try:
            resp = self._http().get(f"{self.rupload_base}/{container.container_id}",
                                    headers={"Authorization": f"OAuth {self._token()}"},
                                    timeout=30.0)
            if resp.status_code < 400:
                data = resp.json()
                return int(data.get("offset", 0) or 0)
        except (httpx.HTTPError, ValueError, InstagramOAuthError):
            pass
        return 0

    def get_container_status(self, container: ContainerInfo) -> ContainerInfo:
        """GET /{container-id}?fields=status_code,status"""
        url = f"{self.graph_base}/{container.container_id}"
        body = self._request("GET", url, params={"fields": "status_code,status",
                                                 "access_token": self._token()})
        container.status_code = str(body.get("status_code") or container.status_code)
        container.status = str(body.get("status") or "")
        container.raw = body
        if (container.status_code not in TERMINAL_STATUSES
                and container.remaining_seconds(self.settings.instagram_container_ttl_hours) <= 0):
            # Meta expires containers after 24 h; reflect that even if the API
            # has not updated the status yet
            container.status_code = STATUS_EXPIRED
            container.status = "container older than the documented 24 h lifetime"
        return container

    def wait_for_container(self, container: ContainerInfo,
                           *, sleeper: Callable[[float], None] = time.sleep,
                           on_poll: Optional[Callable[[ContainerInfo], None]] = None
                           ) -> ContainerInfo:
        for _ in range(self.settings.instagram_status_poll_max):
            self.get_container_status(container)
            if on_poll:
                on_poll(container)
            if container.status_code in TERMINAL_STATUSES:
                return container
            sleeper(self.settings.instagram_status_poll_seconds)
        return container

    # ------------------------------------------------------------------
    def media_publish(self, *args, **kwargs):  # pragma: no cover - must never run
        """Publishing is disabled by design. This method always refuses."""
        raise PublishingDisabledError(
            "Instagram automatic publishing is disabled by design. "
            "The media container is staged only; publish it yourself in Instagram.")


class MockInstagramClient(InstagramClient):
    """INSTAGRAM_MOCK=true: exercises the flow without any network call (tests)."""

    def __init__(self, settings: Settings, oauth: Optional[InstagramOAuth] = None) -> None:
        super().__init__(settings, oauth=oauth)
        self.calls: list[str] = []

    def _token(self) -> str:
        return "mock-token"

    def get_account(self) -> dict:
        self.calls.append("get_account")
        return {"id": "17841400000000000", "username": "mock_creator",
                "account_type": "BUSINESS", "media_count": 0}

    def publishing_limit(self) -> dict:
        self.calls.append("publishing_limit")
        return {"quota_usage": 0, "config": {"quota_total": 100, "quota_duration": 86400}}

    def create_container(self, *, ig_user_id: str, caption: str = "", video_url: str = "",
                         resumable: bool = False, share_to_feed: Optional[bool] = None,
                         extra: Optional[dict] = None) -> ContainerInfo:
        self.calls.append("create_container")
        return ContainerInfo(container_id="mock-container-1", created_at=time.time(),
                             method="resumable" if resumable else "video_url",
                             raw={"mock": True, "caption": caption})

    def upload_video(self, container: ContainerInfo, video: Path, *, progress=None) -> ContainerInfo:
        self.calls.append("upload_video")
        container.uploaded_bytes = video.stat().st_size
        if progress:
            progress(container.uploaded_bytes, container.uploaded_bytes)
        return container

    def get_container_status(self, container: ContainerInfo) -> ContainerInfo:
        self.calls.append("get_container_status")
        container.status_code = STATUS_FINISHED
        return container

    def supports_resumable(self) -> bool:
        return True


def build_instagram_client(settings: Settings,
                           oauth: Optional[InstagramOAuth] = None) -> InstagramClient:
    import os
    if os.getenv("INSTAGRAM_MOCK", "").strip().lower() in {"1", "true", "yes"}:
        return MockInstagramClient(settings, oauth=oauth)
    return InstagramClient(settings, oauth=oauth)
