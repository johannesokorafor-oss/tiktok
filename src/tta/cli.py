"""Command line interface.

    tta start                 watcher + dashboard (the normal way to run)
    tta watch                 watcher only
    tta dashboard             read-only dashboard
    tta process VIDEO TEXT    process one pair immediately (--force to
                              override duplicate protection)
    tta retry JOB_ID          retry a failed/duplicate job (--force)
    tta regen-cover JOB_ID    regenerate the cover of a job
    tta jobs                  list recent jobs
    tta auth login|status|logout
    tta diagnose              full system diagnostics
    tta sample                create a demo video+text pair in input/
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import threading
from pathlib import Path

from . import __version__, db
from .config import Config, load_config
from .logging_setup import setup_logging


def _make_stack(config: Config):
    from .pipeline import Pipeline
    from .uploader import build_uploader

    config.ensure_dirs()
    store = db.JobStore(config.db_path)
    pipeline = Pipeline(config, store, uploader=build_uploader(config))
    return store, pipeline


def _run_dashboard(app, config: Config):
    import uvicorn

    uvicorn.run(app, host=config.dashboard_host, port=config.dashboard_port,
                log_level="warning")


# ----------------------------------------------------------------- commands
def cmd_start(config: Config, args) -> int:
    from .dashboard import create_app
    from .watcher import Watcher

    store, pipeline = _make_stack(config)
    watcher = Watcher(config, store, pipeline)
    app = create_app(config, store, watcher=watcher, pipeline=pipeline)

    thread = threading.Thread(target=watcher.run_forever, daemon=True)
    thread.start()
    print(f"Watching:  {config.input_dir}")
    print(f"Dashboard: http://127.0.0.1:{config.dashboard_port}")
    print(f"Dry run:   {config.dry_run}")
    try:
        _run_dashboard(app, config)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        thread.join(timeout=10)
    return 0


def cmd_watch(config: Config, args) -> int:
    from .watcher import Watcher

    store, pipeline = _make_stack(config)
    watcher = Watcher(config, store, pipeline)
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        watcher.stop()
    return 0


def cmd_dashboard(config: Config, args) -> int:
    from .dashboard import create_app
    from .providers import build_registry

    config.ensure_dirs()
    store = db.JobStore(config.db_path)
    app = create_app(config, store, registry=build_registry(config))
    print(f"Dashboard (read-only): http://127.0.0.1:{config.dashboard_port}")
    _run_dashboard(app, config)
    return 0


def cmd_process(config: Config, args) -> int:
    store, pipeline = _make_stack(config)
    video = Path(args.video).resolve()
    text = Path(args.text).resolve()
    if not video.is_file() or not text.is_file():
        print("error: video or text file not found", file=sys.stderr)
        return 2
    # work on copies in processing/ so originals stay in place untouched
    config.processing_dir.mkdir(parents=True, exist_ok=True)
    video_copy = config.processing_dir / video.name
    text_copy = config.processing_dir / text.name
    shutil.copy2(video, video_copy)
    shutil.copy2(text, text_copy)
    job = store.create_job(video.stem, str(video_copy), str(text_copy))
    job = pipeline.run_job(job, force=args.force)
    print(json.dumps({"id": job.id, "state": job.state, "error": job.error,
                      "output_dir": job.output_dir}, indent=2))
    return 0 if job.state in (db.COMPLETED, db.UPLOADED) else 1


def cmd_retry(config: Config, args) -> int:
    store, pipeline = _make_stack(config)
    job = store.get(args.job_id)
    if not job:
        print(f"error: job {args.job_id} not found", file=sys.stderr)
        return 2
    job = pipeline.run_job(job, force=args.force or job.state == db.DUPLICATE)
    print(f"{job.id}: {job.state} {job.error or ''}".strip())
    return 0 if job.state in (db.COMPLETED, db.UPLOADED) else 1


def cmd_regen_cover(config: Config, args) -> int:
    store, pipeline = _make_stack(config)
    try:
        cover = pipeline.regenerate_cover(args.job_id)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"new cover: {cover}")
    return 0


def cmd_jobs(config: Config, args) -> int:
    store = db.JobStore(config.db_path)
    jobs = store.list_jobs(limit=args.limit)
    if not jobs:
        print("no jobs yet")
        return 0
    for job in jobs:
        print(f"{job.id}  {job.state:<18} {job.base_name:<30} "
              f"{job.upload_status or '-':<20} {job.error[:60]}")
    return 0


def cmd_auth(config: Config, args) -> int:
    from .tiktok_api import TokenStore
    from .tiktok_api.oauth import OAuthError, run_local_auth_flow

    token_store = TokenStore(config.token_file)
    if args.action == "status":
        status = token_store.status()
        status["configured"] = config.tiktok_configured
        print(json.dumps(status, indent=2))
        return 0
    if args.action == "logout":
        token_store.clear()
        print("local tokens removed")
        return 0
    # login
    if not config.tiktok_configured:
        print("error: set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first "
              "(see docs/TIKTOK_SETUP.md)", file=sys.stderr)
        return 2
    try:
        tokens = run_local_auth_flow(
            config.tiktok_client_key, config.tiktok_client_secret,
            config.tiktok_redirect_uri, open_browser=not args.no_browser,
        )
    except OAuthError as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 1
    token_store.save(tokens)
    print("TikTok authorization successful; tokens stored at "
          f"{config.token_file} (never commit this file)")
    return 0


def cmd_diagnose(config: Config, args) -> int:
    from .diagnostics import format_checks, run_diagnostics

    checks = run_diagnostics(config, check_network=not args.offline)
    print(format_checks(checks))
    return 0 if all(c.ok or not c.critical for c in checks) else 1


def cmd_sample(config: Config, args) -> int:
    """Create a demo pair in input/ using ffmpeg's test source."""
    import subprocess

    from .video import find_ffmpeg

    config.ensure_dirs()
    video_path = config.input_dir / "demo_reise.mp4"
    text_path = config.input_dir / "demo_reise.txt"
    exe = find_ffmpeg(config.ffmpeg_path)
    subprocess.run([
        exe, "-y",
        "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(video_path),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    text_path.write_text(
        "TITLE:\nDie Reise beginnt in dir\n\n"
        "DESCRIPTION:\nManchmal zeigt dir das Universum Zeichen, wenn du sie am "
        "wenigsten erwartest. Diese kurze Meditation über Energie und "
        "Bewusstsein hilft dir, deine Intuition zu stärken.\n\n"
        "IMAGE_PROMPT:\nEine einsame Silhouette auf einem Berg unter einem "
        "leuchtenden Nachthimmel, mystisches Licht, Nebel\n",
        encoding="utf-8",
    )
    print(f"sample pair created:\n  {video_path}\n  {text_path}")
    print("run `tta start` (or scripts\\start.ps1) and watch it get processed")
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tta",
                                     description="TikTok Auto-Poster")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", help="path to a .env file")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="watcher + dashboard")
    sub.add_parser("watch", help="watcher only")
    sub.add_parser("dashboard", help="read-only dashboard")

    p = sub.add_parser("process", help="process one video+text pair now")
    p.add_argument("video")
    p.add_argument("text")
    p.add_argument("--force", action="store_true",
                   help="force reprocess even if duplicate content")

    p = sub.add_parser("retry", help="retry a job")
    p.add_argument("job_id")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("regen-cover", help="regenerate a job's cover")
    p.add_argument("job_id")

    p = sub.add_parser("jobs", help="list recent jobs")
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("auth", help="TikTok authentication")
    p.add_argument("action", choices=["login", "status", "logout"])
    p.add_argument("--no-browser", action="store_true")

    p = sub.add_parser("diagnose", help="run system diagnostics")
    p.add_argument("--offline", action="store_true", help="skip network checks")

    sub.add_parser("sample", help="create a demo input pair")
    return parser


_COMMANDS = {
    "start": cmd_start,
    "watch": cmd_watch,
    "dashboard": cmd_dashboard,
    "process": cmd_process,
    "retry": cmd_retry,
    "regen-cover": cmd_regen_cover,
    "jobs": cmd_jobs,
    "auth": cmd_auth,
    "diagnose": cmd_diagnose,
    "sample": cmd_sample,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.env_file)
    setup_logging(config.logs_dir,
                  logging.DEBUG if args.verbose else logging.INFO)
    return _COMMANDS[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
