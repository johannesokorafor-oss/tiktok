"""Folder watcher.

Polling-based scanner (robust on Windows network shares and cross-platform):

* pairs video (.mp4/.mov/.mkv/.webm) with text (.txt/.md) by base filename
* waits until both files are size-stable and openable (configurable delay)
* moves the stable pair into processing/ and hands it to the pipeline
* duplicate protection at path level (db) and content level (sha-256)
* requeues RETRY_PENDING jobs, recovers interrupted jobs after a restart
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import TEXT_EXTENSIONS, VIDEO_EXTENSIONS, Config
from .pipeline import Pipeline

log = logging.getLogger("tta.watcher")


@dataclass
class _Candidate:
    video: Path | None = None
    text: Path | None = None
    first_seen: float = field(default_factory=time.monotonic)
    sizes: dict = field(default_factory=dict)          # path -> (size, since)
    announced_waiting: bool = False


class FileStabilityTracker:
    """Tracks size stability of files across scans."""

    def __init__(self, stabilize_seconds: float):
        self.stabilize_seconds = stabilize_seconds
        self._seen: dict[str, tuple[int, float]] = {}

    def is_stable(self, path: Path, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        try:
            size = path.stat().st_size
        except OSError:
            self._seen.pop(str(path), None)
            return False
        key = str(path)
        prev = self._seen.get(key)
        if prev is None or prev[0] != size:
            self._seen[key] = (size, now)
            return False
        if size == 0:
            return False
        if now - prev[1] < self.stabilize_seconds:
            return False
        return _is_readable(path)

    def forget(self, path: Path) -> None:
        self._seen.pop(str(path), None)


def _is_readable(path: Path) -> bool:
    """The file must be openable and not exclusively locked by a writer."""
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


class Watcher:
    def __init__(self, config: Config, store: db.JobStore, pipeline: Pipeline):
        self.config = config
        self.store = store
        self.pipeline = pipeline
        self.tracker = FileStabilityTracker(config.stabilize_seconds)
        self._stop = threading.Event()
        self._candidates: dict[str, _Candidate] = {}
        self.started_at: float = 0.0
        self.last_scan: float = 0.0
        self.current_job_id: str = ""

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self.started_at > 0 and not self._stop.is_set()

    def status(self) -> dict:
        return {
            "running": self.running,
            "watch_dir": str(self.config.input_dir),
            "started_at": self.started_at,
            "last_scan": self.last_scan,
            "current_job": self.current_job_id,
            "pending_pairs": {
                base: {
                    "video": str(c.video) if c.video else None,
                    "text": str(c.text) if c.text else None,
                }
                for base, c in self._candidates.items()
            },
            "dry_run": self.config.dry_run,
        }

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        self.config.ensure_dirs()
        self.started_at = time.time()
        recovered = self.store.recover_interrupted()
        if recovered:
            log.info("recovered %d interrupted job(s) after restart", len(recovered))
        log.info("watching %s (dry_run=%s)", self.config.input_dir,
                 self.config.dry_run)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                log.exception("scan error (watcher keeps running)")
            self._stop.wait(self.config.scan_interval)
        log.info("watcher stopped")

    # ------------------------------------------------------------------
    def scan_once(self) -> None:
        self.last_scan = time.time()
        self._collect_candidates()
        self._process_ready()
        self._expire_unpaired()
        self._run_retries()

    def _collect_candidates(self) -> None:
        input_dir = self.config.input_dir
        if not input_dir.is_dir():
            return
        for path in sorted(input_dir.iterdir()):
            if not path.is_file() or path.name.startswith((".", "~")):
                continue
            suffix = path.suffix.lower()
            base = path.stem
            if suffix in VIDEO_EXTENSIONS:
                cand = self._candidates.setdefault(base, _Candidate())
                cand.video = path
            elif suffix in TEXT_EXTENSIONS:
                cand = self._candidates.setdefault(base, _Candidate())
                cand.text = path

    def _process_ready(self) -> None:
        for base in list(self._candidates.keys()):
            cand = self._candidates[base]
            if self._stop.is_set():
                return
            if cand.video and not cand.video.is_file():
                cand.video = None
            if cand.text and not cand.text.is_file():
                cand.text = None
            if not cand.video and not cand.text:
                del self._candidates[base]
                continue
            if not (cand.video and cand.text):
                if not cand.announced_waiting and cand.video:
                    log.info("found %s - waiting for matching text file", cand.video.name)
                    cand.announced_waiting = True
                continue
            if not (self.tracker.is_stable(cand.video)
                    and self.tracker.is_stable(cand.text)):
                continue

            # already tracked? (duplicate path protection)
            if self.store.active_job_for_source(str(cand.video)):
                continue

            del self._candidates[base]
            self.tracker.forget(cand.video)
            self.tracker.forget(cand.text)
            self._launch(base, cand.video, cand.text)

    def _launch(self, base: str, video_path: Path, text_path: Path,
                force: bool = False) -> None:
        # move the stable pair to processing/ (content untouched)
        processing = self.config.processing_dir
        processing.mkdir(parents=True, exist_ok=True)
        moved_video = _move_unique(video_path, processing)
        moved_text = _move_unique(text_path, processing)

        job = self.store.create_job(base, str(moved_video), str(moved_text),
                                    state=db.DISCOVERED)
        log.info("job %s created for pair '%s'", job.id, base)
        self.current_job_id = job.id
        try:
            self.pipeline.run_job(job, force=force)
        finally:
            self.current_job_id = ""

    def _expire_unpaired(self) -> None:
        timeout = self.config.pair_timeout
        now = time.monotonic()
        for base in list(self._candidates.keys()):
            cand = self._candidates[base]
            if cand.video and cand.text:
                continue
            if now - cand.first_seen < timeout:
                continue
            present = cand.video or cand.text
            if present is None:
                del self._candidates[base]
                continue
            log.warning("pair timeout for '%s' - moving %s to failed/",
                        base, present.name)
            job = self.store.create_job(base, str(cand.video or ""),
                                        str(cand.text or ""),
                                        state=db.WAITING_FOR_PAIR)
            self.store.set_state(
                job.id, db.FAILED,
                error=f"no matching {'text' if cand.video else 'video'} file "
                      f"appeared within {timeout:.0f}s",
            )
            _move_unique(present, self.config.failed_dir)
            del self._candidates[base]

    def _run_retries(self) -> None:
        for job in self.store.pending_retries():
            if self._stop.is_set():
                return
            if not Path(job.video_path).is_file() or not Path(job.text_path).is_file():
                self.store.set_state(job.id, db.FAILED,
                                     error="source files no longer available for retry")
                continue
            log.info("retrying job %s (attempt %d)", job.id, job.retries)
            self.current_job_id = job.id
            try:
                self.pipeline.run_job(job)
            finally:
                self.current_job_id = ""


def _move_unique(path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    counter = 1
    while dest.exists():
        dest = target_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(dest))
    return dest
