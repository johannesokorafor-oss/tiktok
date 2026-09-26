"""SQLite persistence for jobs, events and upload results."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.jobs.states import INTERRUPTED, JobState, assert_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL,
    base_name TEXT NOT NULL,
    video_path TEXT NOT NULL,
    metadata_path TEXT,
    video_sha256 TEXT,
    metadata_sha256 TEXT,
    plan_json TEXT,
    parsed_json TEXT,
    cover_path TEXT,
    final_video_path TEXT,
    output_dir TEXT,
    publish_id TEXT,
    upload_status TEXT,
    upload_result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    locked_by TEXT,
    locked_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    overrides_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(video_sha256);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    state TEXT,
    message TEXT,
    data_json TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def create_job(self, *, base_name: str, video_path: str, metadata_path: Optional[str],
                   state: JobState = JobState.DISCOVERED, dry_run: bool = True,
                   job_id: Optional[str] = None) -> str:
        jid = job_id or uuid.uuid4().hex[:12]
        now = utcnow()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs (id, created_at, updated_at, state, base_name, video_path,"
                " metadata_path, dry_run) VALUES (?,?,?,?,?,?,?,?)",
                (jid, now, now, state.value, base_name, video_path, metadata_path, int(dry_run)))
        self.add_event(jid, "JOB_CREATED", state=state.value, message=base_name)
        return jid

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def find_by_video_path(self, video_path: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE video_path=? ORDER BY created_at DESC LIMIT 1",
                (video_path,)).fetchone()
        return self._row_to_job(row) if row else None

    def find_by_hash(self, sha: str, exclude_job: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM jobs WHERE video_sha256=?"
        params: List[Any] = [sha]
        if exclude_job:
            q += " AND id<>?"
            params.append(exclude_job)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def list_jobs(self, limit: int = 50, states: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM jobs"
        params: List[Any] = []
        if states:
            states = list(states)
            q += " WHERE state IN (" + ",".join("?" * len(states)) + ")"
            params.extend(states)
        q += " ORDER BY datetime(created_at) DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("plan_json", "parsed_json", "upload_result_json", "overrides_json"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = utcnow()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def set_state(self, job_id: str, state: JobState, *, message: str = "",
                  enforce: bool = True, **extra: Any) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        if enforce:
            assert_transition(JobState(job["state"]), state)
        self.update_job(job_id, state=state.value, **extra)
        self.add_event(job_id, state.value, state=state.value, message=message)

    def add_event(self, job_id: str, event: str, *, state: Optional[str] = None,
                  message: str = "", data: Optional[dict] = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO job_events (job_id, ts, event, state, message, data_json)"
                      " VALUES (?,?,?,?,?,?)",
                      (job_id, utcnow(), event, state, message,
                       json.dumps(data, ensure_ascii=False, default=str) if data else None))

    def events(self, job_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def try_lock(self, job_id: str, owner: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE jobs SET locked_by=?, locked_at=? WHERE id=? AND (locked_by IS NULL OR locked_by=?)",
                (owner, utcnow(), job_id, owner))
            return cur.rowcount > 0

    def unlock(self, job_id: str) -> None:
        self.update_job(job_id, locked_by=None, locked_at=None)

    def clear_stale_locks(self) -> int:
        with self.tx() as c:
            cur = c.execute("UPDATE jobs SET locked_by=NULL, locked_at=NULL WHERE locked_by IS NOT NULL")
            return cur.rowcount

    def due_retries(self) -> List[Dict[str, Any]]:
        now = utcnow()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state=? AND (next_retry_at IS NULL OR next_retry_at<=?)",
                (JobState.RETRY_PENDING.value, now)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def recover_interrupted(self) -> List[str]:
        """Requeue jobs that were mid-flight when the process/machine stopped.

        SQLite holds the authoritative state, so a crash, reboot or Ctrl+C
        never loses a job: anything left in a working state is moved back to
        RETRY_PENDING and picked up by the next watcher pass.
        """
        states = [s.value for s in INTERRUPTED]
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, state FROM jobs WHERE state IN (" + ",".join("?" * len(states)) + ")",
                states).fetchall()
        recovered = []
        for row in rows:
            self.update_job(row["id"], locked_by=None, locked_at=None, next_retry_at=None)
            self.set_state(row["id"], JobState.RETRY_PENDING, enforce=False,
                           message=f"recovered after restart (was {row['state']})")
            self.add_event(row["id"], "RECOVERED_AFTER_RESTART", state=row["state"])
            recovered.append(row["id"])
        return recovered

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
        job = dict(row)
        job["plan"] = _loads(job.get("plan_json"))
        job["parsed"] = _loads(job.get("parsed_json"))
        job["upload_result"] = _loads(job.get("upload_result_json"))
        job["overrides"] = _loads(job.get("overrides_json")) or {}
        job["dry_run"] = bool(job.get("dry_run"))
        return job
