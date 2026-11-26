"""Keep recent snapshots, weekly history, and dated work directories."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .db import transaction

SNAPSHOT_DAYS = 90
WORK_DAYS = 14
# Phase 2 tables have no snapshot foreign key. Phase 3 tables cascade from it.
SNAPSHOT_ROWS = ("unit_state", "unit_stage_state", "task_state", "pr_state", "review_state", "issue_state",
                 "fact", "blocker", "item_state", "unmatched_name")
WORK_DIR = re.compile(r"(?:onboard-)?[1-9][0-9]*")


def prune_snapshots(conn: sqlite3.Connection, local_now: datetime, keep_days: int) -> int:
    cutoff = local_now.date() - timedelta(days=keep_days)
    with transaction(conn):
        current = conn.execute("SELECT snapshot_id FROM current_snapshot").fetchone()
        if current is None:
            return 0
        weeks = set()
        expired = []
        rows = conn.execute("SELECT id, created_at, published_at FROM snapshot ORDER BY created_at DESC, id DESC")
        for row in rows:
            day = datetime.fromisoformat(row["created_at"]).astimezone(local_now.tzinfo).date()
            week = day.isocalendar()[:2]
            if day < cutoff and row["id"] != current[0] and (not row["published_at"] or week in weeks):
                expired.append((row["id"],))
            if row["published_at"]:
                weeks.add(week)
        for table in SNAPSHOT_ROWS:
            conn.executemany(f"DELETE FROM {table} WHERE snapshot_id = ?", expired)
        conn.executemany("DELETE FROM snapshot WHERE id = ?", expired)
    return len(expired)


def prune_work(root: Path, now: datetime, keep_days: int) -> None:
    if root.is_symlink() or not root.is_dir():
        return
    cutoff = (now - timedelta(days=keep_days)).timestamp()
    for child in root.iterdir():
        if WORK_DIR.fullmatch(child.name) and not child.is_symlink() and child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child)


def forget_project(conn: sqlite3.Connection, project) -> list[str]:
    """Remove derived data for a project whose key was confirmed for deletion."""
    pid = project["id"]
    days = []
    for digest in conn.execute("SELECT id, day, projects, changes, cards FROM digest").fetchall():
        projects = json.loads(digest["projects"])
        if projects:
            affected = pid in projects
        else:
            # Phase 3 kept project IDs in change records, and names in story cards.
            cards = json.loads(digest["cards"])
            affected = any(change.get("project_id") == pid for change in json.loads(digest["changes"])) or any(
                card.get("project_key") == project["key"] or
                (card["kind"] in ("DAYS", "DECISION", "CHANGES", "PROJECT") and card["title"] == project["name"]) or
                (card["kind"] == "MORE" and any(line.startswith(f"{project['name']} · ") for line in card["lines"]))
                for card in cards)
        if affected:
            conn.execute("DELETE FROM digest WHERE id = ?", (digest["id"],))
            days.append(digest["day"])

    for table in SNAPSHOT_ROWS:
        if table == "item_state":
            conn.execute("DELETE FROM item_state WHERE evidence_item_id IN (SELECT e.id FROM evidence_item e "
                         "JOIN gate g ON e.gate_id = g.id WHERE g.project_id = ?)", (pid,))
        else:
            conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
    conn.execute("DELETE FROM run_problem WHERE project_id = ?", (pid,))
    return days


def remove_mirrors(root: Path, source_ids) -> None:
    if root.is_symlink():
        raise OSError("the mirror directory is a symbolic link")
    for source_id in source_ids:
        target = root / f"{source_id}.git"
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def remove_digest_files(root: Path, days: list[str]) -> None:
    if root.is_symlink():
        raise OSError("the digest directory is a symbolic link")
    for day in days:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            (root / f"{day}.txt").unlink(missing_ok=True)
