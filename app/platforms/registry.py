"""Registry of the optional platform providers (all disabled by default)."""
from __future__ import annotations

from typing import Dict, List, Type

from app.config import Settings
from app.platforms.base import PlatformProvider
from app.platforms.apple_music import AppleMusicProvider
from app.platforms.dailymotion import DailymotionProvider
from app.platforms.patreon import PatreonProvider
from app.platforms.rumble import RumbleProvider
from app.platforms.soundcloud import SoundCloudProvider
from app.platforms.spotify import SpotifyProvider
from app.platforms.vimeo import VimeoProvider
from app.platforms.youtube import YouTubeProvider

#: order in which enabled platforms run after the TikTok/Instagram stages
PROVIDER_CLASSES: Dict[str, Type[PlatformProvider]] = {
    "youtube": YouTubeProvider,
    "vimeo": VimeoProvider,
    "dailymotion": DailymotionProvider,
    "soundcloud": SoundCloudProvider,
    "spotify": SpotifyProvider,
    "apple_music": AppleMusicProvider,
    "patreon": PatreonProvider,
    "rumble": RumbleProvider,
}


def build_provider(name: str, settings: Settings) -> PlatformProvider:
    cls = PROVIDER_CLASSES.get(name.strip().lower())
    if cls is None:
        raise KeyError(f"unknown platform '{name}'")
    return cls(settings)


def all_providers(settings: Settings) -> List[PlatformProvider]:
    return [cls(settings) for cls in PROVIDER_CLASSES.values()]


def enabled_providers(settings: Settings) -> List[PlatformProvider]:
    """Only platforms the user explicitly switched on."""
    return [p for p in all_providers(settings) if p.enabled]


def diagnose_all(settings: Settings) -> List[dict]:
    out = []
    for provider in all_providers(settings):
        try:
            out.append(provider.diagnose().as_dict())
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            out.append({"platform": provider.name, "enabled": provider.enabled,
                        "mode": provider.mode.value, "configured": False,
                        "authenticated": False, "capability": "unknown",
                        "detail": f"diagnosis failed: {exc}", "last_error": str(exc)})
    return out
