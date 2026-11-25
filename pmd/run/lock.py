"""One run or onboarding at a time (R-R1, Part 1 §2-2).

An exclusive ``flock`` on a file in the data directory. The kernel releases it when
the process ends, so a crashed run never leaves a stale lock.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class Busy(RuntimeError):
    pass


@contextmanager
def exclusive(path: Path, what: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            raise Busy(f"A run or onboarding is in progress ({holder}).") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{what}, pid {os.getpid()}, since {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
