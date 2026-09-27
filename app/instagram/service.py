"""Instagram UPLOAD_ONLY service.

Stages the already-produced MP4 as an Instagram Reels media container and stops
there. It never calls ``media_publish`` - that is refused by design (see
``InstagramClient.media_publish`` and ``AUTO_PUBLISH_REFUSED``).

Reality check that is surfaced everywhere in the UI and docs:

    Instagram media uploaded and ready to publish. Instagram does not provide
    the same visible draft/inbox workflow as TikTok. The media remains
    unpublished and must be finalized through the supported workflow.

An API-created container is **not** a draft in the Instagram mobile app.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import (InstagramLoginMode, InstagramUploadMethod, PlatformStatus, Settings)
from app.instagram.client import (STATUS_ERROR, STATUS_EXPIRED, STATUS_FINISHED,
                                  STATUS_PUBLISHED, ContainerInfo, InstagramAPIError,
                                  InstagramClient, build_instagram_client)
from app.instagram.oauth import InstagramOAuth, InstagramOAuthError
from app.instagram.validate import validate_reel

log = logging.getLogger(__name__)

READY_MESSAGE = (
    "Instagram media uploaded and ready to publish. Instagram does not provide the same "
    "visible draft/inbox workflow as TikTok. The media remains unpublished and must be "
    "finalized through the supported workflow."
)
AUTO_PUBLISH_REFUSED = "Instagram automatic publishing is disabled by design."


@dataclass
class InstagramResult:
    status: PlatformStatus
    message: str = ""
    container_id: Optional[str] = None
    container: Optional[ContainerInfo] = None
    account: Dict[str, Any] = field(default_factory=dict)
    caption: str = ""
    upload_method: str = ""
    validation: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def as_dict(self, ttl_hours: float = 24.0) -> dict:
        data = {
            "platform": "instagram",
            "status": self.status.value,
            "message": self.message,
            "container_id": self.container_id,
            "account": self.account,
            "caption": self.caption,
            "upload_method": self.upload_method,
            "validation": self.validation,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "auto_publish": False,
            "auto_publish_note": AUTO_PUBLISH_REFUSED,
            "cover": "selected manually by the user in Instagram",
        }
        if self.container:
            data["container"] = self.container.as_dict(ttl_hours)
        return data


def build_instagram_caption(plan, max_chars: int = 2200) -> str:
    """Instagram caption from the same parsed TITLE/DESCRIPTION metadata."""
    title = (getattr(plan, "tiktok_title", "") or "").strip()
    caption = (getattr(plan, "caption", "") or "").strip()
    hashtags = list(getattr(plan, "hashtags", []) or [])

    body = caption
    if title and not caption.lower().startswith(title.lower()[:40]):
        body = f"{title}\n\n{caption}" if caption else title
    tag_line = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    if tag_line and tag_line not in body:
        body = f"{body}\n\n{tag_line}".strip()
    if len(body) > max_chars:
        keep = max_chars - len(tag_line) - 2
        body = body[:max(0, keep)].rstrip() + ("\n\n" + tag_line if tag_line else "")
    return body.strip()[:max_chars]


class InstagramService:
    """High level: is Instagram usable, and stage one job's video."""

    def __init__(self, settings: Settings, oauth: Optional[InstagramOAuth] = None,
                 client_factory: Optional[Callable[[], InstagramClient]] = None) -> None:
        self.settings = settings
        self.oauth = oauth or InstagramOAuth(settings)
        self._client_factory = client_factory or (
            lambda: build_instagram_client(self.settings, self.oauth))

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings.instagram_enabled)

    def resolve_upload_method(self, video: Path) -> tuple[str, str]:
        """Return (method, reason). method is 'resumable', 'video_url' or ''."""
        configured = self.settings.instagram_upload_method
        resumable_ok = self.settings.instagram_login_mode == InstagramLoginMode.FACEBOOK_LOGIN
        public_url = self.public_video_url(video)

        if configured == InstagramUploadMethod.RESUMABLE:
            if not resumable_ok:
                return "", ("INSTAGRAM_UPLOAD_METHOD=RESUMABLE requires "
                            "INSTAGRAM_LOGIN_MODE=FACEBOOK_LOGIN - Meta documents resumable "
                            "uploads for the Facebook Login configuration only")
            return "resumable", "local file upload to rupload.facebook.com"
        if configured == InstagramUploadMethod.VIDEO_URL:
            if not public_url:
                return "", ("INSTAGRAM_UPLOAD_METHOD=VIDEO_URL requires "
                            "INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE")
            return "video_url", f"public URL {public_url}"
        # AUTO
        if resumable_ok:
            return "resumable", "local file upload to rupload.facebook.com (Facebook Login)"
        if public_url:
            return "video_url", f"public URL {public_url}"
        return "", ("Instagram Login cannot upload local files (Meta documents "
                    "upload_type=resumable for Facebook Login only). Provide a public HTTPS "
                    "URL via INSTAGRAM_PUBLIC_VIDEO_URL_TEMPLATE, or switch to "
                    "INSTAGRAM_LOGIN_MODE=FACEBOOK_LOGIN.")

    def public_video_url(self, video: Path) -> str:
        template = (self.settings.instagram_public_video_url_template or "").strip()
        if not template:
            return ""
        url = (template.replace("{filename}", video.name)
                       .replace("{name}", video.stem)
                       .replace("{parent}", video.parent.name))
        return url

    # ------------------------------------------------------------------
    def status(self, video: Optional[Path] = None) -> dict:
        """Dashboard/diagnostics view of the Instagram integration."""
        if not self.enabled:
            return {"status": PlatformStatus.DISABLED.value,
                    "detail": "Instagram integration disabled (INSTAGRAM_ENABLED=false)",
                    "mode": self.settings.instagram_mode.value,
                    "auto_publish": False, "auto_publish_note": AUTO_PUBLISH_REFUSED,
                    "cover": "selected manually by the user in Instagram"}
        auth = self.oauth.status()
        if not auth["client_configured"]:
            status, detail = (PlatformStatus.NOT_CONFIGURED,
                              "INSTAGRAM_CLIENT_ID / INSTAGRAM_CLIENT_SECRET missing in .env")
        elif not auth["authenticated"]:
            status, detail = (PlatformStatus.AUTH_REQUIRED,
                              "not connected - click Connect Instagram in the dashboard")
        elif auth["expired"]:
            status, detail = (PlatformStatus.AUTH_REQUIRED,
                              "the Instagram access token expired - reconnect the account")
        elif auth["missing_scopes"]:
            status, detail = (PlatformStatus.NOT_CONFIGURED,
                              f"missing permissions: {', '.join(auth['missing_scopes'])}")
        else:
            status, detail = PlatformStatus.READY_TO_PUBLISH, "connected and ready to stage media"
        method, reason = self.resolve_upload_method(video or Path("final_tiktok.mp4"))
        if status == PlatformStatus.READY_TO_PUBLISH and not method:
            status, detail = PlatformStatus.NOT_CONFIGURED, reason
        return {
            "status": status.value,
            "detail": detail,
            "mode": self.settings.instagram_mode.value,
            "login_mode": self.settings.instagram_login_mode.value,
            "upload_method": method or "unavailable",
            "upload_method_reason": reason,
            "auto_publish": False,
            "auto_publish_note": AUTO_PUBLISH_REFUSED,
            "cover": "selected manually by the user in Instagram",
            "auth": auth,
            "note": READY_MESSAGE,
        }

    def test_connection(self) -> dict:
        """Verify the token and that the account is an Instagram Professional one."""
        if not self.enabled:
            return {"ok": False, "status": PlatformStatus.DISABLED.value,
                    "detail": "Instagram integration is disabled"}
        client = self._client_factory()
        try:
            account = client.get_account()
            account_type = str(account.get("account_type", "")).upper()
            professional = account_type in {"BUSINESS", "CREATOR", "MEDIA_CREATOR"}
            limit = {}
            try:
                limit = client.publishing_limit()
            except InstagramAPIError as exc:
                limit = {"error": str(exc)}
            return {"ok": professional, "status": (PlatformStatus.READY_TO_PUBLISH.value
                                                   if professional
                                                   else PlatformStatus.NOT_CONFIGURED.value),
                    "account": account, "account_type": account_type,
                    "professional": professional,
                    "publishing_limit": limit,
                    "detail": ("connected" if professional else
                               "this is not an Instagram Professional (Business/Creator) "
                               "account - the Content Publishing API cannot be used with it")}
        except (InstagramAPIError, InstagramOAuthError) as exc:
            return {"ok": False, "status": PlatformStatus.FAILED.value, "detail": str(exc)}
        finally:
            client.close()

    # ------------------------------------------------------------------
    def stage_video(self, video: Path, caption: str, *,
                    on_event: Optional[Callable[[str, str, dict], None]] = None
                    ) -> InstagramResult:
        """Create the container, upload the media, poll until ready. Never publishes."""
        def emit(event: str, message: str = "", **data: Any) -> None:
            if on_event:
                on_event(event, message, data)

        result = InstagramResult(status=PlatformStatus.UPLOADING, caption=caption)

        if not self.enabled:
            result.status = PlatformStatus.DISABLED
            result.message = "Instagram integration disabled"
            return result
        if self.settings.instagram_auto_publish:
            # even if someone flips the flag, we refuse - by design
            log.warning("INSTAGRAM_AUTO_PUBLISH=true ignored: %s", AUTO_PUBLISH_REFUSED)
            emit("INSTAGRAM_AUTO_PUBLISH_REFUSED", AUTO_PUBLISH_REFUSED)

        # ---- validate against the Reels specs (no second transcode if it passes)
        check = validate_reel(video, self.settings)
        result.validation = check.as_dict()
        if not check.ok:
            result.status = PlatformStatus.FAILED
            result.error = "final video does not meet Instagram Reels requirements: " + \
                           "; ".join(check.errors)
            return result

        method, reason = self.resolve_upload_method(video)
        result.upload_method = method
        if not method:
            result.status = PlatformStatus.NOT_CONFIGURED
            result.error = reason
            return result

        client = self._client_factory()
        try:
            account = client.get_account()
            result.account = account
            ig_user_id = str(account.get("id", ""))
            if not ig_user_id:
                raise InstagramAPIError("could not determine the Instagram user id")

            emit("INSTAGRAM_UPLOAD_STARTED", f"method={method} account={account.get('username')}")
            container = client.create_container(
                ig_user_id=ig_user_id,
                caption=caption,
                video_url="" if method == "resumable" else self.public_video_url(video),
                resumable=(method == "resumable"),
            )
            result.container = container
            result.container_id = container.container_id
            emit("INSTAGRAM_CONTAINER_CREATED", container.container_id,
                 container_id=container.container_id, method=method)

            if method == "resumable":
                client.upload_video(container, video)
                emit("INSTAGRAM_MEDIA_UPLOADED", f"{container.uploaded_bytes} bytes")

            result.status = PlatformStatus.PROCESSING
            client.wait_for_container(
                container,
                on_poll=lambda c: emit("INSTAGRAM_CONTAINER_STATUS", c.status_code,
                                       status=c.status_code))

            if container.status_code in {STATUS_FINISHED, STATUS_PUBLISHED}:
                result.status = PlatformStatus.READY_TO_PUBLISH
                result.message = READY_MESSAGE
                emit("INSTAGRAM_READY_TO_PUBLISH",
                     f"container {container.container_id} ready (not published)")
            elif container.status_code == STATUS_EXPIRED:
                result.status = PlatformStatus.EXPIRED
                result.error = ("the media container expired (Meta expires containers 24 h "
                                "after creation) - stage the video again")
                emit("INSTAGRAM_CONTAINER_EXPIRED", result.error)
            elif container.status_code == STATUS_ERROR:
                result.status = PlatformStatus.FAILED
                result.error = f"Instagram reported ERROR for the container: {container.status}"
                emit("INSTAGRAM_FAILED", result.error)
            else:
                result.status = PlatformStatus.PROCESSING
                result.message = ("Instagram is still processing the media; check again later "
                                  "with Prepare Instagram Upload")
        except (InstagramAPIError, InstagramOAuthError) as exc:
            result.status = (PlatformStatus.AUTH_REQUIRED
                             if isinstance(exc, InstagramOAuthError) else PlatformStatus.FAILED)
            result.error = str(exc)
            emit("INSTAGRAM_FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001 - Instagram must never kill a TikTok job
            log.exception("unexpected Instagram failure")
            result.status = PlatformStatus.FAILED
            result.error = f"unexpected {exc.__class__.__name__}: {exc}"
            emit("INSTAGRAM_FAILED", result.error)
        finally:
            client.close()
            result.completed_at = time.time()
        return result

    def refresh_container_status(self, container_id: str, created_at: float) -> InstagramResult:
        """Re-check a previously staged container (e.g. expiry detection)."""
        result = InstagramResult(status=PlatformStatus.PROCESSING, container_id=container_id)
        if not self.enabled:
            result.status = PlatformStatus.DISABLED
            return result
        container = ContainerInfo(container_id=container_id, created_at=created_at)
        result.container = container
        ttl = self.settings.instagram_container_ttl_hours
        if container.remaining_seconds(ttl) <= 0:
            # Meta expires containers after 24 h - no point calling the API
            container.status_code = STATUS_EXPIRED
            result.status = PlatformStatus.EXPIRED
            result.error = (f"container expired ({ttl:.0f} h lifetime); stage the video again")
            return result
        client = self._client_factory()
        try:
            client.get_container_status(container)
            result.container = container
            if container.status_code in {STATUS_FINISHED, STATUS_PUBLISHED}:
                result.status = PlatformStatus.READY_TO_PUBLISH
                result.message = READY_MESSAGE
            elif container.status_code == STATUS_EXPIRED:
                result.status = PlatformStatus.EXPIRED
                result.error = "container expired (24 h lifetime)"
            elif container.status_code == STATUS_ERROR:
                result.status = PlatformStatus.FAILED
                result.error = container.status or "Instagram reported ERROR"
        except (InstagramAPIError, InstagramOAuthError) as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
        finally:
            client.close()
        return result
