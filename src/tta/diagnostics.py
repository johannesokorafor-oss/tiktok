"""System diagnostics: verifies everything the pipeline needs."""

from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config

REQUIRED_PACKAGES = ("PIL", "dotenv", "requests", "fastapi", "uvicorn")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def _run_version(exe: str) -> str:
    proc = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=20)
    first = proc.stdout.decode("utf-8", errors="replace").splitlines()
    return first[0] if first else "unknown version"


def run_diagnostics(config: Config, check_network: bool = True) -> list[Check]:
    checks: list[Check] = []

    # Python
    version_ok = sys.version_info >= (3, 10)
    checks.append(Check("python", version_ok,
                        f"{sys.version.split()[0]} at {sys.executable}"
                        + ("" if version_ok else " (need >= 3.10)")))

    # Packages
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            checks.append(Check(f"package:{pkg}", True, "importable"))
        except ImportError as exc:
            checks.append(Check(f"package:{pkg}", False, str(exc)))

    # FFmpeg / ffprobe
    from . import video as videomod
    for label, finder in (("ffmpeg", videomod.find_ffmpeg),
                          ("ffprobe", videomod.find_ffprobe)):
        configured = config.ffmpeg_path if label == "ffmpeg" else config.ffprobe_path
        try:
            exe = finder(configured)
            checks.append(Check(label, True, f"{exe} ({_run_version(exe)})"))
        except Exception as exc:
            checks.append(Check(label, False, str(exc)))

    # Fonts
    try:
        from .typography import find_font
        checks.append(Check("font", True, find_font()))
    except Exception as exc:
        checks.append(Check("font", False, str(exc)))

    # Directories
    try:
        config.ensure_dirs()
        probe_file = config.data_dir / ".write_test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        checks.append(Check("directories", True,
                            f"created/writable under {config.home}"))
    except OSError as exc:
        checks.append(Check("directories", False, str(exc)))

    # Database
    try:
        conn = sqlite3.connect(str(config.db_path))
        conn.execute("SELECT 1")
        conn.close()
        checks.append(Check("database", True, str(config.db_path)))
    except sqlite3.Error as exc:
        checks.append(Check("database", False, str(exc)))

    # Image providers
    try:
        from .providers import build_registry
        registry = build_registry(config)
        selected = registry.select()
        details = ", ".join(
            f"{d['name']}[{d['cost']}]={'ok' if d['available'] else 'unavailable'}"
            for d in registry.describe()
        )
        checks.append(Check("image-provider", True,
                            f"selected='{selected.name}'; {details}"))
    except Exception as exc:
        checks.append(Check("image-provider", False, str(exc)))

    # TikTok configuration
    if config.tiktok_configured:
        from .tiktok_api import TokenStore
        token_status = TokenStore(config.token_file).status()
        auth = "authenticated" if token_status["authenticated"] else \
            "credentials set, not yet authorized (run `tta auth login`)"
        checks.append(Check("tiktok-config", True, auth, critical=False))
    else:
        checks.append(Check(
            "tiktok-config", config.dry_run,
            "TIKTOK_CLIENT_KEY/SECRET not set - uploads disabled "
            "(dry-run pipeline still fully works)", critical=False))

    # Internet
    if check_network:
        import requests
        try:
            requests.head("https://open.tiktokapis.com", timeout=6)
            checks.append(Check("internet", True,
                                "open.tiktokapis.com reachable", critical=False))
        except requests.RequestException as exc:
            checks.append(Check("internet", False,
                                f"open.tiktokapis.com unreachable "
                                f"({exc.__class__.__name__})", critical=False))

    # Disk space
    try:
        usage = shutil.disk_usage(config.home)
        free_gb = usage.free / (1024 ** 3)
        checks.append(Check("disk-space", free_gb > 2,
                            f"{free_gb:.1f} GB free at {config.home}"
                            + ("" if free_gb > 2 else " (< 2 GB!)")))
    except OSError as exc:
        checks.append(Check("disk-space", False, str(exc)))

    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = "OK  " if check.ok else ("FAIL" if check.critical else "WARN")
        lines.append(f"[{mark}] {check.name:<18} {check.detail}")
    failed = [c for c in checks if not c.ok and c.critical]
    lines.append("")
    lines.append("All critical checks passed." if not failed
                 else f"{len(failed)} critical check(s) FAILED.")
    return "\n".join(lines)
