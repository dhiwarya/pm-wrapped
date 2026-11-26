"""Online SQLite backup and restore into an empty data directory."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

COPY_NAME = re.compile(r"pmd-\d{8}T\d{12}Z\.sqlite3")
KEEP = 14


@contextmanager
def _staging(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pmd-copy-", suffix=".sqlite3", dir=directory)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _copy(source: Path, destination: Path, *, restore: bool = False) -> None:
    # Read-only mode includes committed WAL pages and refuses missing files.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, \
         closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256)
        dst.execute("PRAGMA journal_mode = DELETE")
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("the database failed its SQLite integrity check")
        tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"schema_version", "project", "hand_change_log"} <= tables:
            raise ValueError("the file is not a PM Dash database")
        if dst.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("the database has invalid references")
        if restore:
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            for table in ("run", "onboarding"):
                if table in tables:
                    dst.execute(f"UPDATE {table} SET result = 'FAILED', ended_at = ?, error = ? WHERE result = 'RUNNING'",
                                (now, "This operation was in progress when the backup was taken."))
            dst.commit()
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def create(source: Path, *, now: datetime | None = None) -> Path:
    folder = source.parent / "backup"
    if folder.is_symlink():
        raise OSError("the backup directory is a symbolic link")
    name = f"pmd-{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%dT%H%M%S%fZ}.sqlite3"
    destination = folder / name
    with _staging(folder) as staged:
        _copy(source, staged)
        os.link(staged, destination)
    copies = sorted((path for path in folder.iterdir() if COPY_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()),
                    reverse=True)
    for path in copies[KEEP:]:
        path.unlink(missing_ok=True)
    return destination


def restore(source: Path, destination: Path) -> Path:
    siblings = tuple(destination.with_name(destination.name + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
    if any(path.exists() or path.is_symlink() for path in siblings):
        raise FileExistsError("the restore target contains a database or SQLite sidecar; choose an empty data directory")
    with _staging(destination.parent) as staged:
        _copy(source, staged, restore=True)
        os.link(staged, destination)  # publish atomically and never replace a live database
    return destination
