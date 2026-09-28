"""Guards for the product decision: the app never creates a thumbnail/cover.

The creator makes the cover manually and selects it inside TikTok. These tests
fail loudly if image generation ever sneaks back into the production path.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.content.analyzer import analyze
from app.jobs.pipeline import Pipeline
from app.jobs.states import JobState
from app.parsing.text_parser import parse_text
from app.tiktok.client import TikTokClient
from app.watcher.watcher import WatcherService

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- code is gone
@pytest.mark.parametrize("module", [
    "app.images", "app.images.registry", "app.images.base",
    "app.images.providers.pollinations", "app.images.providers.comfyui",
    "app.images.providers.automatic1111", "app.images.providers.huggingface",
    "app.images.providers.offline",
    "app.cover", "app.cover.compose", "app.cover.presets", "app.cover.fonts",
])
def test_image_modules_do_not_exist(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_image_provider_settings_remain():
    fields = set(Settings.model_fields)
    forbidden = [f for f in fields if any(t in f for t in
                 ("pollinations", "comfyui", "automatic1111", "huggingface",
                  "image_provider", "cover_", "style_preset", "offline"))]
    # instagram_cover_mode only records that the *user* picks the cover manually
    forbidden = [f for f in forbidden if f != "instagram_cover_mode"]
    assert forbidden == []
    from app.config import InstagramCoverMode
    assert list(InstagramCoverMode) == [InstagramCoverMode.MANUAL]


def test_source_tree_has_no_image_generation_calls():
    """Belt and braces: no provider endpoints are referenced anywhere in app/."""
    needles = ("image.pollinations.ai", "sdapi/v1/txt2img", "hf-inference",
               "/prompt/", "ImageProvider", "CoverComposer")
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}: {n}" for n in needles if n in text]
    assert hits == []


def test_pillow_is_not_imported_by_the_app():
    hits = [p.name for p in (REPO / "app").rglob("*.py")
            if "from PIL" in p.read_text(encoding="utf-8")
            or "import PIL" in p.read_text(encoding="utf-8")]
    assert hits == []


# ---------------------------------------------------------------- txt handling
def test_image_prompt_is_accepted_and_preserved():
    meta = parse_text("TITLE: T\nDESCRIPTION: Eine Beschreibung über Intuition.\n"
                      "IMAGE_PROMPT: a cinematic mystical night scene")
    plan = analyze(meta)
    assert plan.image_prompt == "a cinematic mystical night scene"
    assert plan.caption and plan.hashtags


def test_txt_without_image_prompt_works():
    plan = analyze(parse_text("TITLE: T\nDESCRIPTION: Eine Beschreibung über Intuition."))
    assert plan.image_prompt == ""


# ---------------------------------------------------------------- pipeline
def test_pipeline_makes_no_http_call_and_no_cover(settings, db, sample_video, monkeypatch):
    video = settings.input_dir / "clean.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "clean.txt").write_text(
        "TITLE:\nDie Zeichen\n\nDESCRIPTION:\nEine mystische Betrachtung über Intuition.\n\n"
        "IMAGE_PROMPT:\nA cinematic mystical night scene, deep blue tones.\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("no outbound HTTP request may happen in DRY_RUN")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.Client, "request", boom)

    watcher = WatcherService(settings, db, Pipeline(settings, db))
    job = db.get_job(watcher.scan_once()[0])
    assert job["state"] == JobState.COMPLETED.value, job.get("error")

    out = Path(job["output_dir"])
    assert {p.name for p in out.iterdir()} == {
        "source.mp4", "metadata.txt", "final_tiktok.mp4", "caption.txt",
        "job.json", "upload_result.json", "source_reference.txt"}
    assert not list(out.rglob("*.png")) and not list(out.rglob("*cover*"))
    assert not list(settings.processing_dir.rglob("*.png"))

    record = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert record["cover"]["generated"] is False
    assert record["plan"]["image_prompt"].startswith("A cinematic mystical night scene")
    assert record["video_build"]["cover_embedded"] is False

    events = {e["event"] for e in db.events(job["id"])}
    assert not events & {"GENERATING_IMAGE", "IMAGE_PREFLIGHT", "OFFLINE_FALLBACK_USED",
                         "COVER_GENERATED"}


def test_covers_directory_is_not_created(settings, db, sample_video, metadata_text):
    video = settings.input_dir / "nodir.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "nodir.txt").write_text(metadata_text, encoding="utf-8")
    Pipeline(settings, db).run(db.create_job(
        base_name="nodir", video_path=str(video),
        metadata_path=str(settings.input_dir / "nodir.txt"), dry_run=True))
    assert not (settings.base_dir / "covers").exists()
    assert not hasattr(settings, "covers_dir")


# ---------------------------------------------------------------- TikTok payloads
def test_draft_init_payload_has_no_cover_field(settings, tmp_path):
    """The inbox/draft request must contain only source_info."""
    from app.tiktok.tokens import TokenSet, TokenStore
    import time

    TokenStore(settings.token_path).save(TokenSet(
        access_token="act.test", refresh_token="rft.test",
        expires_at=time.time() + 9999, refresh_expires_at=time.time() + 99999))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 20_000)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inbox/video/init/"):
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={"data": {"publish_id": "p1",
                                                      "upload_url": "https://u/x"}})
        return httpx.Response(201)

    client = TikTokClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.upload_draft(video)
    client.close()

    body = captured["body"]
    assert set(body) == {"source_info"}
    assert "video_cover_timestamp_ms" not in json.dumps(body)
    assert "post_info" not in body


def test_direct_post_payload_has_no_cover_timestamp(settings, db, sample_video, metadata_text):
    """Even the optional Direct Post path no longer sets a cover timestamp."""
    settings.dry_run = False
    settings.tiktok_mock = True
    settings.tiktok_post_mode = "DIRECT_POST"
    settings.confirm_direct_post = True
    settings.tiktok_scopes = "user.info.basic,video.publish"
    video = settings.input_dir / "direct.mp4"
    video.write_bytes(sample_video.read_bytes())
    (settings.input_dir / "direct.txt").write_text(metadata_text, encoding="utf-8")

    job = Pipeline(settings, db).run(db.create_job(
        base_name="direct", video_path=str(video),
        metadata_path=str(settings.input_dir / "direct.txt"), dry_run=False))
    assert job["state"] == JobState.COMPLETED.value, job.get("error")
    post_info = job["upload_result"]["init_response"]["post_info"]
    assert "video_cover_timestamp_ms" not in post_info
    assert post_info["title"]
