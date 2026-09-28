"""Local FastAPI dashboard + OAuth callback + control API."""
from __future__ import annotations

import os
import platform
import time
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import PlatformStatus, Settings, get_settings
from app.instagram.oauth import InstagramOAuth, InstagramOAuthError
from app.instagram.service import InstagramService
from app.jobs.pipeline import Pipeline
from app.platforms.base import PlatformError
from app.platforms.registry import PROVIDER_CLASSES, all_providers, build_provider, diagnose_all
from app.jobs.states import JobState
from app.storage.db import Database
from app.tiktok.oauth import OAuthError, TikTokOAuth
from app.watcher.watcher import WatcherService

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class Overrides(BaseModel):
    """Manual corrections a user can apply before reprocessing a job."""

    caption: Optional[str] = None
    title: Optional[str] = None


class AppContext:
    """Shared runtime objects for the dashboard and the watcher."""

    def __init__(self, settings: Optional[Settings] = None, autostart: bool = True) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.oauth = TikTokOAuth(self.settings)
        self.instagram_oauth = InstagramOAuth(self.settings)
        self.instagram = InstagramService(self.settings, self.instagram_oauth)
        self.pipeline = Pipeline(self.settings, self.db, oauth=self.oauth,
                                 instagram=self.instagram)
        self.watcher = WatcherService(self.settings, self.db, self.pipeline)
        self._lock = threading.Lock()
        if autostart:
            self.watcher.start()

    def close(self) -> None:
        self.watcher.stop()
        self.db.close()


def create_app(ctx: Optional[AppContext] = None, autostart: bool = True) -> FastAPI:
    context = ctx or AppContext(autostart=autostart)
    app = FastAPI(title="TikTok Cover & Upload Automation", version="1.0.0")
    app.state.ctx = context

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        s = context.settings
        return {
            "watcher": context.watcher.status(),
            "cover_policy": {
                "generated_by_app": False,
                "note": "Thumbnail/Cover: User creates and selects this manually in TikTok.",
            },
            "mode": {
                "effective": s.effective_mode.value,
                "requested": s.app_mode.value,
                "post_mode": s.tiktok_post_mode,
                "warnings": s.mode_warnings,
                "never_auto_publishes": s.effective_mode.value != "DIRECT_POST",
            },
            "settings": {
                "dry_run": s.dry_run,
                "tiktok_mock": s.tiktok_mock,
                "video_quality_mode": s.quality_mode.value,
                "post_mode": s.tiktok_post_mode,
                "effective_mode": s.effective_mode.value,
                "content_is_aigc": s.content_is_aigc,
                "input_dir": str(s.input_dir),
                "output_dir": str(s.output_dir),
                "language_default": s.default_language,
            },
            "tiktok": context.oauth.status(),
            "platforms": {
                "tiktok": {
                    "enabled": True,
                    "mode": s.effective_mode.value,
                    "connected": context.oauth.status()["authenticated"],
                    "note": "Draft upload - you finish and publish the post in TikTok.",
                },
                "instagram": context.instagram.status(),
                **{d["platform"]: d for d in diagnose_all(context.settings)},
            },
            "stats": context.db.stats(),
            "platform_stats": context.db.platform_stats(),
        }

    @app.get("/api/jobs")
    def jobs(limit: int = Query(30, ge=1, le=200)):
        return {"jobs": context.db.list_jobs(limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        job["events"] = context.db.events(job_id, limit=100)
        return job

    # ------------------------------------------------------------------
    @app.get("/api/jobs/{job_id}/caption")
    def job_caption(job_id: str):
        """Caption + hashtags for the draft flow (the inbox API accepts no caption)."""
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        plan = job.get("plan") or {}
        caption = plan.get("caption", "")
        path = Path(job["output_dir"]) / "caption.txt" if job.get("output_dir") else None
        if (not caption) and path and path.is_file():
            caption = path.read_text(encoding="utf-8")
        return {"caption": caption, "hashtags": plan.get("hashtags", []),
                "title": plan.get("tiktok_title", ""),
                "file": str(path) if path and path.is_file() else None,
                "note": ("TikTok's inbox/draft endpoint accepts no caption field - paste this "
                         "when you finish the post in the TikTok app.")}

    @app.post("/api/jobs/{job_id}/overrides")
    def set_overrides(job_id: str, overrides: Overrides):
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        merged = {**(job.get("overrides") or {}),
                  **{k: v for k, v in overrides.model_dump().items() if v}}
        context.db.update_job(job_id, overrides_json=merged)
        context.db.add_event(job_id, "OVERRIDES_UPDATED", data=merged)
        return {"ok": True, "overrides": merged}

    def _run_job(job_id: str, force: bool) -> None:
        owner = f"dashboard-{threading.get_ident()}"
        if not context.db.try_lock(job_id, owner):
            context.db.add_event(job_id, "LOCK_BUSY", message="job already running")
            return
        try:
            context.pipeline.run(job_id, force=force)
        finally:
            context.db.unlock(job_id)

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, background: BackgroundTasks, force: bool = False):
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        context.db.update_job(job_id, next_retry_at=None, error=None)
        context.db.set_state(job_id, JobState.RETRY_PENDING, enforce=False, message="manual retry")
        background.add_task(_run_job, job_id, force)
        return {"ok": True, "job_id": job_id, "force": force}

    @app.post("/api/watcher/{action}")
    def watcher_control(action: str):
        if action == "start":
            context.watcher.start()
        elif action == "stop":
            context.watcher.stop()
        elif action == "scan":
            context.watcher.scan_once()
        else:
            raise HTTPException(400, "unknown action")
        return context.watcher.status()

    @app.post("/api/open-folder")
    def open_folder(payload: Dict[str, str]):
        target = Path(payload.get("path") or context.settings.output_dir)
        if not target.exists():
            raise HTTPException(404, "path does not exist")
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.Popen(["open", str(target)])
            else:
                opener = shutil.which("xdg-open")
                if opener:
                    subprocess.Popen([opener, str(target)])
                else:
                    return {"ok": False, "detail": "no file manager available", "path": str(target)}
        except OSError as exc:
            return {"ok": False, "detail": str(exc), "path": str(target)}
        return {"ok": True, "path": str(target)}

    # ------------------------- optional platforms ---------------------
    @app.get("/api/platforms")
    def platforms_status():
        """One row per platform: mode, status, capability, credentials."""
        rows = [
            {"platform": "tiktok", "enabled": True,
             "mode": context.settings.effective_mode.value,
             "configured": context.oauth.status()["client_configured"],
             "authenticated": context.oauth.status()["authenticated"],
             "capability": "Official Content Posting API draft upload (never publishes)",
             "detail": "Draft upload - you finish and publish the post in TikTok."},
            {"platform": "instagram", **{
                k: v for k, v in context.instagram.status().items()
                if k in {"mode", "detail", "status"}},
             "enabled": context.settings.instagram_enabled,
             "configured": context.instagram.oauth.is_configured(),
             "authenticated": context.instagram.oauth.status()["authenticated"],
             "capability": "PREPARE_ONLY by default; optional API_STAGE_ONLY container"},
        ]
        rows.extend(diagnose_all(context.settings))
        return {"platforms": rows,
                "note": "No platform in this application ever publishes publicly."}

    @app.post("/api/platforms/{platform}/test")
    def platform_test(platform: str):
        if platform not in PROVIDER_CLASSES:
            raise HTTPException(404, f"unknown platform '{platform}'")
        provider = build_provider(platform, context.settings)
        diag = provider.diagnose().as_dict()
        try:
            provider.authenticate()
            diag["authenticated"] = True
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            diag["last_error"] = str(exc)
        return diag

    @app.post("/api/jobs/{job_id}/platforms/{platform}/run")
    def platform_run(job_id: str, platform: str, background: BackgroundTasks):
        """Run the single action this platform allows (upload-private or prepare)."""
        if platform not in PROVIDER_CLASSES:
            raise HTTPException(404, f"unknown platform '{platform}'")
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        provider = build_provider(platform, context.settings)
        if not provider.enabled:
            raise HTTPException(400, f"{platform} is disabled "
                                     f"({platform.upper()}_ENABLED=false)")
        video = job.get("final_video_path")
        if not video or not Path(video).is_file():
            raise HTTPException(400, "this job has no processed video yet")
        out_dir = Path(job.get("output_dir") or (context.settings.output_dir / job["base_name"]))
        plan_dict = job.get("plan") or {}

        class _Plan:
            tiktok_title = plan_dict.get("tiktok_title", "")
            caption = plan_dict.get("caption", "")
            hashtags = plan_dict.get("hashtags", [])

        def _work():
            context.db.set_platform(job_id, platform, PlatformStatus.UPLOADING.value,
                                    mode=provider.effective_mode.value, started=True)
            try:
                result = provider.run(
                    Path(video), _Plan(), out_dir,
                    on_event=lambda e, m, d: context.db.add_event(job_id, e, message=m,
                                                                  data=d or None))
            except Exception as exc:  # noqa: BLE001
                context.db.set_platform(job_id, platform, PlatformStatus.FAILED.value,
                                        mode=provider.effective_mode.value, error=str(exc),
                                        completed=True)
                return
            context.db.set_platform(job_id, platform, result.status.value,
                                    mode=result.mode.value, url=result.url,
                                    external_id=result.external_id, error=result.error,
                                    metadata=result.as_dict(), completed=True)
            context.db.add_event(job_id, f"{platform.upper()}_{result.status.value}",
                                 message=result.error or result.message or "")

        background.add_task(_work)
        return {"ok": True, "platform": platform, "mode": provider.effective_mode.value,
                "queued": True,
                "note": "This application never publishes publicly on any platform."}

    @app.get("/youtube/login")
    def youtube_login():
        from app.platforms.youtube import YouTubeProvider
        provider = YouTubeProvider(context.settings)
        if not provider.enabled:
            raise HTTPException(400, "YouTube is disabled (YOUTUBE_ENABLED=false)")
        if not provider.is_configured():
            raise HTTPException(400, "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET are missing")
        state = context.oauth.states.issue()
        return RedirectResponse(provider.authorize_url(state), status_code=307)

    @app.get("/youtube/callback")
    def youtube_callback(code: Optional[str] = None, state: Optional[str] = None,
                         error: Optional[str] = None):
        from app.platforms.base import PlatformError
        from app.platforms.youtube import YouTubeProvider
        if error:
            return HTMLResponse(f"<h2>YouTube authorisation failed</h2><p>{error}</p>",
                                status_code=400)
        if not code:
            return HTMLResponse("<h2>Missing authorisation code</h2>", status_code=400)
        if not context.oauth.states.validate(state):
            return HTMLResponse("<h2>OAuth state validation failed</h2>", status_code=400)
        try:
            tokens = YouTubeProvider(context.settings).exchange_code(code)
        except PlatformError as exc:
            return HTMLResponse(f"<h2>YouTube token exchange failed</h2><pre>{exc}</pre>",
                                status_code=400)
        return HTMLResponse(
            "<h2>YouTube connected</h2><p>Scopes: " + (tokens.scope or "") + "</p>"
            "<p>Uploads are always <b>private</b>; you set the thumbnail and visibility "
            "yourself in YouTube Studio.</p>")

    # ---------------------------- Instagram ---------------------------
    @app.get("/api/instagram/status")
    def instagram_status():
        return context.instagram.status()

    @app.post("/api/instagram/test-connection")
    def instagram_test_connection():
        """Verify the token and that this is an Instagram Professional account."""
        return context.instagram.test_connection()

    @app.get("/instagram/login")
    def instagram_login():
        if not context.settings.instagram_enabled:
            raise HTTPException(400, "Instagram integration is disabled (INSTAGRAM_ENABLED=false)")
        try:
            auth = context.instagram_oauth.authorize_url()
        except InstagramOAuthError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(auth.url, status_code=307)

    @app.get("/instagram/callback")
    def instagram_callback(code: Optional[str] = None, state: Optional[str] = None,
                           error: Optional[str] = None,
                           error_description: Optional[str] = None):
        if error:
            return HTMLResponse(f"<h2>Instagram authorisation failed</h2><p>{error}: "
                                f"{error_description or ''}</p>", status_code=400)
        if not code:
            return HTMLResponse("<h2>Missing authorisation code</h2>", status_code=400)
        try:
            tokens = context.instagram_oauth.exchange_code(code, state)
        except InstagramOAuthError as exc:
            return HTMLResponse(f"<h2>Instagram token exchange failed</h2><pre>{exc}</pre>",
                                status_code=400)
        return HTMLResponse(
            "<h2>Instagram connected</h2>"
            f"<p>Permissions: {tokens.permissions}</p>"
            "<p>Uploads are <b>staged only</b> - this app never publishes to Instagram. "
            "You can close this tab.</p>")

    @app.post("/api/instagram/refresh")
    def instagram_refresh():
        try:
            context.instagram_oauth.refresh()
        except InstagramOAuthError as exc:
            raise HTTPException(400, str(exc))
        return context.instagram_oauth.status()

    @app.post("/api/instagram/logout")
    def instagram_logout():
        context.instagram_oauth.logout()
        return context.instagram_oauth.status()

    def _stage_instagram(job_id: str) -> None:
        job = context.db.get_job(job_id)
        if not job or not job.get("final_video_path"):
            return
        plan_dict = job.get("plan") or {}

        class _Plan:                      # minimal shim for the caption builder
            tiktok_title = plan_dict.get("tiktok_title", "")
            caption = plan_dict.get("caption", "")
            hashtags = plan_dict.get("hashtags", [])

        from app.instagram.service import build_instagram_caption
        caption = build_instagram_caption(_Plan(), context.settings.caption_max_chars)
        context.db.set_platform(job_id, "instagram", PlatformStatus.UPLOADING.value, started=True)
        outcome = context.instagram.stage_video(
            Path(job["final_video_path"]), caption,
            on_event=lambda event, message, data: context.db.add_event(
                job_id, event, message=message, data=data or None))
        context.db.set_platform(
            job_id, "instagram", outcome.status.value, external_id=outcome.container_id,
            error=outcome.error,
            metadata=outcome.as_dict(context.settings.instagram_container_ttl_hours),
            completed=True)

    @app.post("/api/jobs/{job_id}/instagram/prepare")
    def instagram_prepare(job_id: str):
        """Prepare for Instagram - local files only, no Meta API call at all."""
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if not context.settings.instagram_enabled:
            raise HTTPException(400, "Instagram integration is disabled (INSTAGRAM_ENABLED=false)")
        video = job.get("final_video_path")
        if not video or not Path(video).is_file():
            raise HTTPException(400, "this job has no processed video yet")
        out_dir = Path(job.get("output_dir") or (context.settings.output_dir / job["base_name"]))
        plan_dict = job.get("plan") or {}

        class _Plan:
            tiktok_title = plan_dict.get("tiktok_title", "")
            caption = plan_dict.get("caption", "")
            hashtags = plan_dict.get("hashtags", [])

        from app.instagram.service import build_instagram_caption
        caption = build_instagram_caption(_Plan(), context.settings.caption_max_chars)
        result = context.instagram.prepare(Path(video), caption, out_dir)
        payload = result.as_dict()
        context.db.set_platform(job_id, "instagram", result.status.value,
                                error=result.error, metadata=payload, started=True,
                                completed=True)
        context.db.add_event(job_id, "INSTAGRAM_PREPARED",
                             message=str(result.video_path or result.error), data=payload)
        return payload

    @app.post("/api/jobs/{job_id}/instagram/stage")
    def instagram_stage(job_id: str, background: BackgroundTasks):
        """Optional API_STAGE_ONLY: create/refresh the media container. Never publishes."""
        job = context.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if not context.settings.instagram_api_staging:
            raise HTTPException(400,
                                "API staging is not enabled - set INSTAGRAM_ENABLED=true and "
                                "INSTAGRAM_MODE=API_STAGE_ONLY (a media container is NOT an "
                                "Instagram app draft and expires after 24 h)")
        if not job.get("final_video_path"):
            raise HTTPException(400, "this job has no processed video yet")
        background.add_task(_stage_instagram, job_id)
        return {"ok": True, "job_id": job_id, "queued": True,
                "note": "Upload only - this application never publishes to Instagram."}

    @app.post("/api/jobs/{job_id}/instagram/refresh-status")
    def instagram_refresh_status(job_id: str):
        record = context.db.get_platform(job_id, "instagram")
        if not record or not record.get("external_id"):
            raise HTTPException(404, "no Instagram container for this job")
        meta = record.get("metadata") or {}
        created = (meta.get("container") or {}).get("created_at") or time.time()
        outcome = context.instagram.refresh_container_status(record["external_id"], created)
        context.db.set_platform(job_id, "instagram", outcome.status.value,
                                external_id=outcome.container_id, error=outcome.error,
                                metadata=outcome.as_dict(
                                    context.settings.instagram_container_ttl_hours))
        return outcome.as_dict(context.settings.instagram_container_ttl_hours)

    # ------------------------------------------------------------------
    @app.get("/tiktok/login")
    def tiktok_login():
        try:
            auth = context.oauth.authorize_url()
        except OAuthError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(auth.url, status_code=307)

    @app.get("/tiktok/callback")
    def tiktok_callback(code: Optional[str] = None, state: Optional[str] = None,
                        error: Optional[str] = None, error_description: Optional[str] = None):
        if error:
            return HTMLResponse(f"<h2>TikTok authorisation failed</h2><p>{error}: "
                                f"{error_description or ''}</p>", status_code=400)
        if not code:
            return HTMLResponse("<h2>Missing authorisation code</h2>", status_code=400)
        try:
            tokens = context.oauth.exchange_code(code, state)
        except OAuthError as exc:
            return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{exc}</pre>", status_code=400)
        return HTMLResponse(
            "<h2>TikTok connected</h2><p>Scopes: "
            f"{tokens.scope}</p><p>You can close this tab and return to the dashboard.</p>")

    @app.post("/api/tiktok/refresh")
    def tiktok_refresh():
        try:
            context.oauth.refresh()
        except OAuthError as exc:
            raise HTTPException(400, str(exc))
        return context.oauth.status()

    @app.post("/api/tiktok/logout")
    def tiktok_logout():
        context.oauth.revoke()
        return context.oauth.status()

    @app.on_event("shutdown")
    def _shutdown():
        context.watcher.stop()

    return app
