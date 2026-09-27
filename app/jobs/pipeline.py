"""The job pipeline: parse -> analyze -> prepare video -> upload as TikTok draft.

There is deliberately **no thumbnail/cover generation**: the creator makes and
selects the cover manually inside TikTok after the draft arrives in the inbox.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import AppMode, PlatformStatus, Settings
from app.content.analyzer import ContentPlan, analyze
from app.instagram.oauth import InstagramOAuth
from app.instagram.service import InstagramService, build_instagram_caption
from app.jobs.states import JobState
from app.platforms.registry import enabled_providers
from app.logging_setup import log_event
from app.parsing.text_parser import ParseError, parse_file
from app.storage.db import Database, utcnow
from app.tiktok.client import TikTokAPIError, UploadOutcome, build_client
from app.tiktok.oauth import OAuthError, TikTokOAuth
from app.video.builder import build_tiktok_video
from app.video.ffmpeg import FFmpegError, probe
from app.video.validate import validate_video

log = logging.getLogger(__name__)

RETRYABLE_STAGES = {"video", "upload"}


class PipelineError(RuntimeError):
    def __init__(self, message: str, *, stage: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass
class JobPaths:
    work_dir: Path
    out_dir: Path
    final_video: Path

    @classmethod
    def build(cls, settings: Settings, job_id: str, base_name: str) -> "JobPaths":
        work = settings.processing_dir / f"{base_name}_{job_id}"
        out = settings.output_dir / base_name
        return cls(work_dir=work, out_dir=out, final_video=work / "final_tiktok.mp4")


class Pipeline:
    def __init__(self, settings: Settings, db: Database,
                 oauth: Optional[TikTokOAuth] = None,
                 instagram: Optional[InstagramService] = None) -> None:
        self.settings = settings
        self.db = db
        self.oauth = oauth or TikTokOAuth(settings)
        #: optional second platform; completely inert while INSTAGRAM_ENABLED=false
        self.instagram = instagram or InstagramService(settings, InstagramOAuth(settings))

    # ------------------------------------------------------------------
    def _emit(self, job_id: str, event: str, message: str = "", **data: Any) -> None:
        self.db.add_event(job_id, event, message=message, data=data or None)
        log_event(log, event, message, job_id=job_id)

    # ------------------------------------------------------------------
    def run(self, job_id: str, *, force: bool = False) -> Dict[str, Any]:
        job = self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        overrides = job.get("overrides") or {}
        video_path = Path(job["video_path"])
        meta_path = Path(job["metadata_path"]) if job.get("metadata_path") else None
        paths = JobPaths.build(self.settings, job_id, job["base_name"])
        paths.work_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()

        try:
            # ---------------- validate & dedupe ----------------
            self.db.set_state(job_id, JobState.VALIDATING, enforce=False)
            self._emit(job_id, "VALIDATING", video_path.name)
            if not video_path.is_file():
                raise PipelineError(f"video file disappeared: {video_path}", stage="validate")
            if meta_path is None or not meta_path.is_file():
                raise PipelineError("metadata text file is missing", stage="validate")

            video_hash = sha256_file(video_path)
            meta_hash = sha256_file(meta_path)
            self.db.update_job(job_id, video_sha256=video_hash, metadata_sha256=meta_hash)

            duplicates = [d for d in self.db.find_by_hash(video_hash, exclude_job=job_id)
                          if d["state"] in {JobState.COMPLETED.value, JobState.UPLOADED.value}]
            if duplicates and not force:
                self.db.set_state(job_id, JobState.DUPLICATE, enforce=False,
                                  error=f"duplicate of job {duplicates[0]['id']}")
                self._emit(job_id, "DUPLICATE_DETECTED",
                           f"already processed as job {duplicates[0]['id']}")
                return self.db.get_job(job_id)

            try:
                source_info = probe(video_path, self.settings)
            except FFmpegError as exc:
                raise PipelineError(f"source video is not readable: {exc}", stage="validate")

            # ---------------- parse + understand ----------------
            self.db.set_state(job_id, JobState.GENERATING_METADATA, enforce=False)
            try:
                parsed = parse_file(meta_path, video_file=str(video_path))
            except (ParseError, OSError) as exc:
                raise PipelineError(f"metadata file could not be parsed: {exc}", stage="parse")

            plan: ContentPlan = analyze(
                parsed,
                default_language=self.settings.default_language,
                caption_max_chars=self.settings.caption_max_chars,
                caption_override=overrides.get("caption"),
                title_override=overrides.get("title"),
            )
            self.db.update_job(job_id, parsed_json=parsed.to_dict(), plan_json=plan.to_dict())
            self._emit(job_id, "METADATA_READY",
                       f"lang={plan.language} topic={plan.topic} "
                       f"hashtags={' '.join(plan.hashtags)}")

            # ---------------- video ----------------
            self.db.set_state(job_id, JobState.BUILDING_VIDEO, enforce=False)
            try:
                build = build_tiktok_video(video_path, paths.final_video, self.settings)
            except FFmpegError as exc:
                raise PipelineError(f"ffmpeg failed to build the video: {exc}", stage="video",
                                    retryable=True)
            self.db.set_state(job_id, JobState.VALIDATING_VIDEO, enforce=False)
            vcheck = validate_video(build.output_path, self.settings,
                                    source_had_audio=source_info.has_audio,
                                    expect_width=self.settings.target_width,
                                    expect_height=self.settings.target_height)
            if not vcheck.ok:
                raise PipelineError("processed video failed validation: "
                                    + "; ".join(vcheck.errors), stage="video")
            self.db.update_job(job_id, final_video_path=str(build.output_path))
            self._emit(job_id, "VIDEO_READY",
                       f"{build.output_path.name} ({build.processing})",
                       processing=build.processing, reasons=build.reasons,
                       scaling=build.scaling_mode, warnings=vcheck.warnings)

            # ---------------- platform: TikTok ----------------
            upload_result: Dict[str, Any]
            if job["dry_run"]:
                upload_result = {"skipped": True, "reason": "DRY_RUN=true",
                                 "would_upload": str(build.output_path),
                                 "mode": self.settings.effective_mode.value,
                                 "caption_preview": plan.caption[:200]}
                self._emit(job_id, "UPLOAD_SKIPPED", "DRY_RUN=true")
                self.db.set_platform(job_id, "tiktok", PlatformStatus.SKIPPED.value,
                                     metadata=upload_result, started=True, completed=True)
            else:
                self.db.set_state(job_id, JobState.UPLOADING, enforce=False)
                self.db.set_platform(job_id, "tiktok", PlatformStatus.UPLOADING.value,
                                     started=True)
                try:
                    upload_result = self._upload(job_id, build.output_path, plan)
                except (OAuthError, TikTokAPIError) as exc:
                    self.db.set_platform(job_id, "tiktok", PlatformStatus.FAILED.value,
                                         error=str(exc), completed=True)
                    raise
                self.db.set_state(job_id, JobState.UPLOADED, enforce=False,
                                  publish_id=upload_result.get("publish_id"),
                                  upload_status=upload_result.get("status"))
                self.db.set_platform(job_id, "tiktok", PlatformStatus.READY_TO_PUBLISH.value,
                                     external_id=upload_result.get("publish_id"),
                                     metadata=upload_result, completed=True)

            self.db.update_job(job_id, upload_result_json=upload_result)

            # ---------------- platform: Instagram (optional, upload only) -------
            instagram_result = self._stage_instagram(job_id, build.output_path, plan,
                                                     dry_run=bool(job["dry_run"]))

            # ---------------- optional extra platforms (all off by default) -----
            self._run_extra_platforms(job_id, build.output_path, plan, paths.out_dir,
                                      dry_run=bool(job["dry_run"]))

            # ---------------- publish artifacts ----------------
            out_dir = self._write_output(job_id, paths, video_path, meta_path,
                                         build, plan, upload_result, vcheck.as_dict(),
                                         instagram_result)
            self.db.update_job(job_id, output_dir=str(out_dir), error=None, next_retry_at=None)
            self.db.set_state(job_id, JobState.COMPLETED, enforce=False)
            self._emit(job_id, "COMPLETED", f"{time.time() - started:.1f}s -> {out_dir}")
            return self.db.get_job(job_id)

        except PipelineError as exc:
            self._fail(job_id, str(exc), stage=exc.stage, retryable=exc.retryable)
            return self.db.get_job(job_id)
        except (OAuthError, TikTokAPIError) as exc:
            retryable = isinstance(exc, TikTokAPIError) and exc.retryable
            self._fail(job_id, str(exc), stage="upload", retryable=retryable)
            return self.db.get_job(job_id)
        except Exception as exc:  # noqa: BLE001 - the watcher must survive anything
            log.exception("unexpected pipeline failure")
            self._fail(job_id, f"unexpected {exc.__class__.__name__}: {exc}", stage="unknown",
                       retryable=False)
            return self.db.get_job(job_id)

    # ------------------------------------------------------------------
    def _upload(self, job_id: str, video: Path, plan: ContentPlan) -> Dict[str, Any]:
        client = build_client(self.settings, self.oauth)
        mode = ("DIRECT_POST" if self.settings.effective_mode == AppMode.DIRECT_POST
                else "UPLOAD")
        self._emit(job_id, "TIKTOK_UPLOAD_STARTED", f"mode={mode} size={video.stat().st_size}")

        def progress(sent: int, total: int) -> None:
            if total and (sent == total or sent % (32 * 1024 * 1024) < 1024):
                self.db.add_event(job_id, "UPLOAD_PROGRESS",
                                  message=f"{sent}/{total} bytes")

        try:
            if mode == "DIRECT_POST":
                creator = client.query_creator_info()
                allowed = creator.get("privacy_level_options") or ["SELF_ONLY"]
                privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]
                post_info = {
                    "title": plan.caption[:self.settings.caption_max_chars],
                    "privacy_level": privacy,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                }
                if self.settings.content_is_aigc:
                    post_info["is_aigc"] = True
                outcome: UploadOutcome = client.direct_post(video, post_info, progress=progress)
            else:
                # documented inbox/draft flow: no post_info fields are accepted here
                outcome = client.upload_draft(video, progress=progress)
            status = client.wait_for_status(outcome.publish_id) if outcome.publish_id else {}
        finally:
            client.close()

        outcome.raw_status = status or {}
        outcome.status = (status or {}).get("status", outcome.status)
        result = outcome.as_dict()
        result.update({
            "uploaded_at": utcnow(),
            "caption_for_review": plan.caption,
            "hashtags": plan.hashtags,
            "is_aigc_flag_sent": bool(self.settings.content_is_aigc) and mode == "DIRECT_POST",
        })
        self._emit(job_id, "TIKTOK_DRAFT_READY" if outcome.mode == "UPLOAD" else "TIKTOK_POST_SUBMITTED",
                   f"publish_id={outcome.publish_id} status={outcome.status}")
        return result

    # ------------------------------------------------------------------
    def _stage_instagram(self, job_id: str, video: Path, plan: ContentPlan,
                         *, dry_run: bool) -> Optional[Dict[str, Any]]:
        """Optional second platform. A failure here never fails the TikTok job."""
        if not self.settings.instagram_enabled:
            return None
        caption = build_instagram_caption(plan, self.settings.caption_max_chars)

        if not self.settings.instagram_api_staging:
            # PREPARE_ONLY (default): never touch the Meta API during a watched
            # folder job. The caption is written, and the user presses
            # "Prepare for Instagram" in the dashboard when they want the file.
            result = {"platform": "instagram",
                      "mode": self.settings.instagram_effective_mode.value,
                      "status": PlatformStatus.SKIPPED.value,
                      "message": ("No Instagram API call is made in PREPARE_ONLY mode. Use "
                                  "'Prepare for Instagram' on this job to produce "
                                  "instagram_ready.mp4 for a manual upload."),
                      "caption": caption, "auto_publish": False, "api_calls_made": 0}
            self.db.set_platform(job_id, "instagram", PlatformStatus.SKIPPED.value,
                                 metadata=result, started=True, completed=True)
            self._emit(job_id, "INSTAGRAM_PREPARE_AVAILABLE",
                       "PREPARE_ONLY - no media container created")
            return result

        if dry_run:
            result = {"platform": "instagram", "status": PlatformStatus.SKIPPED.value,
                      "message": "DRY_RUN=true - nothing was sent to Instagram",
                      "caption": caption, "auto_publish": False}
            self.db.set_platform(job_id, "instagram", PlatformStatus.SKIPPED.value,
                                 metadata=result, started=True, completed=True)
            self._emit(job_id, "INSTAGRAM_UPLOAD_SKIPPED", "DRY_RUN=true")
            return result

        self.db.set_platform(job_id, "instagram", PlatformStatus.UPLOADING.value, started=True)
        outcome = self.instagram.stage_video(
            video, caption,
            on_event=lambda event, message, data: self._emit(job_id, event, message, **data))
        result = outcome.as_dict(self.settings.instagram_container_ttl_hours)
        self.db.set_platform(job_id, "instagram", outcome.status.value,
                             external_id=outcome.container_id, error=outcome.error,
                             metadata=result, completed=True)
        if outcome.error:
            # recorded, but the TikTok result stays intact and the job still completes
            self._emit(job_id, "INSTAGRAM_STAGE_FAILED", outcome.error)
        return result

    # ------------------------------------------------------------------
    def _run_extra_platforms(self, job_id: str, video: Path, plan: ContentPlan,
                             out_dir: Path, *, dry_run: bool) -> None:
        """Vimeo / Dailymotion / SoundCloud / Patreon / Rumble.

        Each platform is independent: a failure is recorded on that platform's
        row only and never affects TikTok, Instagram or the job outcome.
        """
        for provider in enabled_providers(self.settings):
            name = provider.name
            try:
                if dry_run and provider.effective_mode.value in {"UPLOAD_PRIVATE",
                                                                 "API_STAGE_ONLY"}:
                    payload = {"platform": name, "mode": provider.effective_mode.value,
                               "status": PlatformStatus.SKIPPED.value,
                               "message": "DRY_RUN=true - no API call was made",
                               "auto_publish": False}
                    self.db.set_platform(job_id, name, PlatformStatus.SKIPPED.value,
                                         mode=provider.effective_mode.value, metadata=payload,
                                         started=True, completed=True)
                    self._emit(job_id, f"{name.upper()}_SKIPPED", "DRY_RUN=true")
                    continue

                self.db.set_platform(job_id, name, PlatformStatus.UPLOADING.value,
                                     mode=provider.effective_mode.value, started=True)
                result = provider.run(
                    video, plan, out_dir,
                    on_event=lambda event, message, data, _j=job_id: self._emit(
                        _j, event, message, **(data or {})))
                self.db.set_platform(job_id, name, result.status.value,
                                     mode=result.mode.value, url=result.url,
                                     external_id=result.external_id, error=result.error,
                                     metadata=result.as_dict(), completed=True)
                self._emit(job_id, f"{name.upper()}_{result.status.value}",
                           result.error or result.message or "")
            except Exception as exc:  # noqa: BLE001 - isolate every platform
                log.exception("platform %s failed", name)
                self.db.set_platform(job_id, name, PlatformStatus.FAILED.value,
                                     mode=provider.effective_mode.value,
                                     error=f"unexpected {exc.__class__.__name__}: {exc}",
                                     completed=True)
                self._emit(job_id, f"{name.upper()}_FAILED", str(exc))

    # ------------------------------------------------------------------
    def _write_output(self, job_id: str, paths: JobPaths, video: Path, meta: Path,
                      build, plan: ContentPlan, upload_result: dict,
                      video_check: dict,
                      instagram_result: Optional[dict] = None) -> Path:
        out = paths.out_dir
        out.mkdir(parents=True, exist_ok=True)
        # copies only - the originals in input/ are never moved or modified
        shutil.copy2(meta, out / f"metadata{meta.suffix}")
        final_dest = out / "final_tiktok.mp4"
        if build.output_path.resolve() != final_dest.resolve():
            shutil.copy2(build.output_path, final_dest)
        job_now = self.db.get_job(job_id) or {}
        (out / "source_reference.txt").write_text(
            f"original video: {video}\nsha256: {job_now.get('video_sha256')}\n",
            encoding="utf-8")
        if self.settings.copy_source_to_output:
            source_copy = out / f"source{video.suffix.lower()}"
            if not source_copy.exists() or source_copy.stat().st_size != video.stat().st_size:
                shutil.copy2(video, source_copy)
        job = job_now or self.db.get_job(job_id)
        (out / "job.json").write_text(json.dumps({
            "job_id": job_id,
            "created_at": job["created_at"],
            "base_name": job["base_name"],
            "source_video": str(video),
            "video_sha256": job["video_sha256"],
            "metadata_sha256": job["metadata_sha256"],
            "plan": plan.to_dict(),
            "cover": {"generated": False,
                      "note": "Cover/thumbnail is created and selected manually by the user "
                              "inside TikTok; this application never generates or embeds one."},
            "video_build": build.as_dict(),
            "video_validation": video_check,
            "dry_run": job["dry_run"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "upload_result.json").write_text(
            json.dumps(upload_result, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "caption.txt").write_text(plan.caption, encoding="utf-8")

        if self.settings.instagram_enabled:
            caption_ig = build_instagram_caption(plan, self.settings.caption_max_chars)
            (out / "caption_instagram.txt").write_text(caption_ig, encoding="utf-8")
            # instagram.json / instagram_status.json describe an API media container,
            # so they only exist in API_STAGE_ONLY mode. PREPARE_ONLY writes
            # instagram_preparation.json instead, when you press "Prepare for Instagram".
            if instagram_result is not None and self.settings.instagram_api_staging:
                (out / "instagram.json").write_text(
                    json.dumps(instagram_result, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
                record = self.db.get_platform(job_id, "instagram") or {}
                (out / "instagram_status.json").write_text(json.dumps({
                    "status": record.get("status"),
                    "container_id": record.get("external_id"),
                    "started_at": record.get("started_at"),
                    "completed_at": record.get("completed_at"),
                    "error": record.get("error"),
                    "auto_publish": False,
                    "note": ("Instagram media uploaded and ready to publish. Instagram does "
                             "not provide the same visible draft/inbox workflow as TikTok. "
                             "The media remains unpublished and must be finalized through "
                             "the supported workflow."),
                    "cover": "selected manually by the user in Instagram",
                }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return out

    # ------------------------------------------------------------------
    def _fail(self, job_id: str, error: str, *, stage: str, retryable: bool) -> None:
        job = self.db.get_job(job_id) or {}
        attempts = int(job.get("attempts") or 0) + 1
        max_attempts = self.settings.tiktok_max_retries
        will_retry = retryable and attempts < max_attempts
        next_retry = None
        if will_retry:
            delay = min(900, self.settings.tiktok_backoff_base_seconds * (2 ** attempts))
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(
                timespec="seconds")
        self.db.update_job(job_id, attempts=attempts, error=f"[{stage}] {error}",
                           next_retry_at=next_retry)
        self.db.set_state(job_id, JobState.RETRY_PENDING if will_retry else JobState.FAILED,
                          enforce=False, message=error)
        self._emit(job_id, "RETRY_PENDING" if will_retry else "FAILED", error,
                   stage=stage, attempts=attempts, next_retry_at=next_retry)
        if not will_retry:
            try:
                marker = self.settings.failed_dir / f"{job.get('base_name', job_id)}_{job_id}.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({
                    "job_id": job_id, "stage": stage, "error": error,
                    "video_path": job.get("video_path"),
                    "metadata_path": job.get("metadata_path"),
                    "attempts": attempts, "ts": utcnow(),
                }, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                log.warning("could not write failure marker for job %s", job_id)
