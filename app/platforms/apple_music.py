"""Apple Music - PREPARE_ONLY (no artist-release ingestion API for this workflow).

Checked 2026-09-27. The official **Apple Music API** provides catalog and
personal-library functionality (and MusicKit for playback). **Apple Music for
Artists** is for artist/profile/catalog management and analytics, not release
ingestion: independent artists and labels deliver releases through an approved
music distributor (or Apple-approved encoding partners).

This provider therefore prepares a distributor-ready release package and never
calls any Apple endpoint.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.config import PlatformMode, PlatformStatus
from app.platforms.base import PlatformDiagnosis, PlatformProvider, PlatformResult
from app.platforms.release_package import build_release_package
from app.platforms.validate import validate_soundcloud_audio

DISTRIBUTOR_NOTE = (
    "Apple Music does not provide a public artist-release upload API for this workflow. Apple "
    "Music for Artists manages your artist profile and analytics; release delivery goes through "
    "a music distributor (or an Apple-approved encoding partner). Upload the prepared audio, "
    "metadata and your own artwork there.")

API_UPLOAD_SUPPORTED = False


class AppleMusicProvider(PlatformProvider):
    name = "apple_music"
    supports_api_upload = False
    api_summary = ("Apple Music API is catalog/library only; releases go through a distributor "
                   "-> PREPARE_ONLY release package.")

    @property
    def effective_mode(self) -> PlatformMode:
        return self.mode if API_UPLOAD_SUPPORTED else PlatformMode.PREPARE_ONLY

    def is_configured(self) -> bool:
        return True

    def validate(self, asset: Path) -> Dict[str, Any]:
        return validate_soundcloud_audio(asset, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        result, master = build_release_package(
            "apple_music", video, plan, out_dir, self.settings,
            artist=self.settings.apple_music_artist or "",
            album=self.settings.apple_music_album or "",
            genre=self.settings.apple_music_genre or getattr(plan, "topic", ""),
            release_date=self.settings.apple_music_release_date or "",
            explicit=self.settings.apple_music_explicit,
            distributor_note=DISTRIBUTOR_NOTE)
        result.validation = self.validate(master.path)
        if not result.validation["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(result.validation["errors"])
        return result

    def diagnose(self) -> PlatformDiagnosis:
        missing = []
        if not (self.settings.apple_music_artist or "").strip():
            missing.append("APPLE_MUSIC_ARTIST")
        diag = PlatformDiagnosis(
            platform="apple_music", enabled=self.enabled, mode=self.effective_mode,
            configured=True, authenticated=False,
            capability="Release upload API: NOT AVAILABLE -> distributor package",
            required_access="a music distributor account of your choice (no Apple API key)")
        diag.detail = ("disabled (APPLE_MUSIC_ENABLED=false)" if not self.enabled else
                       (DISTRIBUTOR_NOTE + (f" | supply manually: {', '.join(missing)}"
                                            if missing else " | metadata complete")))
        return diag
