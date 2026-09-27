"""Local FastAPI dashboard + OAuth callback + control API."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.jobs.pipeline import Pipeline
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
        self.pipeline = Pipeline(self.settings, self.db, oauth=self.oauth)
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
            "stats": context.db.stats(),
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
