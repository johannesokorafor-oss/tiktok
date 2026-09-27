import time
from pathlib import Path

from tta import db
from tta.watcher import FileStabilityTracker, Watcher, _is_readable


class StubPipeline:
    """Records jobs instead of running the real pipeline."""

    def __init__(self, store):
        self.store = store
        self.ran = []

    def run_job(self, job, force=False):
        self.ran.append((job.id, force))
        self.store.set_state(job.id, db.COMPLETED)
        return self.store.get(job.id)


def _watcher(config, store):
    return Watcher(config, store, StubPipeline(store))


# ---------------------------------------------------------- stability
def test_stability_requires_constant_size(tmp_path):
    tracker = FileStabilityTracker(stabilize_seconds=10)
    f = tmp_path / "grow.bin"
    f.write_bytes(b"a" * 10)
    t0 = 1000.0
    assert not tracker.is_stable(f, now=t0)          # first sighting
    f.write_bytes(b"a" * 20)                          # still growing
    assert not tracker.is_stable(f, now=t0 + 5)       # size changed -> reset
    assert not tracker.is_stable(f, now=t0 + 9)       # not enough quiet time
    assert tracker.is_stable(f, now=t0 + 16)          # stable long enough


def test_stability_rejects_empty_file(tmp_path):
    tracker = FileStabilityTracker(stabilize_seconds=0)
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    tracker.is_stable(f, now=1.0)
    assert not tracker.is_stable(f, now=100.0)


def test_stability_missing_file(tmp_path):
    tracker = FileStabilityTracker(stabilize_seconds=0)
    assert not tracker.is_stable(tmp_path / "nope.bin", now=1.0)


def test_is_readable(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    assert _is_readable(f)
    assert not _is_readable(tmp_path / "gone.bin")


# ---------------------------------------------------------- pairing
def test_pair_detected_and_processed(config, store, input_pair):
    video, text = input_pair
    watcher = _watcher(config, store)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not watcher.pipeline.ran:
        watcher.scan_once()
        time.sleep(0.1)
    assert len(watcher.pipeline.ran) == 1
    # pair moved out of input into processing/
    assert not video.exists() and not text.exists()
    job = store.list_jobs()[0]
    assert Path(job.video_path).parent == config.processing_dir
    assert job.base_name == "myclip"


def test_lone_video_waits_for_pair(config, store, fixture_video):
    import shutil

    video = config.input_dir / "lonely.mp4"
    shutil.copy2(fixture_video, video)
    watcher = _watcher(config, store)
    for _ in range(8):
        watcher.scan_once()
        time.sleep(0.05)
    assert watcher.pipeline.ran == []
    assert video.exists()
    assert "lonely" in watcher.status()["pending_pairs"]


def test_pair_timeout_moves_to_failed(config, store, fixture_video):
    import shutil

    config.pair_timeout = 0.3
    video = config.input_dir / "orphan.mp4"
    shutil.copy2(fixture_video, video)
    watcher = _watcher(config, store)
    watcher.scan_once()
    time.sleep(0.5)
    watcher.scan_once()
    assert not video.exists()
    assert (config.failed_dir / "orphan.mp4").exists()
    jobs = store.list_jobs(states=(db.FAILED,))
    assert jobs and "no matching text" in jobs[0].error


def test_no_duplicate_job_for_same_path(config, store, input_pair):
    watcher = _watcher(config, store)
    # force one scan cycle to complete processing
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not watcher.pipeline.ran:
        watcher.scan_once()
        time.sleep(0.1)
    ran_before = len(watcher.pipeline.ran)
    for _ in range(3):
        watcher.scan_once()
    assert len(watcher.pipeline.ran) == ran_before


def test_retry_pending_is_requeued(config, store, input_pair):
    video, text = input_pair
    watcher = _watcher(config, store)
    job = store.create_job("myclip", str(video), str(text))
    store.set_state(job.id, db.RETRY_PENDING)
    watcher.scan_once()
    assert (job.id, False) in watcher.pipeline.ran


def test_retry_with_missing_sources_fails(config, store):
    watcher = _watcher(config, store)
    job = store.create_job("gone", str(config.input_dir / "gone.mp4"),
                           str(config.input_dir / "gone.txt"))
    store.set_state(job.id, db.RETRY_PENDING)
    watcher.scan_once()
    assert store.get(job.id).state == db.FAILED
