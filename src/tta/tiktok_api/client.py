"""Official TikTok Content Posting API client.

Endpoints (see https://developers.tiktok.com/doc/content-posting-api-reference-upload-video):

* POST /v2/post/publish/inbox/video/init/     - upload for in-app review (draft)
* POST /v2/post/publish/video/init/           - direct post (audited apps)
* POST /v2/post/publish/creator_info/query/   - creator info (direct post)
* POST /v2/post/publish/status/fetch/         - publish status
* PUT  {upload_url}                           - chunked binary upload

Features: automatic token refresh, retry with exponential backoff,
Retry-After-aware rate-limit handling, chunked uploads following the
documented 5 MB..64 MB chunk rules.  Tokens never appear in logs.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .oauth import refresh_access_token
from .tokens import TokenStore

log = logging.getLogger("tta.tiktok")

BASE = "https://open.tiktokapis.com"
INBOX_INIT_URL = f"{BASE}/v2/post/publish/inbox/video/init/"
DIRECT_INIT_URL = f"{BASE}/v2/post/publish/video/init/"
CREATOR_INFO_URL = f"{BASE}/v2/post/publish/creator_info/query/"
STATUS_FETCH_URL = f"{BASE}/v2/post/publish/status/fetch/"

MIN_CHUNK = 5 * 1024 * 1024        # 5 MB
MAX_CHUNK = 64 * 1024 * 1024       # 64 MB
SINGLE_CHUNK_LIMIT = 64 * 1024 * 1024

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TikTokAPIError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0,
                 retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def plan_chunks(video_size: int) -> tuple[int, int, list[tuple[int, int]]]:
    """Return (chunk_size, total_chunk_count, [(start, end_inclusive), ...]).

    Follows the documented rules:
    * whole file as a single chunk when it is <= 64 MB
    * otherwise fixed chunk_size (10 MB), total_chunk_count = floor(size/chunk),
      the final chunk absorbs the remainder (may exceed chunk_size).
    """
    if video_size <= 0:
        raise ValueError("video_size must be > 0")
    if video_size <= SINGLE_CHUNK_LIMIT:
        return video_size, 1, [(0, video_size - 1)]
    chunk_size = 10 * 1024 * 1024
    total = video_size // chunk_size
    ranges = []
    for i in range(total):
        start = i * chunk_size
        end = video_size - 1 if i == total - 1 else start + chunk_size - 1
        ranges.append((start, end))
    return chunk_size, total, ranges


class TikTokClient:
    def __init__(self, client_key: str, client_secret: str, token_store: TokenStore,
                 session: requests.Session | None = None,
                 max_retries: int = 3, backoff_base: float = 2.0,
                 sleep=time.sleep):
        self.client_key = client_key
        self.client_secret = client_secret
        self.tokens = token_store
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    # ------------------------------------------------------------- auth
    def ensure_access_token(self) -> str:
        if self.tokens.is_access_valid():
            return self.tokens.access_token
        refresh = self.tokens.refresh_token
        if not refresh:
            raise TikTokAPIError(
                "not authenticated - run `tta auth login` first", code="no_token"
            )
        log.info("refreshing TikTok access token")
        payload = refresh_access_token(self.client_key, self.client_secret, refresh,
                                       session=self.session)
        self.tokens.save(payload)
        return payload["access_token"]

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.ensure_access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    # ------------------------------------------------------------- http
    def _post(self, url: str, payload: dict) -> dict:
        last_error: TikTokAPIError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload,
                                         headers=self._headers(), timeout=60)
            except requests.RequestException as exc:
                last_error = TikTokAPIError(f"network error: {exc}", retryable=True)
                self._backoff(attempt, None)
                continue

            if resp.status_code == 401:
                # token might have been revoked/expired mid-flight: force refresh once
                log.info("HTTP 401 - forcing token refresh")
                data = self.tokens.load()
                data.pop("access_token", None)
                data["expires_at"] = 0
                self.tokens.save(data)
                last_error = TikTokAPIError("unauthorized", status=401, retryable=True)
                continue

            if resp.status_code in RETRYABLE_STATUS:
                retry_after = _retry_after_seconds(resp)
                last_error = TikTokAPIError(
                    f"HTTP {resp.status_code} from TikTok", status=resp.status_code,
                    retryable=True,
                )
                if attempt < self.max_retries:
                    log.warning("TikTok HTTP %s, retrying (attempt %d)",
                                resp.status_code, attempt + 1)
                    self._backoff(attempt, retry_after)
                    continue
                break

            try:
                body = resp.json()
            except ValueError:
                raise TikTokAPIError(
                    f"non-JSON response (HTTP {resp.status_code})",
                    status=resp.status_code,
                )
            error = body.get("error") or {}
            code = error.get("code", "ok")
            if code not in ("ok", "", None):
                retryable = code in ("rate_limit_exceeded", "internal_error")
                err = TikTokAPIError(
                    f"TikTok API error '{code}': {error.get('message', '')}",
                    code=code, status=resp.status_code, retryable=retryable,
                )
                if retryable and attempt < self.max_retries:
                    last_error = err
                    self._backoff(attempt, _retry_after_seconds(resp))
                    continue
                raise err
            return body.get("data", {})
        raise last_error or TikTokAPIError("request failed")

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after else self.backoff_base * (2 ** attempt)
        self._sleep(min(delay, 120))

    # -------------------------------------------------------------- api
    def creator_info(self) -> dict:
        return self._post(CREATOR_INFO_URL, {})

    def init_inbox_upload(self, video_size: int) -> dict:
        chunk_size, total, _ = plan_chunks(video_size)
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total,
            }
        }
        return self._post(INBOX_INIT_URL, payload)

    def init_direct_post(self, video_size: int, title: str,
                         privacy_level: str = "SELF_ONLY",
                         cover_timestamp_ms: int = 0,
                         disable_duet: bool = False,
                         disable_comment: bool = False,
                         disable_stitch: bool = False,
                         is_aigc: bool = False) -> dict:
        chunk_size, total, _ = plan_chunks(video_size)
        payload = {
            "post_info": {
                "title": title[:2200],
                "privacy_level": privacy_level,
                "disable_duet": disable_duet,
                "disable_comment": disable_comment,
                "disable_stitch": disable_stitch,
                "video_cover_timestamp_ms": cover_timestamp_ms,
                "is_aigc": is_aigc,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total,
            },
        }
        return self._post(DIRECT_INIT_URL, payload)

    def upload_file(self, upload_url: str, video_path: str | Path,
                    mime_type: str = "video/mp4") -> None:
        video_path = Path(video_path)
        size = video_path.stat().st_size
        _, _, ranges = plan_chunks(size)
        with open(video_path, "rb") as fh:
            for start, end in ranges:
                fh.seek(start)
                blob = fh.read(end - start + 1)
                self._put_chunk(upload_url, blob, start, end, size, mime_type)

    def _put_chunk(self, upload_url: str, blob: bytes, start: int, end: int,
                   total: int, mime_type: str) -> None:
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(len(blob)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        }
        last_error: TikTokAPIError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.put(upload_url, data=blob, headers=headers,
                                        timeout=600)
            except requests.RequestException as exc:
                last_error = TikTokAPIError(f"upload network error: {exc}",
                                            retryable=True)
                self._backoff(attempt, None)
                continue
            if resp.status_code in (200, 201, 206):
                log.info("uploaded bytes %d-%d/%d", start, end, total)
                return
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                last_error = TikTokAPIError(
                    f"upload HTTP {resp.status_code}", status=resp.status_code,
                    retryable=True,
                )
                self._backoff(attempt, _retry_after_seconds(resp))
                continue
            raise TikTokAPIError(
                f"chunk upload failed: HTTP {resp.status_code} {resp.text[:200]}",
                status=resp.status_code,
            )
        raise last_error or TikTokAPIError("chunk upload failed")

    def fetch_status(self, publish_id: str) -> dict:
        return self._post(STATUS_FETCH_URL, {"publish_id": publish_id})

    def wait_for_upload(self, publish_id: str, timeout: float = 600.0,
                        poll_interval: float = 5.0) -> dict:
        """Poll status until a terminal status or timeout."""
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.fetch_status(publish_id)
            status = last.get("status", "")
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                return last
            if status == "FAILED":
                raise TikTokAPIError(
                    f"TikTok processing failed: {last.get('fail_reason', 'unknown')}",
                    code=str(last.get("fail_reason", "")),
                )
            self._sleep(poll_interval)
        return last


def _retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
