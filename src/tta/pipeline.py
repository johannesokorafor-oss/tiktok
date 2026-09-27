"""End-to-end job pipeline.

Executes a job through all states:

VALIDATING -> GENERATING_METADATA -> GENERATING_IMAGE -> BUILDING_VIDEO
-> VALIDATING_VIDEO -> UPLOADING -> UPLOADED -> COMPLETED

* DRY_RUN=true executes everything except the TikTok upload.
* Failures mark the job FAILED or RETRY_PENDING (never crash the watcher).
* Original input files are only read/moved, never modified.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from . import db, textparse, understanding, typography, video
from .config import Config
from .hashing import sha256_file
from .providers import ImageRequest, ProviderRegistry, build_registry

log = logging.getLogger("tta.pipeline")


class PipelineError(RuntimeError):
    """Recoverable pipeline failure."""


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_\-]", "_", name)
    return slug[:60] or "job"


class Pipeline:
    def __init__(self, config: Config, store: db.JobStore,
                 registry: ProviderRegistry | None = None,
                 uploader=None):
        self.config = config
        self.store = store
        self.registry = registry or build_registry(config)
        # uploader: callable(job, final_path, plan, timestamp_ms) -> dict
        self.uploader = uploader
        # guard against the same job being executed concurrently
        # (e.g. dashboard retry thread + watcher retry loop)
        self._active_lock = threading.Lock()
        self._active: set[str] = set()

    # ------------------------------------------------------------------
    def run_job(self, job: db.Job, force: bool = False) -> db.Job:
        """Run the whole pipeline for one job. Never raises for job errors."""
        with self._active_lock:
            if job.id in self._active:
                log.info("job %s is already running - ignoring duplicate start",
                         job.id)
                return self.store.get(job.id) or job
            self._active.add(job.id)
        try:
            self._execute(job, force=force)
        except PipelineError as exc:
            self._fail(job, str(exc))
        except Exception as exc:  # defensive: unexpected bug should not kill watcher
            log.exception("unexpected error in job %s", job.id)
            self._fail(job, f"unexpected error: {exc}")
        finally:
            with self._active_lock:
                self._active.discard(job.id)
        return self.store.get(job.id) or job

    def _fail(self, job: db.Job, message: str) -> None:
        current = self.store.get(job.id)
        retries = current.retries if current else 0
        if retries < self.config.max_retries:
            self.store.update_fields(job.id, retries=retries + 1, error=message)
            self.store.set_state(job.id, db.RETRY_PENDING,
                                 f"attempt {retries + 1} failed: {message}")
            log.warning("job %s -> RETRY_PENDING (%s)", job.id, message)
        else:
            self.store.set_state(job.id, db.FAILED, error=message)
            log.error("job %s -> FAILED (%s)", job.id, message)
            self._move_sources(job, self.config.failed_dir)

    # ------------------------------------------------------------------
    def _execute(self, job: db.Job, force: bool = False) -> None:
        config = self.config
        video_src = Path(job.video_path)
        text_src = Path(job.text_path)

        # ---------------------------------------------------- VALIDATING
        self.store.set_state(job.id, db.VALIDATING, "hashing and duplicate check")
        if not video_src.is_file():
            raise PipelineError(f"video file disappeared: {video_src}")
        if not text_src.is_file():
            raise PipelineError(f"text file disappeared: {text_src}")

        sha = sha256_file(video_src)
        self.store.update_fields(job.id, video_sha256=sha)
        duplicate = self.store.find_duplicate(sha, exclude_job_id=job.id)
        if duplicate and not force:
            self.store.set_state(
                job.id, db.DUPLICATE,
                f"same content as job {duplicate.id} ({duplicate.base_name}); "
                "use force-reprocess to override",
            )
            self._move_sources(job, self.config.archive_dir)
            return

        job_dir = config.output_dir / f"{_slug(job.base_name)}-{job.id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        self.store.update_fields(job.id, output_dir=str(job_dir))

        # copy sources into the job dir (originals stay untouched)
        source_copy = job_dir / ("source" + video_src.suffix.lower())
        if not source_copy.exists():
            shutil.copy2(video_src, source_copy)
        shutil.copy2(text_src, job_dir / "metadata.txt")

        # -------------------------------------------- GENERATING_METADATA
        self.store.set_state(job.id, db.GENERATING_METADATA, "parsing text")
        meta = textparse.parse_file(text_src)
        if not (meta.title or meta.description or meta.image_prompt):
            raise PipelineError("metadata file is empty or unparseable")
        plan = understanding.understand(meta)
        meta_payload = {"parsed": meta.as_dict(), "plan": plan.as_dict()}
        self.store.update_fields(job.id, meta=meta_payload)
        log.info("job %s: hook=%r cover=%r style=%s lang=%s", job.id,
                 plan.hook, plan.cover_text, plan.style, plan.language)

        # ---------------------------------------------- GENERATING_IMAGE
        self.store.set_state(job.id, db.GENERATING_IMAGE,
                             f"provider={self.registry.preference}")
        background = job_dir / "background.png"
        request = ImageRequest(
            prompt=plan.image_prompt,
            negative_prompt=plan.negative_prompt,
            width=config.cover_width,
            height=config.cover_height,
            style=plan.style,
        )
        try:
            gen = self.registry.generate(request, background)
        except Exception as exc:
            raise PipelineError(f"image generation failed: {exc}") from exc
        meta_payload["image_provider"] = gen.provider
        meta_payload["image_generation_mode"] = gen.mode
        if gen.fallback_reason:
            meta_payload["image_fallback_reason"] = gen.fallback_reason
            log.warning("job %s: '%s' failed, cover is a PROCEDURAL_FALLBACK "
                        "by '%s'", job.id, gen.requested_provider, gen.provider)

        cover = job_dir / "cover.png"
        try:
            typography.render_cover_text(background, plan.cover_text, cover)
        except typography.TypographyError as exc:
            raise PipelineError(f"typography failed: {exc}") from exc
        # convenience copy for the covers/ gallery + dashboard
        shutil.copy2(cover, config.covers_dir / f"{job.id}.png")
        self.store.update_fields(job.id, cover_path=str(cover), meta=meta_payload)

        # ------------------------------------------------ BUILDING_VIDEO
        self.store.set_state(job.id, db.BUILDING_VIDEO, "normalizing + embedding cover")
        normalized = job_dir / "normalized.mp4"
        final = job_dir / "final_tiktok.mp4"
        try:
            video.normalize_video(source_copy, normalized,
                                  config.ffmpeg_path, config.ffprobe_path)
            final_info, ts_ms = video.insert_cover(
                normalized, cover, final,
                cover_duration_ms=config.cover_duration_ms,
                ffmpeg=config.ffmpeg_path, ffprobe=config.ffprobe_path,
            )
        except video.VideoError as exc:
            raise PipelineError(f"video processing failed: {exc}") from exc
        finally:
            if normalized.exists():
                normalized.unlink()
        meta_payload["cover_timestamp_ms"] = ts_ms
        self.store.update_fields(job.id, final_path=str(final), meta=meta_payload)

        # ---------------------------------------------- VALIDATING_VIDEO
        self.store.set_state(job.id, db.VALIDATING_VIDEO, "ffprobe validation")
        report = video.validate_final(final, cover_timestamp_ms=ts_ms,
                                      ffprobe=config.ffprobe_path)
        meta_payload["validation"] = report.as_dict()
        self.store.update_fields(job.id, meta=meta_payload)
        if not report.ok:
            raise PipelineError("final video failed validation: "
                                + "; ".join(report.issues))

        # --------------------------------------------------------- job.json
        self._write_job_json(job.id, job_dir)

        # --------------------------------------------------- UPLOADING
        upload_result: dict
        if config.dry_run:
            upload_result = {
                "skipped": True,
                "reason": "DRY_RUN=true - no TikTok call was made",
                "would_use_mode": config.upload_mode,
                "video_cover_timestamp_ms": ts_ms,
            }
            self.store.update_fields(job.id, upload_status="skipped_dry_run")
            log.info("job %s: dry run - skipping TikTok upload", job.id)
        elif self.uploader is None:
            upload_result = {
                "skipped": True,
                "reason": "no uploader configured (TikTok credentials missing)",
            }
            self.store.update_fields(job.id, upload_status="skipped_no_credentials")
        else:
            self.store.set_state(job.id, db.UPLOADING,
                                 f"mode={config.upload_mode}")
            try:
                upload_result = self.uploader(job, final, plan, ts_ms)
            except Exception as exc:
                raise PipelineError(f"TikTok upload failed: {exc}") from exc
            self.store.update_fields(
                job.id,
                publish_id=str(upload_result.get("publish_id", "")),
                upload_status=str(upload_result.get("status", "uploaded")),
            )
            self.store.set_state(job.id, db.UPLOADED,
                                 f"publish_id={upload_result.get('publish_id', '')}")

        (job_dir / "upload_result.json").write_text(
            json.dumps(upload_result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # --------------------------------------------------- COMPLETED
        self.store.set_state(job.id, db.COMPLETED, "done")
        self._write_job_json(job.id, job_dir)
        self._move_sources(job, self.config.archive_dir)
        log.info("job %s COMPLETED (%s)", job.id, job_dir)

    # ------------------------------------------------------------------
    def regenerate_cover(self, job_id: str) -> Path:
        """Re-run image generation + typography for an existing job."""
        job = self.store.get(job_id)
        if not job:
            raise PipelineError(f"job not found: {job_id}")
        plan_data = (job.meta or {}).get("plan") or {}
        if not plan_data:
            raise PipelineError("job has no stored content plan yet")
        job_dir = Path(job.output_dir) if job.output_dir else None
        if not job_dir or not job_dir.is_dir():
            raise PipelineError("job output directory missing")
        background = job_dir / "background.png"
        request = ImageRequest(
            prompt=plan_data.get("image_prompt", ""),
            negative_prompt=plan_data.get("negative_prompt", ""),
            width=self.config.cover_width,
            height=self.config.cover_height,
            style=plan_data.get("style", "CLEAN_MODERN"),
            seed=int(time.time()) % (2 ** 31),
        )
        gen = self.registry.generate(request, background)
        cover = job_dir / "cover.png"
        typography.render_cover_text(background, plan_data.get("cover_text", ""), cover)
        shutil.copy2(cover, self.config.covers_dir / f"{job.id}.png")
        meta = dict(job.meta or {})
        meta["image_provider"] = gen.provider
        meta["image_generation_mode"] = gen.mode
        if gen.fallback_reason:
            meta["image_fallback_reason"] = gen.fallback_reason
        self.store.update_fields(job.id, cover_path=str(cover), meta=meta)
        return cover

    def _write_job_json(self, job_id: str, job_dir: Path) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        payload = job.as_dict()
        payload["events"] = self.store.events(job_id)
        (job_dir / "job.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _move_sources(self, job: db.Job, target_dir: Path) -> None:
        """Move original files (content untouched) out of the active area."""
        target_dir.mkdir(parents=True, exist_ok=True)
        current = self.store.get(job.id) or job
        updates: dict[str, str] = {}
        for field_name, path_str in (("video_path", current.video_path),
                                     ("text_path", current.text_path)):
            path = Path(path_str)
            if not path.is_file():
                continue
            dest = target_dir / path.name
            counter = 1
            while dest.exists():
                dest = target_dir / f"{path.stem}_{counter}{path.suffix}"
                counter += 1
            try:
                shutil.move(str(path), str(dest))
                updates[field_name] = str(dest)
            except OSError as exc:
                log.warning("could not move %s to %s: %s", path, target_dir, exc)
        if updates:
            self.store.update_fields(job.id, **updates)
