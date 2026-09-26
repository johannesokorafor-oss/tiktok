"""TikTok Content Posting API client (official API only).

Implemented against the current TikTok for Developers documentation
(verified 2026-09-26):

  Upload (draft / inbox, scope ``video.upload``)
      POST /v2/post/publish/inbox/video/init/
          body: {"source_info": {"source": "FILE_UPLOAD", "video_size",
                 "chunk_size", "total_chunk_count"}}
          -> {"data": {"publish_id", "upload_url"}}
      PUT  {upload_url}   headers: Content-Type, Content-Length, Content-Range
      The video lands in the creator's TikTok inbox; the creator opens the
      notification and finishes/publishes the post inside the TikTok app.
      No caption/cover fields are accepted by this endpoint - the creator
      sets them in the TikTok editor, which is exactly the review workflow
      this application targets.

  Direct Post (optional, scope ``video.publish``)
      POST /v2/post/publish/video/init/  with post_info (title,
      privacy_level, disable_*, video_cover_timestamp_ms, is_aigc ...)

  Creator info        POST /v2/post/publish/creator_info/query/   (20 rpm)
  Status              POST /v2/post/publish/status/fetch/         (30 rpm)

Rate limits enforced client-side: 6 init/min, 30 status/min, 20 creator/min
per user access token, plus exponential backoff on HTTP 429/5xx.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.config import Settings
from app.tiktok.oauth import OAuthError, TikTokOAuth

log = logging.getLogger(__name__)

MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
MAX_FINAL_CHUNK = 128 * 1024 * 1024
MAX_CHUNKS = 1000

MIME_BY_SUFFIX = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class TikTokAPIError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[str] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class RateLimiter:
    """Simple per-endpoint sliding-window limiter."""

    def __init__(self, max_calls: int, per_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.per = per_seconds
        self._calls: List[float] = []
        self._lock = threading.Lock()

    def acquire(self, sleeper: Callable[[float], None] = time.sleep) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.per]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited
                sleep_for = self.per - (now - self._calls[0]) + 0.05
            waited += sleep_for
            sleeper(sleep_for)


def plan_chunks(size: int, preferred: int = MAX_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) per the documented chunk rules."""
    if size <= 0:
        raise ValueError("video size must be positive")
    if size < MIN_CHUNK:
        return size, 1                      # small files must be a single chunk
    chunk = max(MIN_CHUNK, min(preferred, MAX_CHUNK))
    count = size // chunk                   # trailing bytes ride along with the last chunk
    if count > MAX_CHUNKS:
        count = MAX_CHUNKS
        chunk = size // count
        chunk = min(max(chunk, MIN_CHUNK), MAX_CHUNK)
        count = size // chunk
    if count < 1:
        return size, 1
    last = size - chunk * (count - 1)
    if last > MAX_FINAL_CHUNK:
        count += 1
        last = size - chunk * (count - 1)
        if last <= 0:
            count -= 1
    return chunk, count


@dataclass
class UploadOutcome:
    publish_id: str
    mode: str
    status: str = "PENDING"
    raw_status: Dict[str, Any] = field(default_factory=dict)
    init_response: Dict[str, Any] = field(default_factory=dict)
    chunks: int = 0
    chunk_size: int = 0
    uploaded_bytes: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"publish_id": self.publish_id, "mode": self.mode, "status": self.status,
                "chunks": self.chunks, "chunk_size": self.chunk_size,
                "uploaded_bytes": self.uploaded_bytes, "raw_status": self.raw_status,
                "errors": self.errors}


class TikTokClient:
    def __init__(self, settings: Settings, oauth: Optional[TikTokOAuth] = None,
                 client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.oauth = oauth or TikTokOAuth(settings)
        self._client = client
        self._owns_client = client is None
        self.init_limiter = RateLimiter(6)
        self.status_limiter = RateLimiter(30)
        self.creator_limiter = RateLimiter(20)

    # ------------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=120.0)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _auth_headers(self) -> Dict[str, str]:
        tokens = self.oauth.valid_tokens()
        return {"Authorization": f"Bearer {tokens.access_token}",
                "Content-Type": "application/json; charset=UTF-8"}

    def _url(self, path: str) -> str:
        return self.settings.tiktok_api_base.rstrip("/") + path

    # ------------------------------------------------------------------
    def _post_json(self, path: str, payload: dict, limiter: Optional[RateLimiter] = None) -> dict:
        if limiter:
            limiter.acquire()
        attempts = 0
        last_error: Optional[TikTokAPIError] = None
        while attempts <= self.settings.tiktok_max_retries:
            attempts += 1
            try:
                resp = self._http().post(self._url(path), json=payload, headers=self._auth_headers())
            except OAuthError:
                raise
            except httpx.HTTPError as exc:
                last_error = TikTokAPIError(f"network error calling {path}: {exc}", retryable=True)
            else:
                body: Dict[str, Any]
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                err = (body.get("error") or {}) if isinstance(body.get("error"), dict) else {}
                code = err.get("code", "")
                if resp.status_code < 400 and code in ("", "ok"):
                    return body
                retryable = resp.status_code == 429 or resp.status_code >= 500 or \
                    code in {"rate_limit_exceeded", "internal_error"}
                message = (f"TikTok API {path} failed: HTTP {resp.status_code} "
                           f"code={code or 'n/a'} message={err.get('message', resp.text[:200])}")
                last_error = TikTokAPIError(message, status=resp.status_code, code=code,
                                            retryable=retryable)
                if not retryable:
                    raise last_error
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                    continue
            backoff = min(60.0, self.settings.tiktok_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying %s in %.1fs (attempt %d): %s", path, backoff, attempts, last_error)
            time.sleep(backoff)
        raise last_error or TikTokAPIError(f"TikTok API {path} failed")

    # ------------------------------------------------------------------
    def query_creator_info(self) -> dict:
        """POST /v2/post/publish/creator_info/query/ (required before Direct Post)."""
        body = self._post_json("/v2/post/publish/creator_info/query/", {}, self.creator_limiter)
        return body.get("data", {})

    # ------------------------------------------------------------------
    def upload_draft(self, video_path: Path, *, progress: Optional[Callable[[int, int], None]] = None
                     ) -> UploadOutcome:
        """Upload the video into the creator's TikTok inbox as a reviewable draft."""
        size = video_path.stat().st_size
        chunk_size, total = plan_chunks(size, self.settings.tiktok_upload_chunk_size)
        init = self._post_json("/v2/post/publish/inbox/video/init/", {
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size, "total_chunk_count": total},
        }, self.init_limiter)
        data = init.get("data", {})
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise TikTokAPIError(f"inbox init returned no upload target: {init}")
        outcome = UploadOutcome(publish_id=publish_id, mode="UPLOAD", init_response=init,
                                chunks=total, chunk_size=chunk_size)
        self._put_chunks(upload_url, video_path, size, chunk_size, total, outcome, progress)
        return outcome

    def direct_post(self, video_path: Path, post_info: dict,
                    *, progress: Optional[Callable[[int, int], None]] = None) -> UploadOutcome:
        """Direct Post flow (requires the video.publish scope and app audit)."""
        size = video_path.stat().st_size
        chunk_size, total = plan_chunks(size, self.settings.tiktok_upload_chunk_size)
        init = self._post_json("/v2/post/publish/video/init/", {
            "post_info": post_info,
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size, "total_chunk_count": total},
        }, self.init_limiter)
        data = init.get("data", {})
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise TikTokAPIError(f"direct post init returned no upload target: {init}")
        outcome = UploadOutcome(publish_id=publish_id, mode="DIRECT_POST", init_response=init,
                                chunks=total, chunk_size=chunk_size)
        self._put_chunks(upload_url, video_path, size, chunk_size, total, outcome, progress)
        return outcome

    # ------------------------------------------------------------------
    def _put_chunks(self, upload_url: str, path: Path, size: int, chunk_size: int, total: int,
                    outcome: UploadOutcome, progress: Optional[Callable[[int, int], None]]) -> None:
        mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "video/mp4")
        sent = 0
        with path.open("rb") as fh:
            for index in range(total):
                first = index * chunk_size
                length = (size - first) if index == total - 1 else chunk_size
                fh.seek(first)
                payload = fh.read(length)
                if not payload:
                    break
                last = first + len(payload) - 1
                headers = {
                    "Content-Type": mime,
                    "Content-Length": str(len(payload)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                }
                self._put_with_retry(upload_url, payload, headers, index)
                sent += len(payload)
                outcome.uploaded_bytes = sent
                if progress:
                    progress(sent, size)

    def _put_with_retry(self, url: str, payload: bytes, headers: Dict[str, str], index: int) -> None:
        attempts = 0
        while attempts <= self.settings.tiktok_max_retries:
            attempts += 1
            try:
                resp = self._http().put(url, content=payload, headers=headers, timeout=600.0)
                if resp.status_code in (200, 201, 202, 204, 206, 308):
                    return
                retryable = resp.status_code == 429 or resp.status_code >= 500
                msg = f"chunk {index} upload failed: HTTP {resp.status_code} {resp.text[:200]}"
                if not retryable:
                    raise TikTokAPIError(msg, status=resp.status_code)
                last = TikTokAPIError(msg, status=resp.status_code, retryable=True)
            except httpx.HTTPError as exc:
                last = TikTokAPIError(f"chunk {index} network error: {exc}", retryable=True)
            backoff = min(60.0, self.settings.tiktok_backoff_base_seconds * (2 ** (attempts - 1)))
            log.warning("retrying chunk %d in %.1fs: %s", index, backoff, last)
            time.sleep(backoff)
        raise last

    # ------------------------------------------------------------------
    def fetch_status(self, publish_id: str) -> dict:
        body = self._post_json("/v2/post/publish/status/fetch/", {"publish_id": publish_id},
                               self.status_limiter)
        return body.get("data", {})

    def wait_for_status(self, publish_id: str, *, terminal: Optional[set] = None,
                        sleeper: Callable[[float], None] = time.sleep) -> dict:
        """Poll until the post reaches a terminal status or the poll budget is used."""
        terminal = terminal or {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"}
        last: dict = {}
        for _ in range(self.settings.tiktok_status_poll_max):
            last = self.fetch_status(publish_id)
            status = last.get("status", "")
            if status in terminal:
                return last
            sleeper(self.settings.tiktok_status_poll_seconds)
        return last


class MockTikTokClient(TikTokClient):
    """TIKTOK_MOCK=true: exercises the full code path without any network call.

    Only for integration tests / rehearsals; the production path uses
    TikTokClient.
    """

    def __init__(self, settings: Settings, oauth: Optional[TikTokOAuth] = None) -> None:
        super().__init__(settings, oauth=oauth)
        self.calls: List[str] = []

    def _auth_headers(self) -> Dict[str, str]:  # no token needed
        return {"Authorization": "Bearer mock", "Content-Type": "application/json"}

    def query_creator_info(self) -> dict:
        self.calls.append("creator_info")
        return {"creator_username": "mock_user", "privacy_level_options":
                ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
                "max_video_post_duration_sec": 600}

    def upload_draft(self, video_path: Path, *, progress=None) -> UploadOutcome:
        self.calls.append("upload_draft")
        size = video_path.stat().st_size
        chunk_size, total = plan_chunks(size, self.settings.tiktok_upload_chunk_size)
        if progress:
            progress(size, size)
        return UploadOutcome(publish_id=f"mock.{abs(hash(str(video_path))) % 10**12}",
                             mode="UPLOAD", status="SEND_TO_USER_INBOX", chunks=total,
                             chunk_size=chunk_size, uploaded_bytes=size,
                             init_response={"mock": True})

    def direct_post(self, video_path: Path, post_info: dict, *, progress=None) -> UploadOutcome:
        self.calls.append("direct_post")
        size = video_path.stat().st_size
        chunk_size, total = plan_chunks(size, self.settings.tiktok_upload_chunk_size)
        if progress:
            progress(size, size)
        return UploadOutcome(publish_id=f"mock.{abs(hash(str(video_path))) % 10**12}",
                             mode="DIRECT_POST", status="PROCESSING_UPLOAD", chunks=total,
                             chunk_size=chunk_size, uploaded_bytes=size,
                             init_response={"mock": True, "post_info": post_info})

    def fetch_status(self, publish_id: str) -> dict:
        self.calls.append("fetch_status")
        return {"status": "SEND_TO_USER_INBOX", "publish_id": publish_id}

    def wait_for_status(self, publish_id: str, *, terminal=None, sleeper=time.sleep) -> dict:
        return self.fetch_status(publish_id)


def build_client(settings: Settings, oauth: Optional[TikTokOAuth] = None) -> TikTokClient:
    if settings.tiktok_mock:
        return MockTikTokClient(settings, oauth=oauth)
    return TikTokClient(settings, oauth=oauth)
