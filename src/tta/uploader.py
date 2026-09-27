"""Glue between the pipeline and the TikTok Content Posting API."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .tiktok_api import TikTokClient, TokenStore

log = logging.getLogger("tta.uploader")


def build_uploader(config: Config):
    """Return an uploader callable, or None when TikTok is not configured.

    The callable signature is ``(job, final_path, plan, cover_timestamp_ms) -> dict``
    and its return value is persisted as ``upload_result.json``.
    """
    if not config.tiktok_configured:
        return None
    token_store = TokenStore(config.token_file)
    client = TikTokClient(config.tiktok_client_key, config.tiktok_client_secret,
                          token_store)

    def upload(job, final_path: Path, plan, cover_timestamp_ms: int) -> dict:
        final_path = Path(final_path)
        size = final_path.stat().st_size
        caption = getattr(plan, "caption", "") or getattr(plan, "hook", "")
        if config.upload_mode == "direct":
            creator = client.creator_info()
            privacy_options = creator.get("privacy_level_options") or []
            privacy = config.direct_post_privacy
            if privacy_options and privacy not in privacy_options:
                log.warning("privacy %s not available, using %s",
                            privacy, privacy_options[0])
                privacy = privacy_options[0]
            init = client.init_direct_post(
                video_size=size,
                title=caption,
                privacy_level=privacy,
                cover_timestamp_ms=cover_timestamp_ms,
                is_aigc=True,  # cover contains AI-generated imagery
            )
        else:
            init = client.init_inbox_upload(video_size=size)

        publish_id = init.get("publish_id", "")
        upload_url = init.get("upload_url", "")
        if not publish_id or not upload_url:
            raise RuntimeError("TikTok init response missing publish_id/upload_url")
        log.info("uploading %s (%.1f MB) publish_id=%s mode=%s",
                 final_path.name, size / 1e6, publish_id, config.upload_mode)
        client.upload_file(upload_url, final_path)
        status = client.wait_for_upload(publish_id)
        return {
            "mode": config.upload_mode,
            "publish_id": publish_id,
            "status": status.get("status", "UNKNOWN"),
            "video_cover_timestamp_ms": cover_timestamp_ms,
            "detail": status,
        }

    return upload
