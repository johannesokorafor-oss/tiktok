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

from app.config import ProviderCost, Settings
from app.cover.fonts import FontNotFoundError, find_bold_font
from app.images.registry import ImageService
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

    # database
    try:
        db = Database(settings.db_path)
        stats = db.stats()
        db.close()
        checks.append(Check("SQLite database", PASS, f"{settings.db_path} jobs={sum(stats.values())}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("SQLite database", FAIL, str(exc)))

    # fonts
    try:
        font = find_bold_font(settings.font_bold)
        checks.append(Check("Headline font", PASS, str(font)))
    except FontNotFoundError as exc:
        checks.append(Check("Headline font", FAIL, str(exc)))

    # ---------------- image providers ----------------
    svc = ImageService(settings)
    if not svc.chain:
        checks.append(Check("Image provider", FAIL, "no usable provider configured"))
    reports = svc.preflight()
    for report in reports:
        provider = next(p for p in svc.chain if p.name == report.provider)
        status = PASS if report.usable else WARN
        if report.tier.tier == ProviderCost.PAID and not settings.allow_paid_api:
            status = WARN
        bits = [
            f"Tier: {report.tier.tier.value} ({report.tier.source})",
            f"Model: {report.model or 'n/a'}",
            "Authentication: " + ("N/A" if not provider.requires_auth()
                                  else ("OK" if report.authenticated else "MISSING")),
            "High Quality: " + ("YES" if report.generative else "NO (fallback renderer)"),
            f"Resolution {settings.cover_width}x{settings.cover_height}: "
            + ("OK" if report.resolution_ok else "UNSUPPORTED"),
            f"Status: {report.detail}",
        ]
        checks.append(Check(f"Image provider: {report.provider}", status, " | ".join(bits)))
        if report.tier.detail and report.tier.tier == ProviderCost.UNKNOWN:
            checks.append(Check(f"  └ {report.provider} pricing", WARN, report.tier.detail))

    generative = svc.generative_providers(reports)
    hq_ready = bool(generative)
    fallback_enabled = settings.allow_offline_image_fallback
    checks.append(Check(
        "Image quality capability",
        PASS if hq_ready else (WARN if fallback_enabled else FAIL),
        (f"AI image model available ({', '.join(r.provider for r in generative)}); "
         f"Fallback Enabled: {'YES' if fallback_enabled else 'NO'}") if hq_ready else
        ("No AI image model reachable - covers would use the OFFLINE FALLBACK renderer. "
         f"Fallback Enabled: {'YES' if fallback_enabled else 'NO'}."
         + ("" if fallback_enabled else " With fallback disabled, jobs will FAIL."))))
    if settings.quality_mode.value == "HIGH_QUALITY" and not hq_ready:
        checks.append(Check(
            "HIGH_QUALITY mode", FAIL if not (settings.dry_run and settings.dry_run_allow_offline)
            else WARN,
            "HIGH_QUALITY requested but no genuine image provider is available - jobs will fail "
            "unless DRY_RUN_ALLOW_OFFLINE=true (dry runs) or a provider is configured."))

    # ---------------- operating mode ----------------
    mode = settings.effective_mode
    mode_detail = {
        "DRY_RUN": "produce cover + video locally, upload NOTHING",
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

    # connectivity
    checks.append(Check("Internet (TikTok API)", PASS if _net_ok() else WARN,
                        "open.tiktokapis.com:443 reachable" if _net_ok() else
                        "cannot reach open.tiktokapis.com - uploads will fail"))

    # end-to-end capability summary: can the local/dry-run path run right now?
    hard = {c.name: c.status for c in checks}
    local_ready = (hard.get("Python") == PASS and hard.get("FFmpeg") == PASS
                   and hard.get("ffprobe") == PASS and hard.get("Headline font") == PASS
                   and hard.get("SQLite database") == PASS
                   and any(c.status == PASS and c.name.startswith("Image provider:")
                           for c in checks))
    checks.append(Check(
        "Local pipeline (cover + video, DRY_RUN)", PASS if local_ready else FAIL,
        "ready - drop a video + .txt into input/" if local_ready else
        "not ready - fix the FAIL/WARN items above (Python, FFmpeg, font, provider)"))

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
                        f"ALLOW_PAID_API={settings.allow_paid_api}"))
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
