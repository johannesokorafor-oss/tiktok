import shutil

import pytest
from fastapi.testclient import TestClient

from tta import db
from tta.dashboard import create_app
from tta.pipeline import Pipeline
from tta.providers import build_registry


@pytest.fixture()
def client(config, store):
    pipeline = Pipeline(config, store)
    app = create_app(config, store, pipeline=pipeline,
                     registry=pipeline.registry)
    return TestClient(app)


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "TikTok Auto-Poster" in resp.text


def test_status_endpoint(client):
    data = client.get("/api/status").json()
    assert "watcher" in data and "providers" in data and "tiktok" in data
    assert data["selected_provider"] == "local"
    names = {p["name"]: p for p in data["providers"]}
    assert names["openai"]["cost"] == "PAID"
    assert data["tiktok"]["configured"] is False


def test_jobs_listing_and_detail(client, store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    store.set_state(job.id, db.FAILED, error="boom")
    listing = client.get("/api/jobs").json()["jobs"]
    assert listing[0]["id"] == job.id
    assert listing[0]["state"] == db.FAILED
    detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["error"] == "boom"
    assert detail["events"]


def test_job_detail_404(client):
    assert client.get("/api/jobs/doesnotexist").status_code == 404


def test_cover_endpoint(client, config, store, fixture_video, tmp_path):
    from tta.providers.base import ImageRequest
    from tta.providers.local_art import LocalArtProvider

    job = store.create_job("clip", "/v.mp4", "/t.txt")
    LocalArtProvider().generate(ImageRequest(prompt="x", seed=5),
                                config.covers_dir / f"{job.id}.png")
    resp = client.get(f"/covers/{job.id}.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert client.get("/covers/unknown.png").status_code == 404


def test_retry_endpoint_validations(client, store):
    assert client.post("/api/jobs/nope/retry").status_code == 404
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    resp = client.post(f"/api/jobs/{job.id}/retry")
    assert resp.status_code == 409  # DISCOVERED is not retryable


def test_regenerate_cover_endpoint_missing_plan(client, store):
    job = store.create_job("clip", "/v.mp4", "/t.txt")
    resp = client.post(f"/api/jobs/{job.id}/regenerate-cover")
    assert resp.status_code == 400
