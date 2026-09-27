"""First-run diagnostics: PASS / WARN / FAIL report."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

from app.config import Settings
from app.storage.db import Database
from app.tiktok.oauth import TikTokOAuth
from app.video.ffmpeg import FFmpegError, find_ffmpeg, find_ffprobe, run

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _net_ok(host: str = "open.tiktokapis.com", port: int = 443, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_diagnostics(settings: Settings) -> List[Check]:
    checks: List[Check] = []

    # python
    v = sys.version_info
    checks.append(Check("Python", PASS if v >= (3, 11) else FAIL,
                        f"{platform.python_version()} ({sys.executable})"))

    # ffmpeg / ffprobe
    try:
        ff = find_ffmpeg(settings)
        out = run([ff, "-version"], timeout=60).stdout.splitlines()[0]
        checks.append(Check("FFmpeg", PASS, out))
    except (FFmpegError, Exception) as exc:  # noqa: BLE001
        checks.append(Check("FFmpeg", FAIL, str(exc)))
    try:
        fp = find_ffprobe(settings)
        checks.append(Check("ffprobe", PASS, fp))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("ffprobe", FAIL, str(exc)))

    # directories + permissions
    try:
        settings.ensure_dirs()
        probe = settings.state_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("Filesystem permissions", PASS, f"writable: {settings.base_dir}"))
    except OSError as exc:
        checks.append(Check("Filesystem permissions", FAIL, str(exc)))

    # disk space
    try:
        usage = shutil.disk_usage(settings.base_dir)
        free_gb = usage.free / 1e9
        checks.append(Check("Disk space", PASS if free_gb >= 5 else WARN,
                            f"{free_gb:.1f} GB free"))
    except OSError as exc:
        checks.append(Check("Disk space", WARN, str(exc)))

    # watched folder
    try:
        settings.input_dir.mkdir(parents=True, exist_ok=True)
        pending = [p.name for p in settings.input_dir.glob("*")
                   if p.suffix.lower() in {e.lower() for e in settings.video_extensions}]
        checks.append(Check("Watched folder", PASS,
                            f"{settings.input_dir} ({len(pending)} video file(s) present)"))
    except OSError as exc:
        checks.append(Check("Watched folder", FAIL, str(exc)))

    # database
    try:
        db = Database(settings.db_path)
        stats = db.stats()
        db.close()
        checks.append(Check("SQLite database", PASS, f"{settings.db_path} jobs={sum(stats.values())}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("SQLite database", FAIL, str(exc)))

    # ---------------- operating mode ----------------
    mode = settings.effective_mode
    mode_detail = {
        "DRY_RUN": "process + validate the video locally, upload NOTHING",
        "DRAFT_UPLOAD": "official inbox/draft upload - you review and publish inside TikTok",
        "DIRECT_POST": "Direct Post API (opt-in, confirmed) - posts without a second review step",
    }[mode.value]
    checks.append(Check(f"MODE: {mode.value}", PASS, mode_detail))
    for warning in settings.mode_warnings:
        checks.append(Check("  mode note", WARN, warning))

    # tiktok configuration
    oauth = TikTokOAuth(settings)
    st = oauth.status()
    if not st["client_configured"]:
        checks.append(Check("TikTok app credentials", FAIL,
                            "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing in .env"))
    else:
        checks.append(Check("TikTok app credentials", PASS, "client key + secret present"))
    if st["authenticated"]:
        checks.append(Check("TikTok OAuth", PASS if not st["access_token_expired"] else WARN,
                            f"open_id={st['open_id']} expires_in={st['expires_in_seconds']}s "
                            f"scopes={st['scope'] or st['scopes']}"))
    else:
        checks.append(Check("TikTok OAuth", WARN,
                            "not authenticated - open the dashboard and click Connect"))
    scopes = settings.tiktok_scopes
    need = "video.upload" if settings.tiktok_post_mode.upper() == "UPLOAD" else "video.publish"
    checks.append(Check("TikTok scopes", PASS if need in scopes else FAIL,
                        f"configured='{scopes}' required for {settings.tiktok_post_mode}: {need}"))

    # ---------------- instagram (optional) ----------------
    from app.instagram.oauth import InstagramOAuth
    from app.instagram.service import InstagramService
    ig = InstagramService(settings, InstagramOAuth(settings)).status()
    if not settings.instagram_enabled:
        checks.append(Check("Instagram integration", PASS,
                            "disabled (INSTAGRAM_ENABLED=false) - no Instagram API calls, "
                            "no credentials required"))
    elif not settings.instagram_api_staging:
        checks.append(Check("Instagram integration", PASS,
                            f"enabled in {ig['mode']} mode - no Instagram API calls, no "
                            "credentials needed. Use 'Prepare for Instagram' on a finished job "
                            "to produce instagram_ready.mp4 + caption_instagram.txt for a "
                            "manual upload."))
        checks.append(Check("  Instagram cover", PASS,
                            "selected manually by the user in Instagram"))
    else:
        ig_status = ig["status"]
        if ig_status in {"NOT_CONFIGURED", "AUTH_REQUIRED"}:
            checks.append(Check("Instagram integration", WARN,
                                f"Instagram enabled but not ready: {ig['detail']}"))
        elif ig_status == "READY_TO_PUBLISH":
            auth = ig["auth"]
            checks.append(Check("Instagram integration", PASS,
                                f"Instagram Professional account connected "
                                f"(user_id={auth.get('user_id') or 'n/a'}, "
                                f"token expires in {auth.get('expires_in_seconds', 0)}s, "
                                f"long-lived={auth.get('long_lived')})"))
        else:
            checks.append(Check("Instagram integration", WARN, ig["detail"]))
        checks.append(Check("  Instagram mode", WARN,
                            f"{ig['mode']} - creates Meta media containers that are NOT "
                            f"Instagram app drafts and expire after "
                            f"{settings.instagram_container_ttl_hours:.0f} h | "
                            f"login={ig['login_mode']} | "
                            f"upload method={ig['upload_method']} | "
                            f"Automatic publish: DISABLED ({ig['auto_publish_note']})"))
        checks.append(Check("  Instagram cover", PASS,
                            "selected manually by the user in Instagram"))
        if ig["upload_method"] == "unavailable":
            checks.append(Check("  Instagram upload method", WARN,
                                ig["upload_method_reason"]))

    # ---------------- optional extra platforms ----------------
    from app.platforms.registry import diagnose_all
    for entry in diagnose_all(settings):
        name = entry["platform"].capitalize()
        if not entry["enabled"]:
            checks.append(Check(f"{name}", PASS,
                                f"Enabled: NO | Mode: {entry['mode']} | "
                                f"{entry['capability']}"))
            continue
        ready = entry["configured"] and (entry["authenticated"] or not entry["configured"])
        status = PASS
        if entry["mode"] in {"UPLOAD_PRIVATE", "API_STAGE_ONLY"} and not entry["authenticated"]:
            status = WARN
        checks.append(Check(
            f"{name}", status,
            f"Enabled: YES | Mode: {entry['mode']} | Authentication: "
            + ("OK" if entry["authenticated"] else
               ("MISSING" if entry["mode"] in {"UPLOAD_PRIVATE", "API_STAGE_ONLY"} else "N/A"))
            + f" | Required API access: {entry['required_access']}"
            + f" | Capability: {entry['capability']}"
            + (f" | Last error: {entry['last_error']}" if entry["last_error"] else "")))
        if entry["detail"]:
            checks.append(Check(f"  {name} detail", PASS if status == PASS else WARN,
                                entry["detail"]))

    # connectivity
    checks.append(Check("Internet (TikTok API)", PASS if _net_ok() else WARN,
                        "open.tiktokapis.com:443 reachable" if _net_ok() else
                        "cannot reach open.tiktokapis.com - uploads will fail"))

    # end-to-end capability summary: can the local/dry-run path run right now?
    hard = {c.name: c.status for c in checks}
    local_ready = (hard.get("Python") == PASS and hard.get("FFmpeg") == PASS
                   and hard.get("ffprobe") == PASS and hard.get("SQLite database") == PASS
                   and hard.get("Filesystem permissions") == PASS
                   and hard.get("Watched folder") == PASS)
    checks.append(Check(
        "Local pipeline (video processing, DRY_RUN)", PASS if local_ready else FAIL,
        "ready - drop a video + .txt into input/" if local_ready else
        "not ready - fix the FAIL items above (Python, FFmpeg, permissions, database)"))
    checks.append(Check(
        "Thumbnail / cover", PASS,
        "not generated by this application - you create and select it manually in TikTok"))

    upload_blockers = []
    if not st["client_configured"]:
        upload_blockers.append("TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET in .env")
    if not st["authenticated"]:
        upload_blockers.append("OAuth connect (dashboard button or 'python -m app.main auth login')")
    if need not in scopes:
        upload_blockers.append(f"scope {need} in TIKTOK_SCOPES")
    if settings.dry_run:
        upload_blockers.append("DRY_RUN=false (currently true, nothing will be uploaded)")
    checks.append(Check(
        "TikTok upload path", PASS if not upload_blockers else WARN,
        "ready" if not upload_blockers else "missing: " + "; ".join(upload_blockers)))

    # modes / cost safety
    checks.append(Check("Mode", WARN if settings.dry_run else PASS,
                        f"DRY_RUN={settings.dry_run} TIKTOK_MOCK={settings.tiktok_mock} "
                        f"VIDEO_NORMALIZATION={settings.video_normalization}"))
    env_file = settings.base_dir / ".env"
    checks.append(Check(".env", PASS if env_file.is_file() else WARN,
                        str(env_file) if env_file.is_file() else "missing - copy .env.example"))
    return checks


def format_report(checks: List[Check]) -> str:
    width = max(len(c.name) for c in checks) + 2
    lines = ["", "=" * 78, " DIAGNOSTIC REPORT", "=" * 78]
    for c in checks:
        lines.append(f"[{c.status:<4}] {c.name.ljust(width)} {c.detail}")
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    lines += ["-" * 78,
              f" PASS={counts[PASS]}  WARN={counts[WARN]}  FAIL={counts[FAIL]}", "=" * 78, ""]
    return "\n".join(lines)


def exit_code(checks: List[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
