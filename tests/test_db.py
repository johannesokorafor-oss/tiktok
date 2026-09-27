import pytest

from tta import db


def test_create_and_get(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    loaded = store.get(job.id)
    assert loaded and loaded.base_name == "clip"
    assert loaded.state == db.DISCOVERED


def test_state_transitions_and_events(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    for state in (db.VALIDATING, db.GENERATING_METADATA, db.GENERATING_IMAGE,
                  db.BUILDING_VIDEO, db.VALIDATING_VIDEO, db.UPLOADING,
                  db.UPLOADED, db.COMPLETED):
        store.set_state(job.id, state)
    assert store.get(job.id).state == db.COMPLETED
    events = store.events(job.id)
    assert [e["state"] for e in events][0] == db.DISCOVERED
    assert events[-1]["state"] == db.COMPLETED


def test_invalid_state_rejected(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    with pytest.raises(ValueError):
        store.set_state(job.id, "NOT_A_STATE")


def test_update_fields_and_meta(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    store.update_fields(job.id, video_sha256="abc", meta={"plan": {"hook": "hi"}})
    loaded = store.get(job.id)
    assert loaded.video_sha256 == "abc"
    assert loaded.meta["plan"]["hook"] == "hi"
    with pytest.raises(ValueError):
        store.update_fields(job.id, state="COMPLETED")  # not writable directly


def test_duplicate_detection_only_on_processed_states(store):
    a = store.create_job("a", "/a.mp4", "/a.txt")
    store.update_fields(a.id, video_sha256="deadbeef")
    # not yet completed -> no duplicate flag
    assert store.find_duplicate("deadbeef", exclude_job_id="zz") is None
    store.set_state(a.id, db.COMPLETED)
    dup = store.find_duplicate("deadbeef", exclude_job_id="zz")
    assert dup and dup.id == a.id
    # a job never matches itself
    assert store.find_duplicate("deadbeef", exclude_job_id=a.id) is None


def test_recover_interrupted_requeues(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    store.set_state(job.id, db.BUILDING_VIDEO)
    recovered = store.recover_interrupted()
    assert [j.id for j in recovered] == [job.id]
    assert store.get(job.id).state == db.RETRY_PENDING


def test_recover_ignores_terminal_states(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    store.set_state(job.id, db.COMPLETED)
    assert store.recover_interrupted() == []


def test_active_job_for_source(store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    assert store.active_job_for_source("/v.mp4").id == job.id
    store.set_state(job.id, db.COMPLETED)
    assert store.active_job_for_source("/v.mp4") is None


def test_list_jobs_filtering(store):
    a = store.create_job("a", "/a.mp4", "/a.txt")
    b = store.create_job("b", "/b.mp4", "/b.txt")
    store.set_state(b.id, db.FAILED, error="x")
    failed = store.list_jobs(states=(db.FAILED,))
    assert [j.id for j in failed] == [b.id]
    assert len(store.list_jobs()) == 2
