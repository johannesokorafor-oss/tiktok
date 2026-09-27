"""Structured JSON logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SECRET_PATTERNS = [
    re.compile(r"(act\.[A-Za-z0-9\-_.]{6,})"),
    re.compile(r"(rft\.[A-Za-z0-9\-_.]{6,})"),
    re.compile(r"((?:access|refresh)_token\"?\s*[:=]\s*\"?)([^\"\s,&}]+)", re.I),
    re.compile(r"((?:client_secret|api_token|api_key|authorization)\"?\s*[:=]\s*\"?)([^\"\s,&}]+)", re.I),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-_.]+)", re.I),
]


def redact(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups == 1:
            out = pat.sub("***REDACTED***", out)
        else:
            out = pat.sub(lambda m: m.group(1) + "***REDACTED***", out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("job_id", "event", "state", "provider", "duration_ms", "detail"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = redact(val) if isinstance(val, str) else val
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        job = getattr(record, "job_id", None)
        event = getattr(record, "event", None)
        prefix = f"{ts} "
        if job:
            prefix += f"[JOB {job}] "
        msg = redact(record.getMessage())
        if event:
            msg = f"{event} {msg}".strip()
        return prefix + msg


_configured = False


def setup_logging(logs_dir: Path, level: str = "INFO", json_file: str = "app.jsonl") -> None:
    global _configured
    if _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(HumanFormatter())
    root.addHandler(console)

    fh = logging.FileHandler(logs_dir / json_file, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, message: str = "",
              job_id: Optional[str] = None, **extra: Any) -> None:
    logger.info(message, extra={"event": event, "job_id": job_id, **extra})
