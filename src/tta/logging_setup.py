"""Logging with secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Patterns that must never appear in logs.
_REDACT_PATTERNS = [
    re.compile(r"(act\.[A-Za-z0-9_\-!]{6,})"),          # TikTok access tokens
    re.compile(r"(rft\.[A-Za-z0-9_\-!]{6,})"),          # TikTok refresh tokens
    re.compile(r"(sk-[A-Za-z0-9_\-]{10,})"),             # OpenAI keys
    re.compile(r"((?:access_token|refresh_token|client_secret|api[_-]?key|authorization)\s*[=:]\s*)([^\s&\"',;]+)", re.I),
    re.compile(r"(Bearer\s+)([A-Za-z0-9._\-!]+)"),
]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    for pattern in _REDACT_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + "***REDACTED***", text)
        else:
            text = pattern.sub("***REDACTED***", text)
    return text


def setup_logging(logs_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("tta")
    if root.handlers:
        return root
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactingFilter())
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            logs_dir / "tta.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        fileh.addFilter(RedactingFilter())
        root.addHandler(fileh)
    return root
