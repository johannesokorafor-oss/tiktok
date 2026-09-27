import json
import time

import pytest

from tta.tiktok_api import TokenStore, plan_chunks
from tta.tiktok_api.client import (
    CREATOR_INFO_URL,
    DIRECT_INIT_URL,
    INBOX_INIT_URL,
    STATUS_FETCH_URL,
    TikTokAPIError,
    TikTokClient,
)
from tta.tiktok_api.oauth import OAuthError, build_authorize_url, _parse_token_response

MB = 1024 * 1024


# ------------------------------------------------------------- chunking
def test_single_chunk_small_file():
    size = 3 * MB
    chunk, count, ranges = plan_chunks(size)
    assert (chunk, count) == (size, 1)
    assert ranges == [(0, size - 1)]


def test_single_chunk_up_to_64mb():
    size = 64 * MB
    chunk, count, ranges = plan_chunks(size)
    assert count == 1 and ranges[-1][1] == size - 1


def test_multi_chunk_math():
    size = 87 * MB + 123
    chunk, count, ranges = plan_chunks(size)
    assert chunk == 10 * MB
    assert count == size // chunk           # floor per docs
    assert ranges[0] == (0, chunk - 1)
    assert ranges[-1][1] == size - 1        # final chunk absorbs remainder
    # ranges are contiguous
    for (s1, e1), (s2, e2) in zip(ranges, ranges[1:]):
        assert s2 == e1 + 1
    assert sum(e - s + 1 for s, e in ranges) == size


def test_plan_chunks_rejects_zero():
    with pytest.raises(ValueError):
        plan_chunks(0)


# ------------------------------------------------------------- tokens
def test_token_store_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.load() == {}
    store.save({"access_token": "act.secret", "refresh_token": "rft.secret",
                "expires_in": 86400, "refresh_expires_in": 31536000,
                "open_id": "user-1", "scope": "video.upload"})
    assert store.access_token == "act.secret"
    assert store.is_access_valid()
    assert store.is_refresh_valid()
    status = store.status()
    assert status["authenticated"] and status["open_id"] == "user-1"
    assert "act.secret" not in json.dumps(status)  # no secrets in status
    store.clear()
    assert store.load() == {}


def test_refresh_token_preserved_when_response_omits_it(tmp_path):
    """Per TikTok docs, a refresh response may omit refresh_token - the
    stored one must survive."""
    store = TokenStore(tmp_path / "tokens.json")
    store.save({"access_token": "act.a", "refresh_token": "rft.keepme",
                "expires_in": 100, "refresh_expires_in": 31536000})
    store.save({"access_token": "act.b", "expires_in": 100})  # no refresh_token
    data = store.load()
    assert data["access_token"] == "act.b"
    assert data["refresh_token"] == "rft.keepme"
    assert store.is_refresh_valid()


def test_token_expiry(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save({"access_token": "act.x", "expires_at": time.time() - 10})
    assert not store.is_access_valid()
    assert not store.is_refresh_valid()  # no refresh token at all


# ------------------------------------------------------------- oauth
def test_authorize_url_contains_required_params():
    url, state = build_authorize_url("ck123", "http://127.0.0.1:8765/callback/")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert "client_key=ck123" in url
    assert "response_type=code" in url
    assert f"state={state}" in url
    assert "video.upload" in url
    # states are unique per call
    _, state2 = build_authorize_url("ck123", "http://x/")
    assert state != state2


class _FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_parse_token_response_error():
    resp = _FakeResponse(400, {"error": "invalid_grant",
                               "error_description": "code expired"})
    with pytest.raises(OAuthError, match="invalid_grant"):
        _parse_token_response(resp)


def test_parse_token_response_ok():
    resp = _FakeResponse(200, {"access_token": "act.x", "expires_in": 100})
    assert _parse_token_response(resp)["access_token"] == "act.x"


# ------------------------------------------------------------- client
class FakeSession:
    """Queue-based fake requests.Session recording every call."""

    def __init__(self):
        self.queue = []
        self.calls = []

    def add(self, resp):
        self.queue.append(resp)

    def _next(self):
        if not self.queue:
            raise AssertionError("no more queued responses")
        return self.queue.pop(0)

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json if json is not None else data, headers))
        return self._next()

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, data, headers))
        return self._next()


@pytest.fixture()
def authed_client(tmp_path):
    tokens = TokenStore(tmp_path / "tok.json")
    tokens.save({"access_token": "act.valid", "refresh_token": "rft.valid",
                 "expires_in": 3600})
    session = FakeSession()
    client = TikTokClient("ck", "cs", tokens, session=session,
                          max_retries=2, sleep=lambda s: None)
    return client, session


def test_inbox_init_request_construction(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(200, {
        "data": {"publish_id": "v_inbox_file~1", "upload_url": "https://up/x"},
        "error": {"code": "ok"},
    }))
    data = client.init_inbox_upload(3 * MB)
    method, url, payload, headers = session.calls[0]
    assert (method, url) == ("POST", INBOX_INIT_URL)
    assert payload["source_info"]["source"] == "FILE_UPLOAD"
    assert payload["source_info"]["video_size"] == 3 * MB
    assert payload["source_info"]["total_chunk_count"] == 1
    assert headers["Authorization"] == "Bearer act.valid"
    assert data["publish_id"] == "v_inbox_file~1"


def test_direct_post_includes_cover_timestamp(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(200, {"data": {"publish_id": "p", "upload_url": "u"},
                                    "error": {"code": "ok"}}))
    client.init_direct_post(MB, title="hi", privacy_level="SELF_ONLY",
                            cover_timestamp_ms=250)
    _, url, payload, _ = session.calls[0]
    assert url == DIRECT_INIT_URL
    assert payload["post_info"]["video_cover_timestamp_ms"] == 250
    assert payload["post_info"]["privacy_level"] == "SELF_ONLY"


def test_upload_file_content_range(authed_client, tmp_path):
    client, session = authed_client
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    session.add(_FakeResponse(201))
    client.upload_file("https://up/x", video)
    method, url, data, headers = session.calls[0]
    assert method == "PUT"
    assert headers["Content-Range"] == "bytes 0-999/1000"
    assert headers["Content-Type"] == "video/mp4"
    assert len(data) == 1000


def test_retry_on_500_then_success(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(500))
    session.add(_FakeResponse(200, {"data": {"status": "PROCESSING"},
                                    "error": {"code": "ok"}}))
    data = client.fetch_status("pid")
    assert data["status"] == "PROCESSING"
    assert len(session.calls) == 2
    assert session.calls[0][1] == STATUS_FETCH_URL


def test_rate_limit_respects_retry_after(authed_client):
    client, session = authed_client
    sleeps = []
    client._sleep = sleeps.append
    session.add(_FakeResponse(429, headers={"Retry-After": "7"}))
    session.add(_FakeResponse(200, {"data": {}, "error": {"code": "ok"}}))
    client.creator_info()
    assert 7 in sleeps
    assert session.calls[0][1] == CREATOR_INFO_URL


def test_api_error_code_raises(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(200, {"data": {},
                                    "error": {"code": "spam_risk_too_many_pending_share",
                                              "message": "too many"}}))
    with pytest.raises(TikTokAPIError, match="spam_risk"):
        client.init_inbox_upload(MB)


def test_expired_token_triggers_refresh(tmp_path):
    tokens = TokenStore(tmp_path / "tok.json")
    tokens.save({"access_token": "act.old", "refresh_token": "rft.valid",
                 "expires_at": time.time() - 100})
    session = FakeSession()
    # first call: the oauth refresh (form-encoded POST)
    session.add(_FakeResponse(200, {"access_token": "act.new",
                                    "refresh_token": "rft.new",
                                    "expires_in": 3600}))
    session.add(_FakeResponse(200, {"data": {}, "error": {"code": "ok"}}))
    client = TikTokClient("ck", "cs", tokens, session=session, sleep=lambda s: None)
    client.creator_info()
    refresh_call = session.calls[0]
    assert refresh_call[1].endswith("/v2/oauth/token/")
    assert refresh_call[2]["grant_type"] == "refresh_token"
    assert tokens.access_token == "act.new"
    api_call = session.calls[1]
    assert api_call[3]["Authorization"] == "Bearer act.new"


def test_unauthenticated_raises(tmp_path):
    tokens = TokenStore(tmp_path / "tok.json")
    client = TikTokClient("ck", "cs", tokens, session=FakeSession(),
                          sleep=lambda s: None)
    with pytest.raises(TikTokAPIError, match="not authenticated"):
        client.creator_info()


def test_wait_for_upload_success(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(200, {"data": {"status": "PROCESSING_UPLOAD"},
                                    "error": {"code": "ok"}}))
    session.add(_FakeResponse(200, {"data": {"status": "SEND_TO_USER_INBOX"},
                                    "error": {"code": "ok"}}))
    result = client.wait_for_upload("pid", timeout=30, poll_interval=0)
    assert result["status"] == "SEND_TO_USER_INBOX"


def test_wait_for_upload_failure(authed_client):
    client, session = authed_client
    session.add(_FakeResponse(200, {"data": {"status": "FAILED",
                                             "fail_reason": "file_format_check_failed"},
                                    "error": {"code": "ok"}}))
    with pytest.raises(TikTokAPIError, match="file_format_check_failed"):
        client.wait_for_upload("pid", timeout=30, poll_interval=0)
