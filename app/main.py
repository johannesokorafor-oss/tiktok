"""Command line entry point.

Normal operation needs no CLI interaction:  ``python -m app.main run``
starts the watcher plus the local dashboard and everything else is
automatic (drop a video + txt into input/).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from app.config import get_settings
from app.diagnostics import exit_code, format_report, run_diagnostics
from app.logging_setup import get_logger, setup_logging

log = get_logger("app.main")


def _banner(settings) -> None:
    from app.images.registry import ImageService
    service = ImageService(settings)
    reports = service.preflight()
    providers = [f"{r.provider}[{r.tier.tier.value}{'' if r.tier.verified else '?'}]"
                 for r in reports]
    generative = [r.provider for r in service.generative_providers(reports)]
    print("=" * 74)
    print(" TikTok Cover & Upload Automation")
    print("=" * 74)
    print(f" input folder      : {settings.input_dir}")
    print(f" output folder     : {settings.output_dir}")
    print(f" quality / style   : {settings.quality_mode.value} / {settings.style_preset.value}")
    print(f" image providers   : {' -> '.join(providers) or 'NONE'}")
    print(f" paid APIs allowed : {settings.allow_paid_api}")
    if generative:
        print(f" image quality     : AI model available ({', '.join(generative)})")
    else:
        print(" image quality     : !! HIGH QUALITY AI IMAGE PROVIDER NOT CONFIGURED !!")
        print("                     covers will use the OFFLINE FALLBACK renderer (Pillow),")
        print("                     which is NOT equivalent to an AI image model.")
        print("                     -> see docs/IMAGE_PROVIDER.md / docs/REAL_WINDOWS_TEST.md")
    print(f" MODE              : {settings.effective_mode.value} (scopes: {settings.tiktok_scopes})")
    for warning in settings.mode_warnings:
        print(f"   ! {warning}")
    if settings.effective_mode.value == "DRY_RUN":
        print(" >> DRY RUN: covers and videos are produced, nothing is uploaded to TikTok.")
    elif settings.effective_mode.value == "DRAFT_UPLOAD":
        print(" >> DRAFT UPLOAD: videos land in your TikTok inbox as drafts; you review and post.")
    else:
        print(" >> DIRECT POST: explicitly confirmed - posts are created through the API.")
    if settings.tiktok_mock:
        print(" >> TIKTOK_MOCK=true: the upload code path runs without any network call.")
    print("=" * 74)


def cmd_run(args) -> int:
    import uvicorn
    from app.dashboard.server import AppContext, create_app

    settings = get_settings()
    settings.ensure_dirs()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    _banner(settings)
    ctx = AppContext(settings, autostart=not args.no_watch)
    app = create_app(ctx)
    url = f"http://127.0.0.1:{settings.dashboard_port}/"
    print(f" dashboard         : {url}\n")
    if args.open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port,
                log_level=settings.log_level.lower())
    return 0


def cmd_watch(args) -> int:
    """Watcher only, no web UI."""
    from app.dashboard.server import AppContext
    settings = get_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    _banner(settings)
    ctx = AppContext(settings, autostart=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping…")
    finally:
        ctx.close()
    return 0


def cmd_process(args) -> int:
    """Process one pair immediately (used by tests and manual runs)."""
    from app.dashboard.server import AppContext
    settings = get_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    ctx = AppContext(settings, autostart=False)
    video = Path(args.video).resolve()
    meta = Path(args.metadata).resolve() if args.metadata else video.with_suffix(".txt")
    if not video.is_file():
        print(f"video not found: {video}", file=sys.stderr)
        return 2
    if not meta.is_file():
        print(f"metadata not found: {meta}", file=sys.stderr)
        return 2
    job_id = ctx.db.create_job(base_name=video.stem, video_path=str(video),
                               metadata_path=str(meta), dry_run=settings.dry_run)
    job = ctx.pipeline.run(job_id, force=args.force)
    print(json.dumps({"job_id": job_id, "state": job["state"], "error": job.get("error"),
                      "cover": job.get("cover_path"), "video": job.get("final_video_path"),
                      "output": job.get("output_dir")}, indent=2, ensure_ascii=False))
    ctx.close()
    return 0 if job["state"] in {"COMPLETED", "UPLOADED"} else 1


def cmd_scan(args) -> int:
    from app.dashboard.server import AppContext
    settings = get_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    ctx = AppContext(settings, autostart=False)
    done = ctx.watcher.scan_once()
    print(json.dumps({"processed_jobs": done}, indent=2))
    ctx.close()
    return 0


def cmd_auth(args) -> int:
    from app.tiktok.oauth import OAuthError, TikTokOAuth
    settings = get_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    oauth = TikTokOAuth(settings)
    if args.action == "status":
        print(json.dumps(oauth.status(), indent=2))
        return 0
    if args.action == "refresh":
        try:
            oauth.refresh()
        except OAuthError as exc:
            print(f"refresh failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(oauth.status(), indent=2))
        return 0
    if args.action == "logout":
        oauth.revoke()
        print("tokens cleared")
        return 0
    # login
    try:
        auth = oauth.authorize_url()
    except OAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Open this URL in your browser, approve the app, then the local dashboard "
          "callback stores the tokens:\n")
    print(auth.url)
    print("\n(The dashboard must be running: python -m app.main run)")
    return 0


def cmd_diagnose(args) -> int:
    settings = get_settings()
    setup_logging(settings.logs_dir, settings.log_level, settings.log_json_file)
    checks = run_diagnostics(settings)
    print(format_report(checks))
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    return exit_code(checks)


def cmd_reset(args) -> int:
    import shutil
    settings = get_settings()
    targets = [settings.db_path, settings.db_path.with_suffix(".sqlite3-wal"),
               settings.db_path.with_suffix(".sqlite3-shm")]
    for t in targets:
        if t.exists():
            t.unlink()
    if args.all:
        for d in (settings.processing_dir, settings.output_dir, settings.covers_dir,
                  settings.failed_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
    settings.ensure_dirs()
    print("state reset (input files untouched; tokens kept)"
          + (" + processing/output/covers/failed cleared" if args.all else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("tiktok-automation")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start watcher + dashboard (default operation)")
    r.add_argument("--no-watch", action="store_true", help="dashboard only")
    r.add_argument("--open-browser", action="store_true")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("watch", help="watcher only, no dashboard")
    w.set_defaults(func=cmd_watch)

    pr = sub.add_parser("process", help="process a single video/metadata pair")
    pr.add_argument("video")
    pr.add_argument("--metadata")
    pr.add_argument("--force", action="store_true", help="ignore duplicate protection")
    pr.set_defaults(func=cmd_process)

    s = sub.add_parser("scan", help="run one watcher pass and exit")
    s.set_defaults(func=cmd_scan)

    a = sub.add_parser("auth", help="TikTok OAuth helper")
    a.add_argument("action", choices=["login", "status", "refresh", "logout"])
    a.set_defaults(func=cmd_auth)

    d = sub.add_parser("diagnose", help="environment diagnostics")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_diagnose)

    rs = sub.add_parser("reset-state", help="delete the job database")
    rs.add_argument("--all", action="store_true", help="also clear processing/output/covers/failed")
    rs.set_defaults(func=cmd_reset)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
