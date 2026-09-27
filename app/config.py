"""Application configuration (Pydantic settings, .env driven)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import List, Optional

from typing_extensions import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class QualityMode(str, Enum):
    """Video encoding quality (CRF/preset). Nothing to do with images."""

    FAST = "FAST"
    BALANCED = "BALANCED"
    HIGH_QUALITY = "HIGH_QUALITY"


class InstagramMode(str, Enum):
    """How the optional Instagram integration behaves.

    PREPARE_ONLY (default, recommended)
        No Meta API call at all. The app validates the video against the
        current Reels requirements, produces ``instagram_ready.mp4`` and
        ``caption_instagram.txt`` and stops - you upload and publish manually
        in the Instagram app, exactly like you already choose the cover there.

    API_STAGE_ONLY (optional, advanced)
        Uses the official Content Publishing API to create and fill a media
        *container* (never publishes). A container is NOT an Instagram
        mobile-app draft and expires after 24 hours, so this is not the
        default.
    """

    PREPARE_ONLY = "PREPARE_ONLY"
    API_STAGE_ONLY = "API_STAGE_ONLY"
    #: legacy value from the previous release, treated as API_STAGE_ONLY
    UPLOAD_ONLY = "UPLOAD_ONLY"


class InstagramLoginMode(str, Enum):
    """Which official Meta login configuration the app is set up for.

    INSTAGRAM_LOGIN (default) - "Instagram API with Instagram Login":
        host graph.instagram.com, no Facebook Page required, scopes
        instagram_business_basic / instagram_business_content_publish.
        Meta documents resumable local uploads as NOT available here, so a
        public HTTPS video_url is required.
    FACEBOOK_LOGIN - "Instagram API with Facebook Login":
        host graph.facebook.com (+ rupload.facebook.com), the only
        configuration for which Meta documents upload_type=resumable, i.e.
        direct upload of a local file.
    """

    INSTAGRAM_LOGIN = "INSTAGRAM_LOGIN"
    FACEBOOK_LOGIN = "FACEBOOK_LOGIN"


class InstagramUploadMethod(str, Enum):
    AUTO = "AUTO"              # resumable when supported, else video_url
    RESUMABLE = "RESUMABLE"    # local file -> rupload.facebook.com
    VIDEO_URL = "VIDEO_URL"    # public HTTPS URL you host yourself


class InstagramCoverMode(str, Enum):
    MANUAL = "MANUAL"          # the user picks the cover in Instagram


class PlatformStatus(str, Enum):
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    #: PREPARE_ONLY result: local files are ready, upload manually in Instagram
    READY_FOR_MANUAL_UPLOAD = "READY_FOR_MANUAL_UPLOAD"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AppMode(str, Enum):
    """The three explicit operating modes of the application."""

    DRY_RUN = "DRY_RUN"                # produce everything locally, upload nothing
    DRAFT_UPLOAD = "DRAFT_UPLOAD"      # official inbox/draft upload (default)
    DIRECT_POST = "DIRECT_POST"        # opt-in, requires video.publish + confirmation


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- directories ----------------
    base_dir: Path = REPO_ROOT
    input_dir: Path = REPO_ROOT / "input"
    processing_dir: Path = REPO_ROOT / "processing"
    output_dir: Path = REPO_ROOT / "output"
    failed_dir: Path = REPO_ROOT / "failed"
    archive_dir: Path = REPO_ROOT / "archive"
    logs_dir: Path = REPO_ROOT / "logs"
    state_dir: Path = REPO_ROOT / "state"

    # ---------------- watcher ----------------
    watch_recursive: bool = True
    stability_seconds: float = 4.0
    poll_interval_seconds: float = 2.0
    video_extensions: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: [".mp4", ".mov", ".mkv", ".webm"])
    metadata_extensions: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: [".txt", ".md"])
    max_parallel_jobs: int = 1
    #: copy the source video into output/<name>/source.<ext> (original untouched)
    copy_source_to_output: bool = True

    # ---------------- modes ----------------
    #: explicit mode selector. DRAFT_UPLOAD is the default production mode;
    #: DRY_RUN=true (also the default) still wins over it as a safety net.
    app_mode: AppMode = AppMode.DRAFT_UPLOAD
    dry_run: bool = True
    tiktok_mock: bool = False
    quality_mode: QualityMode = QualityMode.BALANCED
    default_language: str = "de"

    # ---------------- video ----------------
    ffmpeg_path: Optional[str] = None
    ffprobe_path: Optional[str] = None
    video_crf: int = 20
    video_preset: str = "medium"
    audio_bitrate: str = "192k"
    target_width: int = 1080
    target_height: int = 1920
    #: only re-encode when the source is not already TikTok compatible.
    #: "auto" (default) = passthrough/remux when possible, transcode otherwise;
    #: "always" = always re-encode; "never" = never re-encode (copy/remux only).
    video_normalization: str = "auto"
    #: convert non-9:16 sources to the target frame (blurred pad / crop).
    #: When false, the original aspect ratio is kept untouched.
    enforce_vertical: bool = True

    # ---------------- tiktok ----------------
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    tiktok_redirect_uri: str = "http://localhost:8765/tiktok/callback"
    tiktok_scopes: str = "user.info.basic,video.upload"
    tiktok_api_base: str = "https://open.tiktokapis.com"
    tiktok_auth_base: str = "https://www.tiktok.com"
    tiktok_post_mode: str = "UPLOAD"  # UPLOAD (inbox draft) or DIRECT_POST
    #: DIRECT_POST additionally requires this explicit confirmation, so the app
    #: can never drift into posting on its own.
    confirm_direct_post: bool = False
    tiktok_upload_chunk_size: int = 64 * 1024 * 1024
    tiktok_max_retries: int = 5
    tiktok_backoff_base_seconds: float = 2.0
    tiktok_status_poll_seconds: float = 4.0
    tiktok_status_poll_max: int = 30
    content_is_aigc: bool = False
    caption_max_chars: int = 2200

    # ---------------- instagram (optional, OFF by default) ----------------
    instagram_enabled: bool = False
    instagram_mode: InstagramMode = InstagramMode.PREPARE_ONLY
    #: hard safety switch - automatic publishing is refused by design
    instagram_auto_publish: bool = False
    instagram_login_mode: InstagramLoginMode = InstagramLoginMode.INSTAGRAM_LOGIN
    instagram_upload_method: InstagramUploadMethod = InstagramUploadMethod.AUTO
    instagram_cover_mode: InstagramCoverMode = InstagramCoverMode.MANUAL
    instagram_client_id: Optional[str] = None
    instagram_client_secret: Optional[str] = None
    instagram_redirect_uri: str = "http://localhost:8765/instagram/callback"
    instagram_scopes: str = "instagram_business_basic,instagram_business_content_publish"
    instagram_api_version: str = "v23.0"
    instagram_graph_base: str = "https://graph.instagram.com"
    instagram_facebook_graph_base: str = "https://graph.facebook.com"
    instagram_rupload_base: str = "https://rupload.facebook.com"
    instagram_auth_base: str = "https://www.instagram.com"
    instagram_token_base: str = "https://api.instagram.com"
    #: public HTTPS URL of the final MP4 (only needed for the VIDEO_URL method).
    #: {filename} and {name} are substituted, e.g. https://cdn.me/{filename}
    instagram_public_video_url_template: str = ""
    instagram_share_to_feed: bool = True
    instagram_status_poll_seconds: float = 10.0
    instagram_status_poll_max: int = 60
    instagram_max_retries: int = 4
    instagram_backoff_base_seconds: float = 2.0
    #: Meta documents media containers as expiring 24 hours after creation
    instagram_container_ttl_hours: float = 24.0

    # ---------------- dashboard ----------------
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8765

    # ---------------- logging ----------------
    log_level: str = "INFO"
    log_json_file: str = "app.jsonl"

    @field_validator("video_extensions", "metadata_extensions", mode="before")
    @classmethod
    def _split_ext(cls, v):
        if isinstance(v, str):
            return [e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
                    for e in v.split(",") if e.strip()]
        return v

    @property
    def db_path(self) -> Path:
        return self.state_dir / "jobs.sqlite3"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "tiktok_tokens.json"

    # ------------------------------------------------------------------
    @property
    def instagram_effective_mode(self) -> InstagramMode:
        """PREPARE_ONLY unless the user explicitly opted into API staging."""
        if self.instagram_mode == InstagramMode.UPLOAD_ONLY:      # legacy alias
            return InstagramMode.API_STAGE_ONLY
        return self.instagram_mode

    @property
    def instagram_api_staging(self) -> bool:
        """True only when the optional Meta API staging mode is selected."""
        return (self.instagram_enabled
                and self.instagram_effective_mode == InstagramMode.API_STAGE_ONLY)

    @property
    def effective_mode(self) -> AppMode:
        """The mode the application will actually run in.

        Precedence (safety first):
          1. DRY_RUN=true or APP_MODE=DRY_RUN  -> DRY_RUN (never uploads)
          2. DIRECT_POST only when it is requested *and* explicitly confirmed
             with CONFIRM_DIRECT_POST=true and the video.publish scope
          3. otherwise DRAFT_UPLOAD
        """
        if self.dry_run or self.app_mode == AppMode.DRY_RUN:
            return AppMode.DRY_RUN
        wants_direct = (self.app_mode == AppMode.DIRECT_POST
                        or self.tiktok_post_mode.strip().upper() == "DIRECT_POST")
        if wants_direct and self.confirm_direct_post and "video.publish" in self.tiktok_scopes:
            return AppMode.DIRECT_POST
        return AppMode.DRAFT_UPLOAD

    @property
    def mode_warnings(self) -> List[str]:
        """Human readable notes about why a requested mode was not applied."""
        notes: List[str] = []
        wants_direct = (self.app_mode == AppMode.DIRECT_POST
                        or self.tiktok_post_mode.strip().upper() == "DIRECT_POST")
        if wants_direct and self.effective_mode != AppMode.DIRECT_POST:
            if self.dry_run or self.app_mode == AppMode.DRY_RUN:
                notes.append("DIRECT_POST requested but DRY_RUN is active - nothing is uploaded.")
            else:
                if not self.confirm_direct_post:
                    notes.append("DIRECT_POST requested but CONFIRM_DIRECT_POST=false "
                                 "- falling back to the reviewable DRAFT_UPLOAD flow.")
                if "video.publish" not in self.tiktok_scopes:
                    notes.append("DIRECT_POST requires the video.publish scope in TIKTOK_SCOPES.")
        return notes

    def ensure_dirs(self) -> None:
        for d in (self.input_dir, self.processing_dir, self.output_dir, self.failed_dir,
                  self.archive_dir, self.logs_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    """Used by tests to inject an isolated configuration."""
    global _settings
    _settings = s
