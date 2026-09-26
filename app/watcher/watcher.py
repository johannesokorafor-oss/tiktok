"""Folder watcher: pairs video + metadata files and drives the pipeline."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config import Settings
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.logging_setup import log_event
from app.storage.db import Database
from app.watcher.stability import StabilityTracker

log = logging.getLogger(__name__)


@dataclass
class Pair:
    base_name: str
    video: Path
    metadata: Optional[Path]

    @property
    def complete(self) -> bool:
        return self.metadata is not None


def discover_pairs(root: Path, settings: Settings) -> List[Pair]:
    """Find video files and their same-basename metadata sidecar."""
    if not root.is_dir():
        return []
    files = root.rglob("*") if settings.watch_recursive else root.glob("*")
    videos: List[Path] = []
    sidecars: Dict[Tuple[Path, str], Path] = {}
    vexts = {e.lower() for e in settings.video_extensions}
    mexts = {e.lower() for e in settings.metadata_extensions}
    for f in files:
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        if suffix in vexts:
            videos.append(f)
        elif suffix in mexts:
            sidecars[(f.parent, f.stem.lower())] = f
    pairs = []
    for v in sorted(videos):
        pairs.append(Pair(base_name=v.stem, video=v,
                          metadata=sidecars.get((v.parent, v.stem.lower()))))
    return pairs


class WatcherService:
    """Watchdog observer + a resilient polling reconciler.

    The poll loop is the source of truth (it also recovers missed events,
    retries and restarts); watchdog just wakes it up sooner.
    """

    def __init__(self, settings: Settings, db: Database, pipeline: Pipeline) -> None:
        self.settings = settings
        self.db = db
        self.pipeline = pipeline
        self.stability = StabilityTracker(settings.stability_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._observer: Optional[Observer] = None
        self.current_job: Optional[str] = None
        self.last_scan: Optional[float] = None
        self.started_at: Optional[float] = None
        self.errors: List[str] = []

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.settings.ensure_dirs()
        self.db.clear_stale_locks()
        recovered = self.db.recover_interrupted()
        if recovered:
            log_event(log, "JOBS_RECOVERED", f"{len(recovered)} interrupted job(s) requeued")
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()
        try:
            handler = _Handler(self._wake)
            self._observer = Observer()
            self._observer.schedule(handler, str(self.settings.input_dir),
                                    recursive=self.settings.watch_recursive)
            self._observer.start()
        except Exception as exc:  # noqa: BLE001 - polling still works without inotify
            log.warning("filesystem observer unavailable (%s); falling back to polling", exc)
            self._observer = None
        log_event(log, "WATCHER_STARTED", str(self.settings.input_dir))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log_event(log, "WATCHER_STOPPED")

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - never let the watcher die
                log.exception("watcher scan failed")
                self.errors = ([f"{time.strftime('%H:%M:%S')} {exc}"] + self.errors)[:20]
            self._wake.wait(self.settings.poll_interval_seconds)
            self._wake.clear()

    # ------------------------------------------------------------------
    def scan_once(self) -> List[str]:
        """One reconcile pass. Returns job ids processed in this pass."""
        self.last_scan = time.time()
        processed: List[str] = []

        # 1) due retries first
        for job in self.db.due_retries():
            processed.append(self._process_job(job["id"]))

        # 2) new / incomplete pairs
        for pair in discover_pairs(self.settings.input_dir, self.settings):
            existing = self.db.find_by_video_path(str(pair.video))
            if existing and existing["state"] not in {JobState.WAITING_FOR_PAIR.value,
                                                      JobState.DISCOVERED.value}:
                continue
            job_id = existing["id"] if existing else self.db.create_job(
                base_name=pair.base_name, video_path=str(pair.video),
                metadata_path=str(pair.metadata) if pair.metadata else None,
                dry_run=self.settings.dry_run)
            if not existing:
                log_event(log, "DISCOVERED", pair.video.name, job_id=job_id)

            if not pair.complete:
                if (self.db.get_job(job_id) or {}).get("state") != JobState.WAITING_FOR_PAIR.value:
                    self.db.set_state(job_id, JobState.WAITING_FOR_PAIR, enforce=False,
                                      message="metadata text file missing")
                continue

            self.db.update_job(job_id, metadata_path=str(pair.metadata))
            stable_v, why_v = self.stability.is_stable(pair.video)
            stable_m, why_m = self.stability.is_stable(pair.metadata)
            if not (stable_v and stable_m):
                self.db.add_event(job_id, "WAITING_FOR_STABILITY",
                                  message=f"video: {why_v}; metadata: {why_m}")
                continue

            processed.append(self._process_job(job_id))
        return [p for p in processed if p]

    # ------------------------------------------------------------------
    def _process_job(self, job_id: str) -> Optional[str]:
        owner = f"watcher-{threading.get_ident()}"
        if not self.db.try_lock(job_id, owner):
            return None
        self.current_job = job_id
        try:
            self.pipeline.run(job_id)
            return job_id
        finally:
            self.current_job = None
            self.db.unlock(job_id)

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "running": self.running,
            "watch_dir": str(self.settings.input_dir),
            "recursive": self.settings.watch_recursive,
            "stability_seconds": self.settings.stability_seconds,
            "current_job": self.current_job,
            "last_scan": self.last_scan,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "observer": bool(self._observer),
            "errors": self.errors[:5],
        }


class _Handler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event) -> None:
        self._wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._wake.set()
