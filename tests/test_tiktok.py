"""TikTok API tests - fully offline, using httpx MockTransport."""
from __future__ import annotations

import json

import time

import httpx
import pytest

from app.tiktok.client import (MIN_CHUNK, RateLimiter, TikTokAPIError, TikTokClient,
                               build_client, plan_chunks)
from app.tiktok.oauth import OAuthError, StateStore, TikTokOAuth
from app.tiktok.tokens import TokenSet, TokenStore


# ----------------------------------------------------------------- OAuth
def test_authorize_url_contains_required_params(settings):
    oauth = TikTokOAuth(settings)
    auth = oauth.authorize_url()
    assert auth.url.startswith("https://www.tiktok.com/v2/auth/authorize/")
    for part in ("client_key=test-key", "response_type=code", "scope=", "state="):
        assert part in auth.url


def test_oauth_state_validation():
    store = StateStore()
    s = store.issue()
    assert store.validate(s)
    assert not store.validate(s)         # single use
    assert not store.validate("forged")
    assert not store.validate(None)


def test_exchange_code_rejects_bad_state(settings):
    oauth = TikTokOAuth(settings)
    with pytest.raises(OAuthError):
        oauth.exchange_code("code123", "not-issued")


def test_exchange_code_persists_tokens(settings, monkeypatch):
    oauth = TikTokOAuth(settings)
    state = oauth.states.issue()
    captured = {}

    def fake_post(self, url, data=None, headers=None):
        captured["url"], captured["data"] = url, data
        return httpx.Response(200, json={"access_token": "act.demo", "refresh_token": "rft.demo",
                                         "open_id": "openid123", "scope": "video.upload",
                                         "expires_in": 86400, "refresh_expires_in": 31536000},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    tokens = oauth.exchange_code("code123", state)
    assert captured["url"].endswith("/v2/oauth/token/")
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["redirect_uri"] == settings.tiktok_redirect_uri
    assert tokens.access_token == "act.demo"
    assert TokenStore(settings.token_path).load().refresh_token == "rft.demo"


def test_refresh_rotates_refresh_token(settings, monkeypatch):
    store = TokenStore(settings.token_path)
    store.save(TokenSet(access_token="old", refresh_token="rft.old", expires_at=time.time() - 10,
                        refresh_expires_at=time.time() + 1000))
    oauth = TikTokOAuth(settings, store=store)

    def fake_post(self, url, data=None, headers=None):
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "rft.old"
        return httpx.Response(200, json={"access_token": "act.new", "refresh_token": "rft.new",
                                         "expires_in": 86400},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    tokens = oauth.valid_tokens()
    assert tokens.access_token == "act.new"
    assert store.load().refresh_token == "rft.new"


def test_error_response_raises(settings, monkeypatch):
    oauth = TikTokOAuth(settings)
    state = oauth.states.issue()
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, data=None, headers=None:
                        httpx.Response(400, json={"error": "invalid_grant",
                                                  "error_description": "expired"},
                                       request=httpx.Request("POST", url)))
    with pytest.raises(OAuthError):
        oauth.exchange_code("c", state)


def test_tokenset_never_exposes_secrets():
    ts = TokenSet(access_token="act.secret", refresh_token="rft.secret", open_id="abcdef123")
    public = ts.public_dict()
    assert "act.secret" not in str(public) and "rft.secret" not in str(public)


# ----------------------------------------------------------------- chunks
def test_plan_chunks_small_file_single_chunk():
    assert plan_chunks(1_000_000) == (1_000_000, 1)


def test_plan_chunks_respects_bounds():
    size = 200 * 1024 * 1024
    chunk, count = plan_chunks(size)
    assert MIN_CHUNK <= chunk <= 64 * 1024 * 1024
    assert count >= 1 and chunk * (count - 1) < size
    last = size - chunk * (count - 1)
    assert last <= 128 * 1024 * 1024


def test_plan_chunks_rejects_zero():
    with pytest.raises(ValueError):
        plan_chunks(0)


# ----------------------------------------------------------------- client
def _client(settings, handler, tokens=True):
    if tokens:
        TokenStore(settings.token_path).save(
            TokenSet(access_token="act.test", refresh_token="rft.test",
                     expires_at=time.time() + 9999, refresh_expires_at=time.time() + 99999))
    transport = httpx.MockTransport(handler)
    return TikTokClient(settings, client=httpx.Client(transport=transport))


def test_upload_draft_uses_inbox_endpoint_and_chunk_headers(settings, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 1_500_000)
    seen = {"puts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/post/publish/inbox/video/init/":
            body = json.loads(request.read().decode())
            assert body["source_info"]["source"] == "FILE_UPLOAD"
            assert body["source_info"]["video_size"] == 1_500_000
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(200, json={"data": {"publish_id": "pub_1",
                                                      "upload_url": "https://upload.tiktok/x"},
                                             "error": {"code": "ok"}})
        if request.url.path == "/v2/post/publish/status/fetch/":
            return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"},
                                             "error": {"code": "ok"}})
        if request.method == "PUT":
            seen["puts"].append(dict(request.headers))
            return httpx.Response(201)
        raise AssertionError(f"unexpected call {request.method} {request.url}")

    client = _client(settings, handler)
    outcome = client.upload_draft(video)
    assert outcome.publish_id == "pub_1" and outcome.mode == "UPLOAD"
    assert outcome.uploaded_bytes == 1_500_000
    hdr = seen["puts"][0]
    assert hdr["content-type"] == "video/mp4"
    assert hdr["content-range"] == "bytes 0-1499999/1500000"
    status = client.wait_for_status("pub_1", sleeper=lambda s: None)
    assert status["status"] == "SEND_TO_USER_INBOX"


def test_direct_post_sends_post_info_with_cover_timestamp(settings, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 50_000)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/post/publish/video/init/":
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={"data": {"publish_id": "pub_2",
                                                      "upload_url": "https://upload.tiktok/y"}})
        return httpx.Response(200)

    client = _client(settings, handler)
    client.direct_post(video, {"title": "caption", "privacy_level": "SELF_ONLY",
                               "video_cover_timestamp_ms": 60})
    assert captured["body"]["post_info"]["video_cover_timestamp_ms"] == 60
    assert captured["body"]["post_info"]["privacy_level"] == "SELF_ONLY"


def test_rate_limit_429_is_retried_then_succeeds(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded",
                                                       "message": "slow down"}})
        return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}})

    client = _client(settings, handler)
    data = client.fetch_status("pub_1")
    assert calls["n"] == 2 and data["status"] == "SEND_TO_USER_INBOX"


def test_non_retryable_error_raises_immediately(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "scope_not_authorized",
                                                   "message": "missing scope"}})
    client = _client(settings, handler)
    with pytest.raises(TikTokAPIError) as exc:
        client.fetch_status("pub_1")
    assert exc.value.code == "scope_not_authorized" and not exc.value.retryable


def test_retries_are_bounded(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"code": "internal_error"}})

    client = _client(settings, handler)
    with pytest.raises(TikTokAPIError):
        client.fetch_status("pub_1")
    assert calls["n"] == settings.tiktok_max_retries + 1


def test_rate_limiter_blocks_over_budget():
    slept = []
    limiter = RateLimiter(2, per_seconds=60)
    for _ in range(2):
        limiter.acquire(sleeper=slept.append)
    assert slept == []
    limiter.acquire(sleeper=lambda s: slept.append(s))
    assert slept and slept[0] > 0


def test_mock_client_never_touches_network(settings, tmp_path):
    settings.tiktok_mock = True
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 10_000)
    client = build_client(settings)
    outcome = client.upload_draft(video)
    assert outcome.publish_id.startswith("mock.")
    assert outcome.status == "SEND_TO_USER_INBOX"
