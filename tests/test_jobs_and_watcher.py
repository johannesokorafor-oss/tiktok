import json
import time
from pathlib import Path

import pytest

from app.jobs.pipeline import Pipeline, sha256_file
from app.jobs.states import InvalidTransition, JobState, assert_transition, can_transition
from app.logging_setup import redact
from app.storage.db import Database
from app.watcher.stability import StabilityTracker
from app.watcher.watcher import WatcherService, discover_pairs


# ---------------------------------------------------------------- state machine
def test_state_machine_allows_expected_flow():
    flow = [JobState.DISCOVERED, JobState.VALIDATING, JobState.GENERATING_METADATA,
            JobState.BUILDING_VIDEO, JobState.VALIDATING_VIDEO,
            JobState.UPLOADING, JobState.UPLOADED, JobState.COMPLETED]
    for a, b in zip(flow, flow[1:]):
        assert can_transition(a, b)


def test_state_machine_rejects_illegal_jump():
    assert not can_transition(JobState.DISCOVERED, JobState.UPLOADED)
    with pytest.raises(InvalidTransition):
        assert_transition(JobState.DISCOVERED, JobState.UPLOADED)


def test_db_enforces_transitions(db):
    jid = db.create_job(base_name="a", video_path="/x/a.mp4", metadata_path="/x/a.txt")
    db.set_state(jid, JobState.VALIDATING)
    with pytest.raises(InvalidTransition):
        db.set_state(jid, JobState.UPLOADED)
    assert db.get_job(jid)["state"] == JobState.VALIDATING.value


def test_job_locking(db):
    jid = db.create_job(base_name="a", video_path="/x/a.mp4", metadata_path=None)
    assert db.try_lock(jid, "w1")
    assert not db.try_lock(jid, "w2")
    db.unlock(jid)
    assert db.try_lock(jid, "w2")


def test_events_and_retry_queue(db):
    jid = db.create_job(base_name="a", video_path="/x/a.mp4", metadata_path=None)
    db.set_state(jid, JobState.VALIDATING)
    db.set_state(jid, JobState.RETRY_PENDING, enforce=False)
    assert jid in [j["id"] for j in db.due_retries()]
    assert any(e["event"] == "RETRY_PENDING" for e in db.events(jid))


# ---------------------------------------------------------------- pairing
def test_pairing_and_extensions(settings):
    inp = settings.input_dir
    (inp / "a.mp4").write_bytes(b"x")
    (inp / "a.txt").write_text("t", encoding="utf-8")
    (inp / "b.mov").write_bytes(b"x")
    (inp / "c.txt").write_text("orphan", encoding="utf-8")
    (inp / "d.avi").write_bytes(b"x")          # unsupported extension
    pairs = {p.base_name: p for p in discover_pairs(inp, settings)}
    assert set(pairs) == {"a", "b"}
    assert pairs["a"].complete and not pairs["b"].complete


def test_pairing_is_case_insensitive_and_recursive(settings):
    sub = settings.input_dir / "sub"
    sub.mkdir()
    (sub / "Clip.MP4").write_bytes(b"x")
    (sub / "clip.TXT").write_text("t", encoding="utf-8")
    pairs = discover_pairs(settings.input_dir, settings)
    assert len(pairs) == 1 and pairs[0].complete


def test_non_recursive_mode(settings):
    (settings.input_dir / "sub").mkdir()
    (settings.input_dir / "sub" / "x.mp4").write_bytes(b"x")
    settings.watch_recursive = False
    assert discover_pairs(settings.input_dir, settings) == []


# ---------------------------------------------------------------- stability
def test_stability_requires_settled_size(tmp_path):
    clock = {"t": 0.0}
    tracker = StabilityTracker(period=3.0, clock=lambda: clock["t"])
    f = tmp_path / "f.bin"
    f.write_bytes(b"a" * 10)
    assert tracker.is_stable(f)[0] is False          # first sighting
    clock["t"] = 1.0
    assert tracker.is_stable(f)[0] is False          # not enough time
    f.write_bytes(b"a" * 20)                          # still growing
    clock["t"] = 2.0
    assert tracker.is_stable(f)[0] is False
    clock["t"] = 10.0
    tracker.is_stable(f)
    clock["t"] = 20.0
    ok, why = tracker.is_stable(f)
    assert ok, why


def test_stability_rejects_empty_and_missing(tmp_path):
    tracker = StabilityTracker(period=0.0)
    empty = tmp_path / "e.bin"
    empty.write_bytes(b"")
    assert tracker.is_stable(empty)[0] is False
    assert tracker.is_stable(tmp_path / "missing.bin")[0] is False


# ---------------------------------------------------------------- duplicates
def test_sha256_and_duplicate_detection(tmp_path, db):
    a = tmp_path / "a.bin"
    a.write_bytes(b"hello world")
    b = tmp_path / "b.bin"
    b.write_bytes(b"hello world")
    assert sha256_file(a) == sha256_file(b)
    jid = db.create_job(base_name="a", video_path=str(a), metadata_path=None)
    db.update_job(jid, video_sha256=sha256_file(a))
    dupes = db.find_by_hash(sha256_file(b))
    assert dupes and dupes[0]["id"] == jid


# ---------------------------------------------------------------- redaction
def test_secrets_are_redacted_in_logs():
    assert "act.supersecret" not in redact("token act.supersecretvalue123 used")
    assert "REDACTED" in redact('{"access_token": "abc123456"}')
    assert "REDACTED" in redact("Authorization: Bearer act.abc123")


# ---------------------------------------------------------------- end-to-end (offline)
def _write_pair(settings, sample_video: Path, metadata_text: str, name="mystisches-video"):
    video = settings.input_dir / f"{name}.mp4"
    video.write_bytes(sample_video.read_bytes())
    meta = settings.input_dir / f"{name}.txt"
    meta.write_text(metadata_text, encoding="utf-8")
    return video, meta


def test_end_to_end_dry_run(settings, db, sample_video, metadata_text):
    video, meta = _write_pair(settings, sample_video, metadata_text)
    original_bytes = video.read_bytes()
    pipeline = Pipeline(settings, db)
    watcher = WatcherService(settings, db, pipeline)

    done = watcher.scan_once()
    assert len(done) == 1
    job = db.get_job(done[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")

    # artifacts
    out = Path(job["output_dir"])
    for f in ("final_tiktok.mp4", "job.json", "upload_result.json", "caption.txt"):
        assert (out / f).is_file(), f
    result = json.loads((out / "upload_result.json").read_text(encoding="utf-8"))
    assert result["skipped"] is True and "DRY_RUN" in result["reason"]
    plan = json.loads((out / "job.json").read_text(encoding="utf-8"))["plan"]
    assert plan["caption"] and plan["language"] == "de"
    assert not (out / "cover.png").exists()

    # source untouched
    assert video.read_bytes() == original_bytes
    assert meta.read_text(encoding="utf-8") == metadata_text


def test_duplicate_protection_and_force(settings, db, sample_video, metadata_text):
    video, _ = _write_pair(settings, sample_video, metadata_text)
    pipeline = Pipeline(settings, db)
    first = pipeline.run(db.create_job(base_name="v", video_path=str(video),
                                       metadata_path=str(video.with_suffix(".txt")),
                                       dry_run=True))
    assert first["state"] == JobState.COMPLETED.value

    dup_id = db.create_job(base_name="v", video_path=str(video),
                           metadata_path=str(video.with_suffix(".txt")), dry_run=True)
    dup = pipeline.run(dup_id)
    assert dup["state"] == JobState.DUPLICATE.value

    forced = pipeline.run(dup_id, force=True)
    assert forced["state"] == JobState.COMPLETED.value


def test_missing_metadata_waits_for_pair(settings, db, sample_video):
    (settings.input_dir / "lonely.mp4").write_bytes(sample_video.read_bytes())
    watcher = WatcherService(settings, db, Pipeline(settings, db))
    watcher.scan_once()
    job = db.list_jobs()[0]
    assert job["state"] == JobState.WAITING_FOR_PAIR.value


def test_invalid_metadata_fails_job_and_keeps_files(settings, db, sample_video):
    video = settings.input_dir / "bad.mp4"
    video.write_bytes(sample_video.read_bytes())
    meta = settings.input_dir / "bad.txt"
    meta.write_text("   ", encoding="utf-8")
    watcher = WatcherService(settings, db, Pipeline(settings, db))
    watcher.scan_once()
    job = db.list_jobs()[0]
    assert job["state"] == JobState.FAILED.value
    assert "parse" in (job["error"] or "")
    assert video.is_file() and meta.is_file()
    assert list(settings.failed_dir.glob("*.json"))


def test_invalid_video_fails_cleanly(settings, db, metadata_text):
    video = settings.input_dir / "broken.mp4"
    video.write_bytes(b"definitely not a video" * 100)
    (settings.input_dir / "broken.txt").write_text(metadata_text, encoding="utf-8")
    watcher = WatcherService(settings, db, Pipeline(settings, db))
    watcher.scan_once()
    job = db.list_jobs()[0]
    assert job["state"] == JobState.FAILED.value
    assert "validate" in (job["error"] or "")


def test_mock_upload_path_end_to_end(settings, db, sample_video, metadata_text):
    settings.dry_run = False
    settings.tiktok_mock = True
    video, _ = _write_pair(settings, sample_video, metadata_text, name="mockjob")
    pipeline = Pipeline(settings, db)
    job = pipeline.run(db.create_job(base_name="mockjob", video_path=str(video),
                                     metadata_path=str(video.with_suffix(".txt")),
                                     dry_run=False))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert job["publish_id"].startswith("mock.")
    result = job["upload_result"]
    assert result["mode"] == "UPLOAD"
    assert result["status"] == "SEND_TO_USER_INBOX"
    assert result["is_aigc_flag_sent"] is False


def test_caption_override_is_applied(settings, db, sample_video, metadata_text):
    video, _ = _write_pair(settings, sample_video, metadata_text, name="override")
    pipeline = Pipeline(settings, db)
    jid = db.create_job(base_name="override", video_path=str(video),
                        metadata_path=str(video.with_suffix(".txt")), dry_run=True)
    db.update_job(jid, overrides_json={"caption": "Handgeschriebene Caption",
                                       "title": "Mein Titel"})
    job = pipeline.run(jid, force=True)
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert job["plan"]["caption"] == "Handgeschriebene Caption"
    assert job["plan"]["tiktok_title"] == "Mein Titel"


def test_no_image_events_are_emitted(settings, db, sample_video, metadata_text):
    video, _ = _write_pair(settings, sample_video, metadata_text, name="noimage")
    job = Pipeline(settings, db).run(db.create_job(
        base_name="noimage", video_path=str(video),
        metadata_path=str(video.with_suffix(".txt")), dry_run=True))
    events = {e["event"] for e in db.events(job["id"])}
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    assert not events & {"GENERATING_IMAGE", "IMAGE_PREFLIGHT", "OFFLINE_FALLBACK_USED",
                         "COVER_GENERATED"}
    assert {"VALIDATING", "GENERATING_METADATA", "BUILDING_VIDEO", "VALIDATING_VIDEO",
            "COMPLETED"} <= events
    assert job["cover_path"] is None and job["cover_source"] is None
