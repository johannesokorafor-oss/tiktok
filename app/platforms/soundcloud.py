"""SoundCloud - audio platform. Upload only when private sharing is verified.

Verified against the current official API Guide (developers.soundcloud.com/docs,
checked 2026-09-27):

* Base URL ``https://api.soundcloud.com``; auth host ``https://secure.soundcloud.com``
* OAuth 2.1, **PKCE required** for the authorization code flow
  - authorize: ``https://secure.soundcloud.com/authorize``
  - token:     ``POST https://secure.soundcloud.com/oauth/token``
    (``grant_type=authorization_code``, ``client_id``, ``client_secret``,
    ``redirect_uri``, ``code_verifier``, ``code``)
  - refresh:   same endpoint with ``grant_type=refresh_token``;
    access tokens last ~1 hour and **refresh tokens are single use**
* Requests use ``Authorization: OAuth <ACCESS_TOKEN>``
* Track upload: ``POST /tracks`` as ``multipart/form-data`` with
  ``track[title]``, ``track[artist]``, ``track[asset_data]``

**Honesty gate:** the current API Guide's upload example documents only those
three fields. It does not document a private-sharing field for the upload
call. This application therefore does **not** guess: unless you set
``SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=true`` (after checking the current OpenAPI
spec yourself), the platform is automatically downgraded to PREPARE_ONLY and
reports:

    "SoundCloud private upload is not currently verified against the current
     API contract."

The video is never uploaded as-is: audio is extracted to a lossless FLAC
(default) first. No artwork is generated - you set the artwork in SoundCloud.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import PlatformMode, PlatformStatus, Settings
from app.platforms.base import (PlatformAuthError, PlatformDiagnosis, PlatformError,
                                PlatformProvider, PlatformResult)
from app.platforms.validate import validate_soundcloud_audio
from app.video.ffmpeg import FFmpegError, find_ffmpeg, run

log = logging.getLogger(__name__)

UNVERIFIED_NOTE = ("SoundCloud private upload is not currently verified against the current API "
                   "contract. Set SOUNDCLOUD_PRIVATE_FIELD_VERIFIED=true only after confirming "
                   "the sharing field in the current OpenAPI spec.")

#: Re-verification 2026-09-27 against the CURRENT OpenAPI specification
#: (github.com/soundcloud/api -> openapi/api.yaml, schema `TrackDataRequest`):
#: the multipart upload body documents `track[sharing]` as a writable enum
#: ["public", "private"] with default "public". Private upload is therefore
#: officially supported and enabled by default; the response is still re-read
#: and a public result fails the job.
VERIFIED_NOTE = ("track[sharing] is documented as a writable enum "
                 "['public','private'] in the current SoundCloud OpenAPI specification "
                 "(TrackDataRequest), verified 2026-09-27.")


@dataclass
class SoundCloudTokens:
    access_token: str = ""
    refresh_token: str = ""
    scope: str = ""
    expires_at: float = 0.0

    def is_expired(self, skew: float = 120.0) -> bool:
        return (not self.access_token) or (self.expires_at
                                           and time.time() >= self.expires_at - skew)

    def public_dict(self) -> dict:
        return {"authenticated": bool(self.access_token),
                "expires_in_seconds": max(0, int(self.expires_at - time.time()))
                if self.expires_at else 0,
                "refresh_token_present": bool(self.refresh_token), "scope": self.scope}


def extract_audio(video: Path, dest: Path, settings: Settings) -> Path:
    """Extract the audio track losslessly (FLAC by default). No re-framing."""
    fmt = (settings.soundcloud_audio_format or "flac").lower()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(settings)
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-map", "0:a:0"]
    if fmt == "flac":
        cmd += ["-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s16"]
    elif fmt == "wav":
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", "320k"]
    cmd += [str(dest)]
    run(cmd, timeout=3600)
    return dest


class SoundCloudProvider(PlatformProvider):
    name = "soundcloud"
    supports_api_upload = True
    api_summary = ("Official SoundCloud API (OAuth 2.1 + PKCE). Audio is extracted from the "
                   "video first. Private upload is only performed when the sharing field has "
                   "been verified against the current API contract; otherwise PREPARE_ONLY.")

    # ---------------------------------------------------------------- config
    @property
    def private_verified(self) -> bool:
        return bool(self.settings.soundcloud_private_field_verified)

    @property
    def effective_mode(self) -> PlatformMode:
        """Downgrade to PREPARE_ONLY unless private upload is verified."""
        if self.mode == PlatformMode.UPLOAD_PRIVATE and not self.private_verified:
            return PlatformMode.PREPARE_ONLY
        return self.mode

    def token_path(self) -> Path:
        return self.settings.state_dir / "soundcloud_tokens.json"

    def load_tokens(self) -> SoundCloudTokens:
        path = self.token_path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                base = SoundCloudTokens()
                return SoundCloudTokens(**{k: data.get(k, getattr(base, k))
                                           for k in base.__dict__})
            except (json.JSONDecodeError, TypeError):
                pass
        import os
        env = os.getenv("SOUNDCLOUD_ACCESS_TOKEN", "").strip()
        if env:
            return SoundCloudTokens(access_token=env,
                                    refresh_token=os.getenv("SOUNDCLOUD_REFRESH_TOKEN", ""),
                                    expires_at=time.time() + 3600)
        return SoundCloudTokens()

    def save_tokens(self, tokens: SoundCloudTokens) -> None:
        path = self.token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
        try:
            import os
            import stat
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def disconnect(self) -> None:
        path = self.token_path()
        if path.exists():
            path.unlink()

    def is_configured(self) -> bool:
        return bool(self.settings.soundcloud_client_id and self.settings.soundcloud_client_secret)

    # ---------------------------------------------------------------- oauth
    @staticmethod
    def pkce_pair() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return verifier, challenge

    def authorize_url(self, state: str, code_challenge: str) -> str:
        params = {"client_id": self.settings.soundcloud_client_id or "",
                  "redirect_uri": self.settings.soundcloud_redirect_uri,
                  "response_type": "code",
                  "code_challenge": code_challenge,
                  "code_challenge_method": "S256",
                  "state": state}
        return (self.settings.soundcloud_auth_base.rstrip("/") + "/authorize?"
                + urllib.parse.urlencode(params))

    def exchange_code(self, code: str, code_verifier: str) -> SoundCloudTokens:
        data = {"grant_type": "authorization_code",
                "client_id": self.settings.soundcloud_client_id or "",
                "client_secret": self.settings.soundcloud_client_secret or "",
                "redirect_uri": self.settings.soundcloud_redirect_uri,
                "code_verifier": code_verifier,
                "code": code}
        body = self._token_request(data)
        tokens = SoundCloudTokens(access_token=body.get("access_token", ""),
                                  refresh_token=body.get("refresh_token", ""),
                                  scope=body.get("scope", ""),
                                  expires_at=time.time() + float(body.get("expires_in", 3600)))
        self.save_tokens(tokens)
        return tokens

    def refresh(self, tokens: Optional[SoundCloudTokens] = None) -> SoundCloudTokens:
        tokens = tokens or self.load_tokens()
        if not tokens.refresh_token:
            raise PlatformAuthError("no SoundCloud refresh token stored - reconnect the account")
        body = self._token_request({"grant_type": "refresh_token",
                                    "client_id": self.settings.soundcloud_client_id or "",
                                    "client_secret": self.settings.soundcloud_client_secret or "",
                                    "refresh_token": tokens.refresh_token})
        # refresh tokens are single use: always store the new one
        new = SoundCloudTokens(access_token=body.get("access_token", ""),
                               refresh_token=body.get("refresh_token", "") or "",
                               scope=body.get("scope", tokens.scope),
                               expires_at=time.time() + float(body.get("expires_in", 3600)))
        if not new.access_token:
            raise PlatformAuthError("SoundCloud refresh returned no access token")
        self.save_tokens(new)
        return new

    def _token_request(self, data: Dict[str, str]) -> dict:
        url = self.settings.soundcloud_auth_base.rstrip("/") + "/oauth/token"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, data=data,
                                   headers={"accept": "application/json; charset=utf-8"})
        except httpx.HTTPError as exc:
            raise PlatformError(f"soundcloud token request failed: {exc}", retryable=True)
        if resp.status_code >= 400:
            raise PlatformAuthError(
                f"SoundCloud token request failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def authenticate(self) -> bool:
        tokens = self.load_tokens()
        if not tokens.access_token and not tokens.refresh_token:
            raise PlatformAuthError(
                "SoundCloud is not connected - run the OAuth flow (Connect SoundCloud).")
        if tokens.is_expired():
            tokens = self.refresh(tokens)
        return bool(tokens.access_token)

    def _auth_header(self) -> Dict[str, str]:
        tokens = self.load_tokens()
        if tokens.is_expired():
            tokens = self.refresh(tokens)
        return {"Authorization": f"OAuth {tokens.access_token}"}

    # ---------------------------------------------------------------- http
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = 0
        last: Optional[PlatformError] = None
        while attempts <= self.settings.soundcloud_max_retries:
            attempts += 1
            headers = {**self._auth_header(), "accept": "application/json; charset=utf-8",
                       **kwargs.pop("headers", {})}
            try:
                with httpx.Client(timeout=kwargs.pop("timeout", 120.0)) as client:
                    resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last = PlatformError(f"soundcloud network error: {exc}", retryable=True)
            else:
                if resp.status_code == 401:
                    raise PlatformAuthError("SoundCloud rejected the access token (HTTP 401)")
                if resp.status_code == 403:
                    raise PlatformAuthError(
                        f"SoundCloud denied the request (HTTP 403): {resp.text[:200]}")
                if resp.status_code < 400:
                    return resp
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = PlatformError(f"soundcloud HTTP {resp.status_code}: {resp.text[:300]}",
                                     retryable=retryable, status=resp.status_code)
                if not retryable:
                    raise last
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                    continue
            backoff = min(60.0, self.settings.soundcloud_backoff_base_seconds
                          * (2 ** (attempts - 1)))
            log.warning("retrying soundcloud %s in %.1fs: %s", method, backoff, last)
            time.sleep(backoff)
        raise last or PlatformError("soundcloud request failed")

    def get_account(self) -> dict:
        return self._request("GET", f"{self.settings.soundcloud_api_base}/me").json()

    # ---------------------------------------------------------------- base API
    def validate(self, audio: Path) -> Dict[str, Any]:
        return validate_soundcloud_audio(audio, self.settings).as_dict()

    def _audio_path(self, out_dir: Path) -> Path:
        ext = {"flac": ".flac", "wav": ".wav"}.get(
            (self.settings.soundcloud_audio_format or "flac").lower(), ".mp3")
        return out_dir / f"soundcloud_ready{ext}"

    def _caption(self, plan) -> str:
        title = (getattr(plan, "tiktok_title", "") or "").strip()
        caption = (getattr(plan, "caption", "") or "").strip()
        tags = " ".join(f"#{t.lstrip('#')}" for t in (getattr(plan, "hashtags", []) or []))
        parts = [p for p in (title, caption, tags) if p]
        return "\n\n".join(parts)

    def _build_audio(self, video: Path, out_dir: Path) -> tuple[Path, Dict[str, Any]]:
        """Reuse the shared audio_master.flac instead of extracting twice."""
        from app.platforms.audio import build_audio_master
        out_dir.mkdir(parents=True, exist_ok=True)
        audio = self._audio_path(out_dir)
        fmt = (self.settings.soundcloud_audio_format or "flac").lower()
        if fmt == "flac":
            master = build_audio_master(video, out_dir, self.settings)
            if audio.resolve() != master.path.resolve():
                audio.write_bytes(master.path.read_bytes())
        else:
            extract_audio(video, audio, self.settings)
        return audio, self.validate(audio)

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        """Extract audio + caption for a manual SoundCloud upload. No API call."""
        result = PlatformResult("soundcloud", PlatformMode.PREPARE_ONLY,
                                PlatformStatus.READY_FOR_MANUAL_UPLOAD)
        try:
            audio, check = self._build_audio(video, out_dir)
        except FFmpegError as exc:
            result.status = PlatformStatus.FAILED
            result.error = f"audio extraction failed: {exc}"
            return result
        result.validation = check
        if not check["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(check["errors"])
            return result

        caption = self._caption(plan)
        caption_path = out_dir / "caption_soundcloud.txt"
        caption_path.write_text(caption, encoding="utf-8")
        payload = {"platform": "soundcloud", "mode": PlatformMode.PREPARE_ONLY.value,
                   "status": PlatformStatus.READY_FOR_MANUAL_UPLOAD.value,
                   "audio_path": str(audio), "caption_path": str(caption_path),
                   "title": getattr(plan, "tiktok_title", ""), "validation": check,
                   "artwork": "set manually in SoundCloud - never generated here",
                   "auto_publish": False, "api_calls_made": 0,
                   "reason": (UNVERIFIED_NOTE if self.mode == PlatformMode.UPLOAD_PRIVATE
                              and not self.private_verified else
                              "SOUNDCLOUD_MODE=PREPARE_ONLY")}
        json_path = out_dir / "soundcloud.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        result.artifacts = [str(audio), str(caption_path), str(json_path)]
        result.message = payload["reason"]
        result.metadata = payload
        return result

    def upload(self, video: Path, plan, out_dir: Path,
               on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        def emit(event: str, message: str = "", **data: Any) -> None:
            if on_event:
                on_event(event, message, data)

        if not self.private_verified:
            # never guess the sharing field
            emit("SOUNDCLOUD_PRIVATE_UNVERIFIED", UNVERIFIED_NOTE)
            result = self.prepare(video, plan, out_dir)
            result.message = UNVERIFIED_NOTE
            return result

        result = PlatformResult("soundcloud", PlatformMode.UPLOAD_PRIVATE,
                                PlatformStatus.UPLOADING)
        try:
            audio, check = self._build_audio(video, out_dir)
            result.validation = check
            if not check["ok"]:
                result.status = PlatformStatus.FAILED
                result.error = "; ".join(check["errors"])
                return result
            self.authenticate()
            caption = self._caption(plan)
            (out_dir / "caption_soundcloud.txt").write_text(caption, encoding="utf-8")
            emit("SOUNDCLOUD_UPLOAD_STARTED", audio.name)

            data = {
                "track[title]": (getattr(plan, "tiktok_title", "") or audio.stem)[:100],
                "track[description]": caption[:4000],
                self.settings.soundcloud_private_field: self.settings.soundcloud_private_value,
            }
            with audio.open("rb") as fh:
                files = {"track[asset_data]": (audio.name, fh, "application/octet-stream")}
                resp = self._request("POST", f"{self.settings.soundcloud_api_base}/tracks",
                                     data=data, files=files, timeout=3600.0)
            body = resp.json()
            result.external_id = str(body.get("id", "") or body.get("urn", ""))
            result.url = body.get("permalink_url")
            sharing = str(body.get("sharing", "")).lower()
            public = bool(body.get("public")) or sharing == "public"
            result.metadata = {"sharing": sharing, "track": {k: body.get(k) for k in
                                                             ("id", "urn", "title", "sharing",
                                                              "permalink_url", "state")}}
            if public:
                result.status = PlatformStatus.FAILED
                result.error = ("SoundCloud reports the track as public - the configured private "
                                "field did not take effect. Verify it against the current API "
                                "contract and delete the track in SoundCloud.")
                emit("SOUNDCLOUD_PRIVACY_WARNING", result.error)
                return result
            result.status = PlatformStatus.UPLOADED_PRIVATE
            result.message = f"Track uploaded privately (sharing={sharing or 'private'})."
            result.artifacts = [str(audio), str(out_dir / "caption_soundcloud.txt")]
            path = out_dir / "soundcloud.json"
            path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            result.artifacts.append(str(path))
            emit("SOUNDCLOUD_UPLOADED_PRIVATE", str(result.external_id))
        except PlatformAuthError as exc:
            result.status = PlatformStatus.AUTH_REQUIRED
            result.error = str(exc)
            emit("SOUNDCLOUD_FAILED", str(exc))
        except (PlatformError, FFmpegError) as exc:
            result.status = PlatformStatus.FAILED
            result.error = str(exc)
            emit("SOUNDCLOUD_FAILED", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected SoundCloud failure")
            result.status = PlatformStatus.FAILED
            result.error = f"unexpected {exc.__class__.__name__}: {exc}"
            emit("SOUNDCLOUD_FAILED", result.error)
        finally:
            result.completed_at = time.time()
        return result

    # ---------------------------------------------------------------- diag
    def diagnose(self) -> PlatformDiagnosis:
        diag = PlatformDiagnosis(
            platform="soundcloud", enabled=self.enabled, mode=self.effective_mode,
            configured=self.is_configured(), authenticated=False,
            capability=(f"Official API upload with private sharing ({VERIFIED_NOTE})"
                        if self.private_verified else
                        "PREPARE_ONLY - private upload not verified against the current API"),
            required_access="Approved SoundCloud app (client id/secret) + OAuth 2.1 PKCE token")
        if not self.enabled:
            diag.detail = "disabled (SOUNDCLOUD_ENABLED=false)"
            return diag
        if not self.is_configured():
            diag.detail = "missing SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET"
            if not self.private_verified:
                diag.detail += f" | {UNVERIFIED_NOTE}"
            return diag
        tokens = self.load_tokens()
        diag.authenticated = bool(tokens.access_token)
        diag.detail = (f"token stored: {bool(tokens.access_token)}; "
                       f"expires in {max(0, int(tokens.expires_at - time.time()))}s; "
                       f"audio format={self.settings.soundcloud_audio_format}")
        if not self.private_verified:
            diag.detail += f" | {UNVERIFIED_NOTE}"
        return diag
