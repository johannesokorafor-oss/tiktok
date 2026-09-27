"""The job pipeline: parse -> analyze -> cover -> video -> upload."""
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

from app.config import AppMode, CoverSource, QualityMode, Settings, StylePreset
from app.content.analyzer import ContentPlan, analyze
from app.cover.compose import CoverComposer
from app.cover.presets import get_preset
from app.cover.validate import validate_cover
from app.images.base import ImageProviderError
from app.images.registry import ImageService, NoGenerativeProviderError
from app.jobs.states import JobState
from app.logging_setup import log_event
from app.parsing.text_parser import ParseError, parse_file
from app.storage.db import Database, utcnow
from app.tiktok.client import TikTokAPIError, UploadOutcome, build_client
from app.tiktok.oauth import OAuthError, TikTokOAuth
from app.video.builder import build_tiktok_video, extract_frame
from app.video.ffmpeg import FFmpegError, probe
from app.video.validate import validate_video

log = logging.getLogger(__name__)

RETRYABLE_STAGES = {"image", "video", "upload"}


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
    cover: Path
    final_video: Path

    @classmethod
    def build(cls, settings: Settings, job_id: str, base_name: str) -> "JobPaths":
        work = settings.processing_dir / f"{base_name}_{job_id}"
        out = settings.output_dir / base_name
        return cls(work_dir=work, out_dir=out,
                   cover=settings.covers_dir / f"{base_name}_{job_id}.png",
                   final_video=work / "final_tiktok.mp4")


class Pipeline:
    def __init__(self, settings: Settings, db: Database,
                 oauth: Optional[TikTokOAuth] = None,
                 image_service: Optional[ImageService] = None) -> None:
        self.settings = settings
        self.db = db
        self.oauth = oauth or TikTokOAuth(settings)
        self.images = image_service or ImageService(settings)

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

            configured_style = self.settings.style_preset
            if overrides.get("style"):
                try:
                    configured_style = StylePreset(overrides["style"].upper())
                except ValueError:
                    pass
            plan: ContentPlan = analyze(
                parsed,
                configured_style=configured_style,
                default_language=self.settings.default_language,
                caption_max_chars=self.settings.caption_max_chars,
                hook_override=overrides.get("hook"),
                image_prompt_override=overrides.get("image_prompt"),
                caption_override=overrides.get("caption"),
            )
            self.db.update_job(job_id, parsed_json=parsed.to_dict(), plan_json=plan.to_dict())
            self._emit(job_id, "METADATA_READY",
                       f"lang={plan.language} hook='{plan.cover_text}' style={plan.style_preset.value}")

            # ---------------- cover ----------------
            self.db.set_state(job_id, JobState.GENERATING_IMAGE, enforce=False)
            cover_path, cover_report = self._generate_cover(job_id, plan, paths)
            self.db.update_job(job_id, cover_path=str(cover_path))
            self._emit(job_id, "COVER_GENERATED",
                       f"{cover_path.name} [{cover_report['cover_source']}"
                       f" via {cover_report['provider']}]", **cover_report)

            # ---------------- video ----------------
            self.db.set_state(job_id, JobState.BUILDING_VIDEO, enforce=False)
            try:
                build = build_tiktok_video(video_path, cover_path, paths.final_video, self.settings)
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
            # prove the cover frame really is baked in
            frame = extract_frame(build.output_path, build.cover_timestamp_ms,
                                  paths.work_dir / "cover_frame_check.jpg", self.settings)
            self.db.update_job(job_id, final_video_path=str(build.output_path))
            self._emit(job_id, "VIDEO_BUILT", build.output_path.name,
                       cover_timestamp_ms=build.cover_timestamp_ms,
                       scaling=build.scaling_mode, warnings=vcheck.warnings,
                       cover_frame_check=str(frame))

            # ---------------- upload ----------------
            upload_result: Dict[str, Any]
            if job["dry_run"]:
                upload_result = {"skipped": True, "reason": "DRY_RUN=true",
                                 "would_upload": str(build.output_path),
                                 "mode": self.settings.effective_mode.value,
                                 "caption_preview": plan.caption[:200]}
                self._emit(job_id, "UPLOAD_SKIPPED", "DRY_RUN=true")
            else:
                self.db.set_state(job_id, JobState.UPLOADING, enforce=False)
                upload_result = self._upload(job_id, build.output_path, plan,
                                             build.cover_timestamp_ms)
                self.db.set_state(job_id, JobState.UPLOADED, enforce=False,
                                  publish_id=upload_result.get("publish_id"),
                                  upload_status=upload_result.get("status"))

            self.db.update_job(job_id, upload_result_json=upload_result)

            # ---------------- publish artifacts ----------------
            out_dir = self._write_output(job_id, paths, video_path, meta_path, cover_path,
                                         build, plan, upload_result, cover_report, vcheck.as_dict())
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
    def _require_generative(self) -> bool:
        """HIGH_QUALITY demands a real image model unless the user opted out."""
        if self.settings.quality_mode != QualityMode.HIGH_QUALITY:
            return False
        if self.settings.dry_run and self.settings.dry_run_allow_offline:
            return False          # explicit opt-in for rehearsals
        return True

    def _generate_cover(self, job_id: str, plan: ContentPlan, paths: JobPaths) -> tuple[Path, dict]:
        # ---- provider pre-flight before any expensive work -------------
        preflight = self.images.preflight(self.settings.cover_width, self.settings.cover_height)
        self.db.add_event(job_id, "IMAGE_PREFLIGHT",
                          message=", ".join(f"{r.provider}={r.tier.tier.value}"
                                            f"{'' if r.usable else '/unusable'}"
                                            for r in preflight),
                          data={"providers": [r.as_dict() for r in preflight]})
        require_generative = self._require_generative()
        if require_generative and not self.images.generative_providers(preflight):
            raise PipelineError(
                "No high-quality image provider is configured. Configure a supported image "
                "provider or enable offline fallback (ALLOW_OFFLINE_IMAGE_FALLBACK=true, or "
                "DRY_RUN_ALLOW_OFFLINE=true for dry runs).",
                stage="image", retryable=False)

        try:
            result, score, attempts = self.images.generate_background(
                plan.image_prompt, plan.negative_prompt,
                width=self.settings.cover_width, height=self.settings.cover_height,
                require_generative=require_generative)
        except NoGenerativeProviderError as exc:
            raise PipelineError(str(exc), stage="image", retryable=False)
        except ImageProviderError as exc:
            raise PipelineError(str(exc), stage="image", retryable=True)

        raw_bg = paths.work_dir / "background.png"
        raw_bg.parent.mkdir(parents=True, exist_ok=True)
        raw_bg.write_bytes(result.data)

        composer = CoverComposer(get_preset(plan.style_preset),
                                 font_bold=self.settings.font_bold,
                                 font_regular=self.settings.font_regular)
        composed = composer.compose(result.data, plan.hook_words,
                                    width=self.settings.cover_width,
                                    height=self.settings.cover_height)
        composed.save(paths.cover, fmt=self.settings.cover_format)

        check = validate_cover(paths.cover, expected_width=self.settings.cover_width,
                               expected_height=self.settings.cover_height,
                               layout=composed.layout)
        if not check.ok:
            raise PipelineError("generated cover failed validation: " + "; ".join(check.errors),
                                stage="image", retryable=True)
        cover_source = (CoverSource.AI_MODEL if result.generative
                        else CoverSource.OFFLINE_FALLBACK)
        provider_obj = next((p for p in self.images.chain if p.name == result.provider), None)
        tier = provider_obj.tier() if provider_obj else None
        self.db.update_job(job_id, cover_source=cover_source.value,
                           cover_provider=result.provider,
                           cover_model=(result.meta or {}).get("model")
                           or (provider_obj.model_name if provider_obj else None))
        if cover_source is CoverSource.OFFLINE_FALLBACK:
            self._emit(job_id, "OFFLINE_FALLBACK_USED",
                       "cover was rendered by the local procedural fallback, NOT by an AI model")

        report = {
            "provider": result.provider,
            "cover_source": cover_source.value,
            "generative": result.generative,
            "tier": tier.as_dict() if tier else None,
            "preflight": [r.as_dict() for r in preflight],
            "provider_meta": result.meta or {},
            "seed": result.seed,
            "score": score.as_dict(),
            "attempts": attempts,
            "layout": {"lines": composed.layout.lines, "font_size": composed.layout.font_size,
                       "contrast_ratio": round(composed.layout.contrast_ratio, 2)},
            "validation": check.as_dict(),
            "background_path": str(raw_bg),
        }
        return paths.cover, report

    # ------------------------------------------------------------------
    def _upload(self, job_id: str, video: Path, plan: ContentPlan,
                cover_ts_ms: int) -> Dict[str, Any]:
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
                    "video_cover_timestamp_ms": cover_ts_ms,
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
            "cover_timestamp_ms": cover_ts_ms,
            "is_aigc_flag_sent": bool(self.settings.content_is_aigc) and mode == "DIRECT_POST",
        })
        self._emit(job_id, "TIKTOK_DRAFT_READY" if outcome.mode == "UPLOAD" else "TIKTOK_POST_SUBMITTED",
                   f"publish_id={outcome.publish_id} status={outcome.status}")
        return result

    # ------------------------------------------------------------------
    def _write_output(self, job_id: str, paths: JobPaths, video: Path, meta: Path,
                      cover: Path, build, plan: ContentPlan, upload_result: dict,
                      cover_report: dict, video_check: dict) -> Path:
        out = paths.out_dir
        out.mkdir(parents=True, exist_ok=True)
        # copies only - the originals in input/ are never moved or modified
        shutil.copy2(meta, out / f"metadata{meta.suffix}")
        shutil.copy2(cover, out / "cover.png")
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
            "cover": cover_report,
            "video_build": build.as_dict(),
            "video_validation": video_check,
            "dry_run": job["dry_run"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "upload_result.json").write_text(
            json.dumps(upload_result, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "caption.txt").write_text(plan.caption, encoding="utf-8")
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
