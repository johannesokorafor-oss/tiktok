"""File stability detection: size settled + exclusively readable."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple


def can_open_exclusively(path: Path) -> bool:
    """True when the file can be opened for reading/appending.

    On Windows an open handle from the copying application makes the
    r+b open fail, which is exactly the signal we want.
    """
    try:
        with path.open("rb+"):
            return True
    except (PermissionError, OSError):
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
            return False
        except OSError:
            return False


@dataclass
class FileStamp:
    size: int
    mtime: float
    seen_at: float


class StabilityTracker:
    """Tracks files until their size/mtime stopped changing for `period` seconds."""

    def __init__(self, period: float = 4.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.period = period
        self.clock = clock
        self._stamps: Dict[str, FileStamp] = {}

    def forget(self, path: Path) -> None:
        self._stamps.pop(str(path), None)

    def is_stable(self, path: Path) -> Tuple[bool, str]:
        key = str(path)
        try:
            st = path.stat()
        except OSError as exc:
            self._stamps.pop(key, None)
            return False, f"not accessible: {exc.__class__.__name__}"
        if st.st_size <= 0:
            self._stamps[key] = FileStamp(st.st_size, st.st_mtime, self.clock())
            return False, "file is empty"
        prev = self._stamps.get(key)
        now = self.clock()
        if prev is None:
            # first sighting in this process: a file whose mtime is already older
            # than the stability period is settled (survives restarts / one-shot scans)
            age = time.time() - st.st_mtime
            if age >= self.period:
                self._stamps[key] = FileStamp(st.st_size, st.st_mtime, now - self.period)
                if not can_open_exclusively(path):
                    return False, "file is still locked by another process"
                return True, f"stable (mtime age {age:.1f}s)"
        if prev is None or prev.size != st.st_size or prev.mtime != st.st_mtime:
            prev = FileStamp(st.st_size, st.st_mtime, now)
            self._stamps[key] = prev
            if self.period > 0:
                return False, "size/mtime still changing"
        if now - prev.seen_at < self.period:
            return False, f"waiting {self.period - (now - prev.seen_at):.1f}s more"
        if not can_open_exclusively(path):
            return False, "file is still locked by another process"
        return True, "stable"
