"""Application configuration (Pydantic settings, .env driven)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import List, Optional

from typing_extensions import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class QualityMode(str, Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    HIGH_QUALITY = "HIGH_QUALITY"


class StylePreset(str, Enum):
    CINEMATIC_MYSTICAL = "CINEMATIC_MYSTICAL"
    DARK_LUXURY = "DARK_LUXURY"
    CLEAN_MODERN = "CLEAN_MODERN"
    AUTO = "AUTO"


class ProviderCost(str, Enum):
    FREE = "FREE"
    FREE_WITH_QUOTA = "FREE WITH QUOTA"
    PAID = "PAID"
    LOCAL = "LOCAL"


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
    covers_dir: Path = REPO_ROOT / "covers"
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
    dry_run: bool = True
    tiktok_mock: bool = False
    allow_paid_api: bool = False
    quality_mode: QualityMode = QualityMode.BALANCED
    style_preset: StylePreset = StylePreset.AUTO
    default_language: str = "de"

    # ---------------- image providers ----------------
    image_provider: str = "auto"  # auto | explicit provider name
    image_provider_primary: str = "pollinations"
    image_provider_fallback: str = "comfyui,automatic1111,offline"
    image_timeout_seconds: float = 180.0
    image_max_retries: int = 2
    image_backoff_base_seconds: float = 2.0
    image_candidates: int = 1  # HIGH_QUALITY may raise this
    allow_offline_fallback: bool = True

    pollinations_base_url: str = "https://image.pollinations.ai"
    pollinations_model: str = "flux"
    pollinations_token: Optional[str] = None  # optional free registered token

    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_workflow_file: Optional[Path] = None
    comfyui_checkpoint: str = "sd_xl_base_1.0.safetensors"

    automatic1111_base_url: str = "http://127.0.0.1:7860"
    automatic1111_sampler: str = "DPM++ 2M Karras"
    automatic1111_steps: int = 30

    huggingface_api_token: Optional[str] = None
    huggingface_model: str = "black-forest-labs/FLUX.1-schnell"
    huggingface_base_url: str = "https://router.huggingface.co"

    # ---------------- cover ----------------
    cover_width: int = 1080
    cover_height: int = 1920
    cover_format: str = "PNG"
    font_regular: Optional[Path] = None
    font_bold: Optional[Path] = None

    # ---------------- video ----------------
    ffmpeg_path: Optional[str] = None
    ffprobe_path: Optional[str] = None
    video_crf: int = 20
    video_preset: str = "medium"
    audio_bitrate: str = "192k"
    cover_frame_hold_ms: int = 120
    target_width: int = 1080
    target_height: int = 1920

    # ---------------- tiktok ----------------
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    tiktok_redirect_uri: str = "http://localhost:8765/tiktok/callback"
    tiktok_scopes: str = "user.info.basic,video.upload"
    tiktok_api_base: str = "https://open.tiktokapis.com"
    tiktok_auth_base: str = "https://www.tiktok.com"
    tiktok_post_mode: str = "UPLOAD"  # UPLOAD (inbox draft) or DIRECT_POST
    tiktok_upload_chunk_size: int = 64 * 1024 * 1024
    tiktok_max_retries: int = 5
    tiktok_backoff_base_seconds: float = 2.0
    tiktok_status_poll_seconds: float = 4.0
    tiktok_status_poll_max: int = 30
    content_is_aigc: bool = False
    caption_max_chars: int = 2200

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

    @property
    def fallback_providers(self) -> List[str]:
        return [p.strip() for p in self.image_provider_fallback.split(",") if p.strip()]

    def ensure_dirs(self) -> None:
        for d in (self.input_dir, self.processing_dir, self.output_dir, self.failed_dir,
                  self.archive_dir, self.covers_dir, self.logs_dir, self.state_dir):
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
