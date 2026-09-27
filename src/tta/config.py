"""Central configuration.

All settings come from environment variables (optionally loaded from a `.env`
file at the repository root or at ``TTA_HOME``).  Secrets are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

def _find_repo_root() -> Path:
    """Repo root for the documented editable install (<repo>/src/tta).

    If the package was installed non-editable (site-packages), fall back
    to the current working directory; TTA_HOME always overrides either.
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() or (candidate / ".env").is_file():
        return candidate
    return Path.cwd()


REPO_ROOT = _find_repo_root()


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")
TEXT_EXTENSIONS = (".txt", ".md")


@dataclass
class Config:
    # Directories
    home: Path = REPO_ROOT
    input_dir: Path = field(init=False)
    processing_dir: Path = field(init=False)
    failed_dir: Path = field(init=False)
    archive_dir: Path = field(init=False)
    covers_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    output_dir: Path = field(init=False)
    data_dir: Path = field(init=False)
    db_path: Path = field(init=False)
    token_file: Path = field(init=False)

    # General
    dry_run: bool = True

    # Watcher
    stabilize_seconds: float = 3.0
    scan_interval: float = 2.0
    pair_timeout: float = 600.0
    max_retries: int = 2

    # Image generation
    image_provider: str = "auto"
    allow_paid_api: bool = False
    image_fallback_to_local: bool = True
    comfyui_url: str = ""
    comfyui_workflow: str = ""
    openai_api_key: str = ""

    # Cover
    cover_duration_ms: int = 500
    cover_width: int = 1080
    cover_height: int = 1920

    # Video
    ffmpeg_path: str = ""
    ffprobe_path: str = ""

    # TikTok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://127.0.0.1:8765/callback/"
    upload_mode: str = "inbox"  # inbox | direct
    direct_post_privacy: str = "SELF_ONLY"

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    def __post_init__(self) -> None:
        self.home = Path(self.home)
        self.input_dir = self.home / "input"
        self.processing_dir = self.home / "processing"
        self.failed_dir = self.home / "failed"
        self.archive_dir = self.home / "archive"
        self.covers_dir = self.home / "covers"
        self.logs_dir = self.home / "logs"
        self.output_dir = self.home / "output"
        self.data_dir = self.home / "data"
        self.db_path = self.data_dir / "jobs.db"
        self.token_file = self.data_dir / "tiktok_tokens.json"

    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for d in (
            self.input_dir,
            self.processing_dir,
            self.failed_dir,
            self.archive_dir,
            self.covers_dir,
            self.logs_dir,
            self.output_dir,
            self.data_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def tiktok_configured(self) -> bool:
        return bool(self.tiktok_client_key and self.tiktok_client_secret)


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Build a Config from the environment (plus optional .env file)."""
    home = Path(os.environ.get("TTA_HOME", "") or REPO_ROOT)
    candidates = [Path(env_file)] if env_file else [home / ".env", REPO_ROOT / ".env"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            load_dotenv(candidate, override=False)
            break
    # TTA_HOME may itself come from .env
    home = Path(os.environ.get("TTA_HOME", "") or home)

    env = os.environ
    return Config(
        home=home,
        dry_run=_bool(env.get("DRY_RUN"), True),
        stabilize_seconds=_float(env.get("STABILIZE_SECONDS"), 3.0),
        scan_interval=_float(env.get("SCAN_INTERVAL"), 2.0),
        pair_timeout=_float(env.get("PAIR_TIMEOUT"), 600.0),
        max_retries=_int(env.get("MAX_RETRIES"), 2),
        image_provider=env.get("IMAGE_PROVIDER", "auto").strip().lower() or "auto",
        allow_paid_api=_bool(env.get("ALLOW_PAID_API"), False),
        image_fallback_to_local=_bool(env.get("IMAGE_FALLBACK_TO_LOCAL"), True),
        comfyui_url=env.get("COMFYUI_URL", "").strip(),
        comfyui_workflow=env.get("COMFYUI_WORKFLOW", "").strip(),
        openai_api_key=env.get("OPENAI_API_KEY", "").strip(),
        cover_duration_ms=_int(env.get("COVER_DURATION_MS"), 500),
        ffmpeg_path=env.get("FFMPEG_PATH", "").strip(),
        ffprobe_path=env.get("FFPROBE_PATH", "").strip(),
        tiktok_client_key=env.get("TIKTOK_CLIENT_KEY", "").strip(),
        tiktok_client_secret=env.get("TIKTOK_CLIENT_SECRET", "").strip(),
        tiktok_redirect_uri=env.get(
            "TIKTOK_REDIRECT_URI", "http://127.0.0.1:8765/callback/"
        ).strip(),
        upload_mode=env.get("UPLOAD_MODE", "inbox").strip().lower() or "inbox",
        direct_post_privacy=env.get("DIRECT_POST_PRIVACY", "SELF_ONLY").strip()
        or "SELF_ONLY",
        dashboard_host=env.get("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0",
        dashboard_port=_int(env.get("DASHBOARD_PORT"), 8000),
    )
