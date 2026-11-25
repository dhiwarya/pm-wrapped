"""Connection, transactions and migrations (Detailed Design Part 1 §2-3, Part 3 §12-6)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

# Tables written by the user only (Design §4). Each gets change-log triggers, so no
# code path can change a hand row without leaving a record (Part 2 §6-2).
HAND_TABLES = (
    "project",
    "project_setting",
    "person",
    "gate",
    "evidence_item",
    "hand_task",
    "confirmation",
    "holiday",
    "setting",
    "source",
    "person_alias",
    "evidence_link",
    "onboarding_decision",
)

# Tables written by the run and onboarding processes only (Part 2 §6-3).
RUN_TABLES = (
    "run",
    "onboarding",
    "run_source",
    "run_problem",
    "request_log",
    "snapshot",
    "current_snapshot",
    "unit_state",
    "unit_stage_state",
    "task_state",
    "pr_state",
    "review_state",
    "issue_state",
    "fact",
    "blocker",
    "item_state",
    "unmatched_name",
    "onboarding_proposal",
    "proposal",
    "change",
    "digest",
    "model_read",
    "model_source",
)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


_savepoints = 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One transaction. Nested use becomes a savepoint, so a function that opens a
    transaction can be called inside a larger one (accepting a proposal, Part 2 §6-7)."""
    global _savepoints
    if conn.in_transaction:
        _savepoints += 1
        name = f"sp_{_savepoints}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply numbered migration files, each in one transaction. Returns versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    files = sorted(
        (f for f in resources.files("pmd.data.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied = []
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version in done:
            continue
        sql = f.read_text(encoding="utf-8")
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_version(version) VALUES ({version});\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if conn.execute("SELECT 1 FROM schema_version WHERE version = ?", (version,)).fetchone():
                continue  # another process finished this migration while we waited
            raise
        applied.append(version)
    with transaction(conn):
        ensure_change_log_triggers(conn)
    return applied


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def ensure_change_log_triggers(conn: sqlite3.Connection) -> None:
    """(Re)create insert, update and delete triggers that write ``hand_change_log``."""
    for table in HAND_TABLES:
        cols = _columns(conn, table)
        if not cols:
            continue
        new_obj = "json_object(" + ", ".join(f"'{c}', NEW.\"{c}\"" for c in cols) + ")"
        old_obj = "json_object(" + ", ".join(f"'{c}', OLD.\"{c}\"" for c in cols) + ")"
        insert_log = "INSERT INTO hand_change_log(table_name, row_id, field, old, new)"
        for kind in ("ins", "upd", "del"):
            conn.execute(f"DROP TRIGGER IF EXISTS log_{table}_{kind}")
        conn.execute(
            f"CREATE TRIGGER log_{table}_ins AFTER INSERT ON \"{table}\" BEGIN "
            f"{insert_log} VALUES ('{table}', NEW.rowid, '*', NULL, {new_obj}); END"
        )
        per_column = " ".join(
            f"{insert_log} SELECT '{table}', NEW.rowid, '{c}', OLD.\"{c}\", NEW.\"{c}\" "
            f"WHERE OLD.\"{c}\" IS NOT NEW.\"{c}\";"
            for c in cols
        )
        conn.execute(f"CREATE TRIGGER log_{table}_upd AFTER UPDATE ON \"{table}\" BEGIN {per_column} END")
        conn.execute(
            f"CREATE TRIGGER log_{table}_del AFTER DELETE ON \"{table}\" BEGIN "
            f"{insert_log} VALUES ('{table}', OLD.rowid, '*', {old_obj}, NULL); END"
        )


def open_store(path: Path) -> sqlite3.Connection:
    """Open the database file and bring it to the current schema."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = connect(path)
    migrate(conn)
    return conn
