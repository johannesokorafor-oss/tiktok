"""SQLite-backed job persistence and state machine.

Jobs survive restarts: on startup any job stuck in a transient state is
moved to RETRY_PENDING so it is picked up again instead of being lost.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Required states
DISCOVERED = "DISCOVERED"
WAITING_FOR_PAIR = "WAITING_FOR_PAIR"
VALIDATING = "VALIDATING"
GENERATING_METADATA = "GENERATING_METADATA"
GENERATING_IMAGE = "GENERATING_IMAGE"
BUILDING_VIDEO = "BUILDING_VIDEO"
VALIDATING_VIDEO = "VALIDATING_VIDEO"
UPLOADING = "UPLOADING"
UPLOADED = "UPLOADED"
FAILED = "FAILED"
RETRY_PENDING = "RETRY_PENDING"
COMPLETED = "COMPLETED"
DUPLICATE = "DUPLICATE"

ALL_STATES = (
    DISCOVERED, WAITING_FOR_PAIR, VALIDATING, GENERATING_METADATA,
    GENERATING_IMAGE, BUILDING_VIDEO, VALIDATING_VIDEO, UPLOADING,
    UPLOADED, FAILED, RETRY_PENDING, COMPLETED, DUPLICATE,
)

# states a crashed/restarted process may leave behind -> requeue
TRANSIENT_STATES = (
    VALIDATING, GENERATING_METADATA, GENERATING_IMAGE,
    BUILDING_VIDEO, VALIDATING_VIDEO, UPLOADING,
)

TERMINAL_STATES = (COMPLETED, FAILED, DUPLICATE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    base_name     TEXT NOT NULL,
    video_path    TEXT NOT NULL,
    text_path     TEXT NOT NULL,
    video_sha256  TEXT DEFAULT '',
    state         TEXT NOT NULL,
    error         TEXT DEFAULT '',
    retries       INTEGER DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    output_dir    TEXT DEFAULT '',
    cover_path    TEXT DEFAULT '',
    final_path    TEXT DEFAULT '',
    publish_id    TEXT DEFAULT '',
    upload_status TEXT DEFAULT '',
    meta_json     TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    ts      REAL NOT NULL,
    state   TEXT NOT NULL,
    message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_sha ON jobs(video_sha256);
"""


@dataclass
class Job:
    id: str
    base_name: str
    video_path: str
    text_path: str
    state: str
    video_sha256: str = ""
    error: str = ""
    retries: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    output_dir: str = ""
    cover_path: str = ""
    final_path: str = ""
    publish_id: str = ""
    upload_status: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        return data


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def create_job(self, base_name: str, video_path: str, text_path: str,
                   state: str = DISCOVERED) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            base_name=base_name,
            video_path=str(video_path),
            text_path=str(text_path),
            state=state,
            created_at=time.time(),
            updated_at=time.time(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, base_name, video_path, text_path, state,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (job.id, job.base_name, job.video_path, job.text_path,
                 job.state, job.created_at, job.updated_at),
            )
            self._conn.execute(
                "INSERT INTO events (job_id, ts, state, message) VALUES (?,?,?,?)",
                (job.id, job.created_at, state, "job created"),
            )
            self._conn.commit()
        return job

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], base_name=row["base_name"],
            video_path=row["video_path"], text_path=row["text_path"],
            state=row["state"], video_sha256=row["video_sha256"],
            error=row["error"], retries=row["retries"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            output_dir=row["output_dir"], cover_path=row["cover_path"],
            final_path=row["final_path"], publish_id=row["publish_id"],
            upload_status=row["upload_status"],
            meta=json.loads(row["meta_json"] or "{}"),
        )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 50, states: tuple[str, ...] | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: tuple = ()
        if states:
            query += f" WHERE state IN ({','.join('?' * len(states))})"
            params = states
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, params + (limit,)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def set_state(self, job_id: str, state: str, message: str = "",
                  error: str | None = None) -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown state: {state}")
        now = time.time()
        with self._lock:
            if error is not None:
                self._conn.execute(
                    "UPDATE jobs SET state=?, updated_at=?, error=? WHERE id=?",
                    (state, now, error, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET state=?, updated_at=? WHERE id=?",
                    (state, now, job_id),
                )
            self._conn.execute(
                "INSERT INTO events (job_id, ts, state, message) VALUES (?,?,?,?)",
                (job_id, now, state, message or error or ""),
            )
            self._conn.commit()

    def update_fields(self, job_id: str, **fields) -> None:
        allowed = {
            "video_sha256", "error", "retries", "output_dir", "cover_path",
            "final_path", "publish_id", "upload_status", "video_path",
            "text_path",
        }
        meta = fields.pop("meta", None)
        cols, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update field: {key}")
            cols.append(f"{key}=?")
            params.append(value)
        if meta is not None:
            cols.append("meta_json=?")
            params.append(json.dumps(meta, ensure_ascii=False))
        if not cols:
            return
        cols.append("updated_at=?")
        params.append(time.time())
        params.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", params
            )
            self._conn.commit()

    def events(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, state, message FROM events WHERE job_id=? ORDER BY ts",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def find_duplicate(self, sha256: str, exclude_job_id: str = "") -> Job | None:
        """A job with the same content hash that was already processed."""
        if not sha256:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE video_sha256=? AND id != ? AND "
                "state IN (?,?,?) ORDER BY created_at DESC LIMIT 1",
                (sha256, exclude_job_id, COMPLETED, UPLOADED, UPLOADING),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def active_job_for_source(self, video_path: str) -> Job | None:
        """Any non-terminal job already tracking this source path."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE video_path=? AND state NOT IN (?,?,?) "
                "ORDER BY created_at DESC LIMIT 1",
                (str(video_path), COMPLETED, FAILED, DUPLICATE),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def recover_interrupted(self) -> list[Job]:
        """Requeue jobs that were mid-flight when the process died."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE state IN "
                f"({','.join('?' * len(TRANSIENT_STATES))})",
                TRANSIENT_STATES,
            ).fetchall()
        recovered = []
        for row in rows:
            job = self._row_to_job(row)
            self.set_state(job.id, RETRY_PENDING,
                           "recovered after restart (was " + job.state + ")")
            recovered.append(job)
        return recovered

    def pending_retries(self) -> list[Job]:
        return self.list_jobs(limit=100, states=(RETRY_PENDING,))
