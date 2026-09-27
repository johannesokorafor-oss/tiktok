import pytest
from fastapi.testclient import TestClient

from app.dashboard.server import AppContext, create_app


@pytest.fixture()
def client(settings):
    ctx = AppContext(settings, autostart=False)
    app = create_app(ctx, autostart=False)
    with TestClient(app) as c:
        yield c, ctx
    ctx.close()


def test_status_endpoint_never_leaks_tokens(client):
    c, _ = client
    body = c.get("/api/status").json()
    text = str(body)
    for secret in ("act.", "rft.", "test-secret", "Bearer "):
        assert secret not in text
    assert body["settings"]["dry_run"] is True
    assert body["mode"]["effective"] == "DRY_RUN"
    assert body["mode"]["never_auto_publishes"] is True
    assert "providers" not in body and "image_quality" not in body
    assert body["cover_policy"]["generated_by_app"] is False


def test_jobs_endpoints(client, sample_video, metadata_text):
    c, ctx = client
    video = ctx.settings.input_dir / "dash.mp4"
    video.write_bytes(sample_video.read_bytes())
    (ctx.settings.input_dir / "dash.txt").write_text(metadata_text, encoding="utf-8")
    ctx.watcher.scan_once()

    jobs = c.get("/api/jobs").json()["jobs"]
    assert jobs and jobs[0]["state"] == "COMPLETED"
    jid = jobs[0]["id"]

    detail = c.get(f"/api/jobs/{jid}").json()
    assert detail["plan"]["caption"] and detail["plan"]["language"]
    assert "cover_text" not in detail["plan"]
    assert detail["cover_path"] is None
    assert detail["events"]

    assert c.get(f"/api/jobs/{jid}/cover").status_code == 404      # endpoint removed
    caption = c.get(f"/api/jobs/{jid}/caption").json()
    assert caption["caption"] and "no caption" in caption["note"]

    r = c.post(f"/api/jobs/{jid}/overrides", json={"caption": "my caption"})
    assert r.json()["overrides"]["caption"] == "my caption"

    assert c.get("/api/jobs/does-not-exist").status_code == 404


def test_watcher_control_endpoints(client):
    c, _ = client
    assert c.post("/api/watcher/scan").status_code == 200
    assert c.post("/api/watcher/start").json()["running"] is True
    assert c.post("/api/watcher/stop").json()["running"] is False
    assert c.post("/api/watcher/nonsense").status_code == 400


def test_oauth_callback_validates_state(client):
    c, _ = client
    r = c.get("/tiktok/callback", params={"code": "abc", "state": "forged"})
    assert r.status_code == 400 and "state" in r.text.lower()


def test_index_renders_without_image_ui(client):
    c, _ = client
    html = c.get("/").text
    assert "TikTok Draft Upload Automation" in html
    assert "User creates and selects this manually in TikTok" in html
    for gone in ("regenerate-cover", "Image providers", "ov-prompt", "ov-style"):
        assert gone not in html


def test_regenerate_cover_endpoint_is_gone(client):
    c, ctx = client
    assert c.post("/api/jobs/whatever/regenerate-cover").status_code == 404


