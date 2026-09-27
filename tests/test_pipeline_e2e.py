"""End-to-end pipeline tests with real FFmpeg, real Pillow, real SQLite.

Only the TikTok upload is mocked (dry run / stub uploader).
"""

import json
import shutil
from pathlib import Path

from PIL import Image

from tta import db
from tta.pipeline import Pipeline
from tta.video import probe, validate_final


def _pair(config, fixture_video, name="clip"):
    video = config.processing_dir / f"{name}.mp4"
    text = config.processing_dir / f"{name}.txt"
    shutil.copy2(fixture_video, video)
    text.write_text(
        "TITLE:\nThe universe sends you signs\n\n"
        "DESCRIPTION:\nTrust your intuition and spiritual energy.\n\n"
        "IMAGE_PROMPT:\na lone silhouette on a mountain under a glowing night sky\n",
        encoding="utf-8",
    )
    return video, text


def test_dry_run_full_pipeline(config, store, fixture_video):
    video, text = _pair(config, fixture_video)
    original_bytes = video.read_bytes()
    pipeline = Pipeline(config, store)   # dry_run=True from config fixture
    job = store.create_job("clip", str(video), str(text))
    job = pipeline.run_job(job)

    assert job.state == db.COMPLETED, job.error
    out = Path(job.output_dir)
    # all artifacts exist
    for artifact in ("source.mp4", "metadata.txt", "background.png",
                     "cover.png", "final_tiktok.mp4", "job.json",
                     "upload_result.json"):
        assert (out / artifact).is_file(), f"missing {artifact}"

    # cover is 1080x1920 PNG
    with Image.open(out / "cover.png") as img:
        assert img.size == (1080, 1920) and img.format == "PNG"
    # covers gallery copy for the dashboard
    assert (config.covers_dir / f"{job.id}.png").is_file()

    # final video validates
    ts = job.meta["cover_timestamp_ms"]
    report = validate_final(out / "final_tiktok.mp4", cover_timestamp_ms=ts)
    assert report.ok, report.issues

    # dry run: upload skipped, recorded as such
    result = json.loads((out / "upload_result.json").read_text(encoding="utf-8"))
    assert result["skipped"] is True and "DRY_RUN" in result["reason"]
    assert job.upload_status == "skipped_dry_run"

    # source copy identical to original; original moved (unmodified) to archive
    assert (out / "source.mp4").read_bytes() == original_bytes
    archived = config.archive_dir / "clip.mp4"
    assert archived.is_file() and archived.read_bytes() == original_bytes
    assert not video.exists()

    # content plan persisted
    assert job.meta["plan"]["cover_text"]
    assert 2 <= len(job.meta["plan"]["cover_text"].split()) <= 4
    assert job.meta["plan"]["style"] == "CINEMATIC_MYSTICAL"
    assert job.meta["image_provider"] == "local"

    # job.json readable and consistent
    payload = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert payload["state"] == db.COMPLETED
    assert payload["events"][0]["state"] == db.DISCOVERED


def test_duplicate_content_not_reuploaded(config, store, fixture_video):
    pipeline = Pipeline(config, store)
    v1, t1 = _pair(config, fixture_video, "first")
    job1 = pipeline.run_job(store.create_job("first", str(v1), str(t1)))
    assert job1.state == db.COMPLETED

    v2, t2 = _pair(config, fixture_video, "second")  # same video content
    job2 = pipeline.run_job(store.create_job("second", str(v2), str(t2)))
    assert job2.state == db.DUPLICATE
    assert not (Path(job2.output_dir or "") / "final_tiktok.mp4").is_file() \
        if job2.output_dir else True


def test_force_reprocess_overrides_duplicate(config, store, fixture_video):
    pipeline = Pipeline(config, store)
    v1, t1 = _pair(config, fixture_video, "one")
    assert pipeline.run_job(store.create_job("one", str(v1), str(t1))).state == db.COMPLETED
    v2, t2 = _pair(config, fixture_video, "two")
    job2 = pipeline.run_job(store.create_job("two", str(v2), str(t2)), force=True)
    assert job2.state == db.COMPLETED


def test_provider_failure_is_recoverable(config, store, fixture_video, monkeypatch):
    from tta.providers import ProviderError

    pipeline = Pipeline(config, store)
    monkeypatch.setattr(pipeline.registry, "generate",
                        lambda req, out: (_ for _ in ()).throw(ProviderError("cloud down")))
    v, t = _pair(config, fixture_video, "prov")
    job = pipeline.run_job(store.create_job("prov", str(v), str(t)))
    # first failure -> RETRY_PENDING (max_retries=1 in fixture)
    assert job.state == db.RETRY_PENDING
    assert "image generation failed" in job.error
    # second failure -> FAILED, sources moved to failed/
    job = pipeline.run_job(job)
    assert job.state == db.FAILED
    assert (config.failed_dir / "prov.mp4").exists()


def test_corrupt_video_fails_gracefully(config, store):
    pipeline = Pipeline(config, store)
    video = config.processing_dir / "bad.mp4"
    video.write_bytes(b"this is not a video at all" * 100)
    text = config.processing_dir / "bad.txt"
    text.write_text("TITLE: broken\nDESCRIPTION: x\n", encoding="utf-8")
    job = pipeline.run_job(store.create_job("bad", str(video), str(text)))
    assert job.state in (db.RETRY_PENDING, db.FAILED)
    assert job.error


def test_empty_metadata_fails(config, store, fixture_video):
    pipeline = Pipeline(config, store)
    video = config.processing_dir / "nometa.mp4"
    shutil.copy2(fixture_video, video)
    text = config.processing_dir / "nometa.txt"
    text.write_text("   \n", encoding="utf-8")
    job = pipeline.run_job(store.create_job("nometa", str(video), str(text)))
    assert job.state in (db.RETRY_PENDING, db.FAILED)
    assert "empty" in job.error


def test_upload_called_when_not_dry_run(config, store, fixture_video):
    config.dry_run = False
    calls = {}

    def fake_uploader(job, final_path, plan, ts_ms):
        calls["final"] = Path(final_path)
        calls["ts"] = ts_ms
        calls["caption"] = plan.caption
        return {"publish_id": "v_inbox_file~42", "status": "SEND_TO_USER_INBOX"}

    pipeline = Pipeline(config, store, uploader=fake_uploader)
    v, t = _pair(config, fixture_video, "upl")
    job = pipeline.run_job(store.create_job("upl", str(v), str(t)))
    assert job.state == db.COMPLETED
    assert job.publish_id == "v_inbox_file~42"
    assert job.upload_status == "SEND_TO_USER_INBOX"
    assert calls["final"].name == "final_tiktok.mp4"
    assert calls["ts"] == 200  # cover_duration_ms=400 in fixture -> midpoint
    result = json.loads((Path(job.output_dir) / "upload_result.json")
                        .read_text(encoding="utf-8"))
    assert result["publish_id"] == "v_inbox_file~42"


def test_upload_failure_leads_to_retry(config, store, fixture_video):
    config.dry_run = False

    def broken_uploader(job, final_path, plan, ts_ms):
        raise RuntimeError("TikTok said no")

    pipeline = Pipeline(config, store, uploader=broken_uploader)
    v, t = _pair(config, fixture_video, "uplfail")
    job = pipeline.run_job(store.create_job("uplfail", str(v), str(t)))
    assert job.state == db.RETRY_PENDING
    assert "TikTok upload failed" in job.error


def test_regenerate_cover(config, store, fixture_video):
    pipeline = Pipeline(config, store)
    v, t = _pair(config, fixture_video, "regen")
    job = pipeline.run_job(store.create_job("regen", str(v), str(t)))
    assert job.state == db.COMPLETED
    cover = Path(job.cover_path)
    before = cover.read_bytes()
    new_cover = pipeline.regenerate_cover(job.id)
    assert new_cover.is_file()
    with Image.open(new_cover) as img:
        assert img.size == (1080, 1920)
    assert new_cover.read_bytes() != before  # new random seed -> new art


def test_final_video_probe_details(config, store, fixture_video):
    pipeline = Pipeline(config, store)
    v, t = _pair(config, fixture_video, "probe")
    job = pipeline.run_job(store.create_job("probe", str(v), str(t)))
    info = probe(Path(job.output_dir) / "final_tiktok.mp4")
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.fps == 30.0
