"""Hardening tests: file safety, slow copies, restart recovery, concurrent
execution, corrupt provider payloads, watcher survival after failures."""

import shutil
import threading
import time
from pathlib import Path

import pytest

from tta import db
from tta.hashing import sha256_file
from tta.pipeline import Pipeline
from tta.providers import ProviderError
from tta.providers.base import write_normalized_png
from tta.watcher import Watcher


# ------------------------------------------------------------------ file safety
def test_original_files_bytes_unchanged_after_full_run(config, store, fixture_video):
    """Hash originals before processing; the archived files must be
    byte-identical after the complete pipeline ran."""
    video = config.input_dir / "orig.mp4"
    text = config.input_dir / "orig.txt"
    shutil.copy2(fixture_video, video)
    text.write_text("TITLE: Keep me safe\nDESCRIPTION: bytes must not change\n",
                    encoding="utf-8")
    video_hash = sha256_file(video)
    text_hash = sha256_file(text)

    pipeline = Pipeline(config, store)
    watcher = Watcher(config, store, pipeline)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        watcher.scan_once()
        jobs = store.list_jobs()
        if jobs and jobs[0].state in (db.COMPLETED, db.FAILED):
            break
        time.sleep(0.1)

    job = store.list_jobs()[0]
    assert job.state == db.COMPLETED, job.error
    archived_video = config.archive_dir / "orig.mp4"
    archived_text = config.archive_dir / "orig.txt"
    assert sha256_file(archived_video) == video_hash
    assert sha256_file(archived_text) == text_hash


# ------------------------------------------------------------------ slow copy
def test_slow_copy_not_processed_until_complete(config, store, fixture_video):
    """Simulate a file being copied in chunks - the watcher must not touch
    it while it is still growing."""
    calls = []

    class Recorder:
        def run_job(self, job, force=False):
            calls.append(job.id)
            store.set_state(job.id, db.COMPLETED)
            return store.get(job.id)

    config.stabilize_seconds = 0.5
    watcher = Watcher(config, store, Recorder())

    src_bytes = Path(fixture_video).read_bytes()
    video = config.input_dir / "slow.mp4"
    (config.input_dir / "slow.txt").write_text("TITLE: slow\n", encoding="utf-8")

    stop_writing = threading.Event()

    def writer():
        with open(video, "wb") as fh:
            for i in range(0, len(src_bytes), len(src_bytes) // 8 + 1):
                fh.write(src_bytes[i:i + len(src_bytes) // 8 + 1])
                fh.flush()
                watcher.scan_once()          # scans happen mid-copy
                time.sleep(0.05)
        stop_writing.set()

    thread = threading.Thread(target=writer)
    thread.start()
    while not stop_writing.is_set():
        watcher.scan_once()
        assert calls == [], "processed a file that was still being written!"
        time.sleep(0.03)
    thread.join()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not calls:
        watcher.scan_once()
        time.sleep(0.1)
    assert len(calls) == 1


# ------------------------------------------------------------------ restart
def test_restart_recovers_interrupted_job(config, fixture_video):
    """Job dies mid-flight -> new process (new JobStore) must requeue it."""
    video = config.processing_dir / "boom.mp4"
    text = config.processing_dir / "boom.txt"
    shutil.copy2(fixture_video, video)
    text.write_text("TITLE: interrupted\n", encoding="utf-8")

    store1 = db.JobStore(config.db_path)
    job = store1.create_job("boom", str(video), str(text))
    store1.set_state(job.id, db.GENERATING_IMAGE)   # "crash" here
    store1.close()

    store2 = db.JobStore(config.db_path)             # simulated restart
    recovered = store2.recover_interrupted()
    assert [j.id for j in recovered] == [job.id]
    persisted = store2.get(job.id)
    assert persisted.state == db.RETRY_PENDING
    events = store2.events(job.id)
    assert events[-1]["state"] == db.RETRY_PENDING
    assert "recovered after restart" in events[-1]["message"]

    # and the watcher actually re-runs it
    pipeline = Pipeline(config, store2)
    watcher = Watcher(config, store2, pipeline)
    watcher.scan_once()
    assert store2.get(job.id).state in (db.COMPLETED, db.RETRY_PENDING, db.FAILED)
    store2.close()


# ------------------------------------------------------------------ concurrency
def test_same_job_never_runs_concurrently(config, store, fixture_video):
    video = config.processing_dir / "conc.mp4"
    text = config.processing_dir / "conc.txt"
    shutil.copy2(fixture_video, video)
    text.write_text("TITLE: concurrent\n", encoding="utf-8")
    job = store.create_job("conc", str(video), str(text))

    pipeline = Pipeline(config, store)
    executions = []
    original_execute = pipeline._execute

    def slow_execute(j, force=False):
        executions.append(threading.current_thread().name)
        time.sleep(0.4)
        return original_execute(j, force=force)

    pipeline._execute = slow_execute
    threads = [threading.Thread(target=pipeline.run_job, args=(job,), name=f"t{i}")
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(executions) == 1, f"job executed {len(executions)} times concurrently"


# ------------------------------------------------------------------ corrupt images
def test_write_normalized_png_rejects_garbage(tmp_path):
    out = tmp_path / "img.png"
    with pytest.raises(ProviderError, match="invalid image"):
        write_normalized_png(b"this is definitely not an image", out, 100, 200)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []      # no .part leftovers


def test_write_normalized_png_accepts_and_resizes(tmp_path):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (50, 50), (200, 10, 10)).save(buf, "JPEG")
    out = tmp_path / "img.png"
    write_normalized_png(buf.getvalue(), out, 108, 192)
    with Image.open(out) as img:
        assert img.size == (108, 192) and img.format == "PNG"


class _GarbageSession:
    """Fake requests session returning image content-type with garbage body."""

    class _Resp:
        ok = True
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        content = b"garbage-bytes-not-an-image"
        text = ""

    def get(self, *args, **kwargs):
        return self._Resp()


def test_pollinations_garbage_body_raises_provider_error(tmp_path):
    from tta.providers.pollinations import PollinationsProvider

    provider = PollinationsProvider(session=_GarbageSession())
    from tta.providers.base import ImageRequest

    with pytest.raises(ProviderError, match="invalid image"):
        provider.generate(ImageRequest(prompt="x", width=100, height=200),
                          tmp_path / "img.png")
    assert not (tmp_path / "img.png").exists()


class _TimeoutSession:
    def get(self, *args, **kwargs):
        import requests

        raise requests.ConnectTimeout("simulated timeout")


def test_pollinations_timeout_raises_provider_error(tmp_path):
    from tta.providers.base import ImageRequest
    from tta.providers.pollinations import PollinationsProvider

    provider = PollinationsProvider(session=_TimeoutSession())
    with pytest.raises(ProviderError, match="request failed"):
        provider.generate(ImageRequest(prompt="x"), tmp_path / "img.png")


def test_registry_falls_back_on_corrupt_cloud_image(tmp_path):
    from tta.providers.base import ImageRequest
    from tta.providers.local_art import LocalArtProvider
    from tta.providers.pollinations import PollinationsProvider
    from tta.providers.registry import ProviderRegistry

    registry = ProviderRegistry(
        {"pollinations": PollinationsProvider(session=_GarbageSession()),
         "local": LocalArtProvider()},
        preference="pollinations", fallback_to_local=True,
    )
    out = tmp_path / "img.png"
    path, used = registry.generate(ImageRequest(prompt="x", seed=1), out)
    assert used == "local" and path.exists()


# ------------------------------------------------------------------ watcher survival
def test_watcher_survives_bad_job_and_processes_next(config, store, fixture_video):
    """A corrupt video job must not prevent the next good pair from
    completing."""
    pipeline = Pipeline(config, store)
    watcher = Watcher(config, store, pipeline)

    (config.input_dir / "bad.mp4").write_bytes(b"not a real video" * 500)
    (config.input_dir / "bad.txt").write_text("TITLE: broken\n", encoding="utf-8")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        watcher.scan_once()
        bad = [j for j in store.list_jobs() if j.base_name == "bad"]
        if bad and bad[0].state == db.FAILED:
            break
        time.sleep(0.1)
    bad = [j for j in store.list_jobs() if j.base_name == "bad"]
    assert bad and bad[0].state == db.FAILED
    assert bad[0].error
    assert (config.failed_dir / "bad.mp4").exists()   # moved, not deleted

    # now a good pair must still work
    shutil.copy2(fixture_video, config.input_dir / "good.mp4")
    (config.input_dir / "good.txt").write_text("TITLE: works fine\n",
                                               encoding="utf-8")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        watcher.scan_once()
        good = [j for j in store.list_jobs() if j.base_name == "good"]
        if good and good[0].state == db.COMPLETED:
            break
        time.sleep(0.1)
    good = [j for j in store.list_jobs() if j.base_name == "good"]
    assert good and good[0].state == db.COMPLETED, good and good[0].error


def test_missing_file_at_run_time_is_controlled_failure(config, store):
    pipeline = Pipeline(config, store)
    job = store.create_job("ghost", str(config.processing_dir / "ghost.mp4"),
                           str(config.processing_dir / "ghost.txt"))
    job = pipeline.run_job(job)
    assert job.state in (db.RETRY_PENDING, db.FAILED)
    assert "disappeared" in job.error
