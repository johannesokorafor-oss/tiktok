"""Spotify - PREPARE_ONLY (no artist-release ingestion API exists).

Checked 2026-09-27. The official **Spotify Web API** covers catalog metadata,
playlists, playback and user library operations. It has **no endpoint that
ingests a new release**: Spotify for Artists directs artists to deliver music
through a music distributor, and Spotify does not accept direct uploads from
independent artists.

This provider therefore prepares a distributor-ready release package and never
calls any Spotify endpoint. If a distributor with an official API is chosen
later, that belongs in a separate distributor adapter - not here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.config import PlatformMode, PlatformStatus
from app.platforms.base import PlatformDiagnosis, PlatformProvider, PlatformResult
from app.platforms.release_package import build_release_package
from app.platforms.validate import validate_soundcloud_audio

DISTRIBUTOR_NOTE = (
    "Spotify does not provide a public artist-release upload API; releases are delivered by a "
    "music distributor of your choice. Upload the prepared audio, metadata and your own artwork "
    "to your distributor, which then delivers the release to Spotify.")

API_UPLOAD_SUPPORTED = False


class SpotifyProvider(PlatformProvider):
    name = "spotify"
    supports_api_upload = False
    api_summary = ("Spotify Web API has no artist-release ingestion endpoint -> PREPARE_ONLY "
                   "release package for your distributor.")

    @property
    def effective_mode(self) -> PlatformMode:
        return self.mode if API_UPLOAD_SUPPORTED else PlatformMode.PREPARE_ONLY

    def is_configured(self) -> bool:
        return True          # nothing to configure for a distributor package

    def validate(self, asset: Path) -> Dict[str, Any]:
        return validate_soundcloud_audio(asset, self.settings).as_dict()

    def prepare(self, video: Path, plan, out_dir: Path) -> PlatformResult:
        result, master = build_release_package(
            "spotify", video, plan, out_dir, self.settings,
            artist=self.settings.spotify_artist or "",
            album=self.settings.spotify_album or "",
            genre=self.settings.spotify_genre or getattr(plan, "topic", ""),
            release_date=self.settings.spotify_release_date or "",
            explicit=self.settings.spotify_explicit,
            distributor_note=DISTRIBUTOR_NOTE)
        result.validation = self.validate(master.path)
        if not result.validation["ok"]:
            result.status = PlatformStatus.FAILED
            result.error = "; ".join(result.validation["errors"])
        return result

    def diagnose(self) -> PlatformDiagnosis:
        missing = []
        if not (self.settings.spotify_artist or "").strip():
            missing.append("SPOTIFY_ARTIST")
        diag = PlatformDiagnosis(
            platform="spotify", enabled=self.enabled, mode=self.effective_mode,
            configured=True, authenticated=False,
            capability="Release upload API: NOT AVAILABLE -> distributor package",
            required_access="a music distributor account of your choice (no Spotify API key)")
        diag.detail = ("disabled (SPOTIFY_ENABLED=false)" if not self.enabled else
                       (DISTRIBUTOR_NOTE + (f" | supply manually: {', '.join(missing)}"
                                            if missing else " | metadata complete")))
        return diag
