"""Shared interface for the optional platform integrations.

Every provider declares honestly what its platform's **current official API**
supports:

``UPLOAD_PRIVATE``   real API upload, result is not publicly visible
``API_STAGE_ONLY``   real API upload that only stages media (no publish)
``PREPARE_ONLY``     no API call at all; local files for a manual upload
``UNSUPPORTED``      the platform has no official API for this action

There is deliberately no ``publish()`` and no ``publish_all()`` anywhere in
this package: nothing in this application ever makes content public.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import PlatformMode, PlatformStatus, Settings


class PlatformError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class PlatformAuthError(PlatformError):
    """Credentials missing/invalid - never retried in a loop."""


class PublishingNotAllowed(RuntimeError):
    """Raised if anything ever tries to make content public."""


@dataclass
class PlatformResult:
    platform: str
    mode: PlatformMode
    status: PlatformStatus
    external_id: Optional[str] = None
    url: Optional[str] = None
    message: str = ""
    error: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "mode": self.mode.value,
            "status": self.status.value,
            "external_id": self.external_id,
            "url": self.url,
            "message": self.message,
            "error": self.error,
            "artifacts": self.artifacts,
            "validation": self.validation,
            "metadata": self.metadata,
            "auto_publish": False,
            "public": False,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class PlatformDiagnosis:
    platform: str
    enabled: bool
    mode: PlatformMode
    configured: bool
    authenticated: bool
    capability: str
    detail: str = ""
    account: str = ""
    required_access: str = ""
    last_error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"platform": self.platform, "enabled": self.enabled, "mode": self.mode.value,
                "configured": self.configured, "authenticated": self.authenticated,
                "capability": self.capability, "detail": self.detail, "account": self.account,
                "required_access": self.required_access, "last_error": self.last_error}


class PlatformProvider(abc.ABC):
    """Base class. Subclasses implement only what their API really supports."""

    name: str = "base"
    #: human readable summary of the official API situation
    api_summary: str = ""
    #: True when the platform performs a genuine API upload
    supports_api_upload: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_error: Optional[str] = None

    # ---------------------------------------------------------------- config
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, f"{self.name}_enabled", False))

    @property
    def mode(self) -> PlatformMode:
        return getattr(self.settings, f"{self.name}_mode", PlatformMode.PREPARE_ONLY)

    @property
    def effective_mode(self) -> PlatformMode:
        """The mode actually used, after capability checks."""
        return self.mode

    def configure(self) -> Dict[str, Any]:
        """Return the (redacted) effective configuration of this provider."""
        return {"platform": self.name, "enabled": self.enabled,
                "mode": self.effective_mode.value, "api_summary": self.api_summary}

    # ---------------------------------------------------------------- auth
    def is_configured(self) -> bool:
        return True

    def authenticate(self) -> bool:
        """Ensure a usable credential exists. PREPARE_ONLY providers need none."""
        return True

    def disconnect(self) -> None:
        """Remove any stored credentials for this platform."""
        return None

    # ---------------------------------------------------------------- work
    @abc.abstractmethod
    def validate(self, video: Path) -> Dict[str, Any]:
        """Validate the asset against this platform's documented requirements."""

    @abc.abstractmethod
    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        """Produce local artifacts for a manual upload. Never calls an API."""

    def upload(self, video: Path, plan, out_dir: Path,
               on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        """Real API upload. Only implemented by providers whose API supports it."""
        raise PlatformError(
            f"{self.name} has no API upload in this application "
            f"(mode {self.effective_mode.value}); use prepare() instead")

    def get_status(self, external_id: str) -> PlatformResult:
        raise PlatformError(f"{self.name} does not expose a status endpoint here")

    def retry(self, video: Path, plan, out_dir: Path,
              on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        """Retry whatever this platform is allowed to do."""
        if self.effective_mode in (PlatformMode.UPLOAD_PRIVATE, PlatformMode.API_STAGE_ONLY):
            return self.upload(video, plan, out_dir, on_event=on_event)
        return self.prepare(video, plan, out_dir)

    def run(self, video: Path, plan, out_dir: Path,
            on_event: Optional[Callable[[str, str, dict], None]] = None) -> PlatformResult:
        """Execute the single action this platform is allowed to perform."""
        if not self.enabled:
            return PlatformResult(self.name, self.effective_mode, PlatformStatus.DISABLED,
                                  message=f"{self.name} is disabled")
        if self.effective_mode in (PlatformMode.UPLOAD_PRIVATE, PlatformMode.API_STAGE_ONLY):
            return self.upload(video, plan, out_dir, on_event=on_event)
        if self.effective_mode == PlatformMode.PREPARE_ONLY:
            return self.prepare(video, plan, out_dir)
        return PlatformResult(self.name, self.effective_mode, PlatformStatus.NOT_CONFIGURED,
                              message=self.api_summary)

    # ---------------------------------------------------------------- diag
    @abc.abstractmethod
    def diagnose(self) -> PlatformDiagnosis:
        """Report enabled/mode/auth/capability for diagnostics and dashboard."""
