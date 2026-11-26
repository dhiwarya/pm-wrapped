"""Read and write functions for hand tables (Design §4, Part 2 §6-2).

Only the web process imports this module. A future run process must never import
it (R-H1, property test PR-2).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from .db import transaction
from .retention import forget_project

COLOURS = ("VIOLET", "BLUE", "TANGERINE", "PINK", "LIME")
PROJECT_STATES = ("ACTIVE", "PAUSED", "ARCHIVED")
HEALTH_VALUES = ("ON_TRACK", "AT_RISK", "LATE")
OUTCOMES = ("PASS", "CONDITIONAL_PASS", "REWORK")
ITEM_HAND_STATES = ("", "MET", "WAIVED")
TASK_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "DONE", "BLOCKED", "DEFERRED")

PROJECT_KEY_RE = re.compile(r"[A-Z0-9][A-Z0-9-]{1,11}")
GATE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,15}")
TASK_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,23}")


class InputError(ValueError):
    """A value the user typed cannot be stored. The message is shown to the user."""


# ---------------------------------------------------------------- input checks

def _text(value: object, label: str, *, required: bool = False, max_len: int = 2000) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise InputError(f"{label} is required.")
    if len(text) > max_len:
        raise InputError(f"{label} is longer than {max_len} characters.")
    return text


def _date(value: object, label: str, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise InputError(f"{label} is required.")
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise InputError(f"{label} must be a date like 2026-12-04.") from None


def _choice(value: object, allowed: tuple[str, ...], label: str) -> str:
    text = "" if value is None else str(value)
    if text not in allowed:
        raise InputError(f"{label} is not a valid choice.")
    return text


def _https_url(value: object) -> str:
    url = _text(value, "Evidence link", max_len=500)
    if url and not url.startswith("https://"):
        raise InputError("Evidence link must start with https://.")
    return url


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise InputError("That item no longer exists.")
    return row


# ---------------------------------------------------------------- settings

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _put_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "WHERE setting.value IS NOT excluded.value",
        (key, value),
    )


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        _put_setting(conn, key, value)


# ---------------------------------------------------------------- projects

_PROJECT_SELECT = "SELECT p.*, s.colour FROM project p JOIN project_setting s ON s.project_id = p.id"


def list_projects(conn: sqlite3.Connection, states: tuple[str, ...] = ("ACTIVE", "PAUSED")) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(states))
    return conn.execute(
        f"{_PROJECT_SELECT} WHERE p.state IN ({marks}) ORDER BY p.name COLLATE NOCASE", states
    ).fetchall()


def get_project(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(f"{_PROJECT_SELECT} WHERE p.key = ?", (key,)).fetchone()


def _new_id(conn, table):
    # An archived source or project ID remains a citation in historical records.
    return conn.execute(f"SELECT max(coalesce((SELECT max(id) FROM {table}), 0), "
                        "coalesce((SELECT max(row_id) FROM hand_change_log WHERE table_name = ?), 0)) + 1", (table,)).fetchone()[0]


def create_project(
    conn: sqlite3.Connection, key: object, name: object, description: object = "", scope_note: object = ""
) -> int:
    key_text = _text(key, "Key", required=True, max_len=12).upper()
    if not PROJECT_KEY_RE.fullmatch(key_text):
        raise InputError("Key must be 2 to 12 capital letters, digits or dashes, starting with a letter or digit.")
    name_text = _text(name, "Name", required=True, max_len=120)
    description_text = _text(description, "Description", max_len=2000)
    scope_text = _text(scope_note, "Scope", max_len=4000)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM project WHERE key = ?", (key_text,)).fetchone():
            raise InputError(f"A project with key {key_text} exists already.")
        # Design §6-9: the next colour in a fixed order, kept for life. The counter
        # only grows, so deleting a project never shifts the colours of others.
        index = int(get_setting(conn, "colour_next", "0") or 0)
        pid = _new_id(conn, "project")
        conn.execute("INSERT INTO project(id, key, name, description, scope_note) VALUES (?, ?, ?, ?, ?)",
                     (pid, key_text, name_text, description_text, scope_text))
        conn.execute(
            "INSERT INTO project_setting(project_id, colour) VALUES (?, ?)", (pid, COLOURS[index % len(COLOURS)])
        )
        _put_setting(conn, "colour_next", str(index + 1))
    return int(pid)


def update_project(
    conn: sqlite3.Connection, project_id: int, name: object, description: object, scope_note: object
) -> None:
    values = (
        _text(name, "Name", required=True, max_len=120),
        _text(description, "Description", max_len=2000),
        _text(scope_note, "Scope", max_len=4000),
        project_id,
    )
    with transaction(conn):
        conn.execute("UPDATE project SET name = ?, description = ?, scope_note = ? WHERE id = ?", values)


def set_project_state(conn: sqlite3.Connection, project_id: int, state: object) -> None:
    value = _choice(state, PROJECT_STATES, "State")
    with transaction(conn):
        conn.execute("UPDATE project SET state = ? WHERE id = ?", (value, project_id))


def set_project_colour(conn: sqlite3.Connection, project_id: int, colour: object) -> None:
    value = _choice(colour, COLOURS, "Colour")
    with transaction(conn):
        conn.execute("UPDATE project_setting SET colour = ? WHERE project_id = ?", (value, project_id))


def delete_project(conn: sqlite3.Connection, project_id: int, typed_key: object) -> list[str]:
    """Part 3 §12-4: a separate action that needs the project key typed in."""
    with transaction(conn):
        row = _one(conn, "SELECT id, key, name FROM project WHERE id = ?", (project_id,))
        if _text(typed_key, "Key").upper() != row["key"]:
            raise InputError(f"Type the project key {row['key']} to delete the project.")
        days = forget_project(conn, row)
        conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
        return days


# ---------------------------------------------------------------- gates

def list_gates(conn: sqlite3.Connection, project_id: int, *, include_closed: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM gate WHERE project_id = ?"
    if not include_closed:
        sql += " AND closed_at IS NULL"
    return conn.execute(sql + " ORDER BY date, key", (project_id,)).fetchall()


def get_gate(conn: sqlite3.Connection, project_id: int, gate_id: int) -> sqlite3.Row:
    return _one(conn, "SELECT * FROM gate WHERE id = ? AND project_id = ?", (gate_id, project_id))


def create_gate(
    conn: sqlite3.Connection, project_id: int, key: object, name: object, gate_date: object, description: object = ""
) -> int:
    key_text = _text(key, "Gate key", required=True, max_len=16)
    if not GATE_KEY_RE.fullmatch(key_text):
        raise InputError("Gate key must be 1 to 16 letters, digits, dots, dashes or underscores.")
    values = (
        project_id,
        key_text,
        _text(name, "Gate name", required=True, max_len=120),
        _date(gate_date, "Gate date"),
        _text(description, "Description", max_len=2000),
    )
    with transaction(conn):
        if conn.execute("SELECT 1 FROM gate WHERE project_id = ? AND key = ?", (project_id, key_text)).fetchone():
            raise InputError(f"Gate key {key_text} is used already in this project. A gate key is never reused.")
        gid = conn.execute(
            "INSERT INTO gate(project_id, key, name, date, description) VALUES (?, ?, ?, ?, ?)", values
        ).lastrowid
    return int(gid)


def update_gate(conn: sqlite3.Connection, gate_id: int, name: object, gate_date: object, description: object) -> None:
    values = (
        _text(name, "Gate name", required=True, max_len=120),
        _date(gate_date, "Gate date"),
        _text(description, "Description", max_len=2000),
        gate_id,
    )
    with transaction(conn):
        conn.execute("UPDATE gate SET name = ?, date = ?, description = ? WHERE id = ?", values)


def close_gate(conn: sqlite3.Connection, gate_id: int) -> None:
    """Gates are closed and hidden, never deleted, so the record stays (Part 2 §7-2)."""
    with transaction(conn):
        conn.execute(
            "UPDATE gate SET closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND closed_at IS NULL",
            (gate_id,),
        )


def record_outcome(
    conn: sqlite3.Connection,
    gate_id: int,
    outcome: object,
    note: object = "",
    open_scope: object = "",
    open_owner: object = "",
    close_date: object = "",
) -> None:
    value = _choice(outcome, ("", *OUTCOMES), "Outcome")
    note_text = _text(note, "Note", max_len=2000)
    if value == "CONDITIONAL_PASS":
        scope = _text(open_scope, "Open scope", required=True, max_len=2000)
        owner = _text(open_owner, "Owner of the open scope", required=True, max_len=120)
        closes = _date(close_date, "Close date")
    else:
        scope, owner, closes = "", "", ""
    with transaction(conn):
        conn.execute(
            "UPDATE gate SET outcome = ?, outcome_note = ?, open_scope = ?, open_owner = ?, close_date = ? "
            "WHERE id = ?",
            (value, note_text, scope, owner, closes, gate_id),
        )


# ---------------------------------------------------------------- confirmations

def add_confirmation(conn: sqlite3.Connection, gate_id: int, value: object, note: object = "", *,
                     proposal_id: int | None = None) -> int:
    """R-C1: only a user action creates a confirmation. Rows are never changed."""
    health = _choice(value, HEALTH_VALUES, "Health")
    note_text = _text(note, "Note", max_len=1000)
    with transaction(conn):
        cid = conn.execute(
            "INSERT INTO confirmation(gate_id, value, note, answered_proposal_id) VALUES (?, ?, ?, ?)",
            (gate_id, health, note_text, proposal_id)
        ).lastrowid
    return int(cid)


def latest_confirmations(conn: sqlite3.Connection, gate_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not gate_ids:
        return {}
    marks = ",".join("?" * len(gate_ids))
    rows = conn.execute(
        f"SELECT * FROM confirmation WHERE id IN "
        f"(SELECT max(id) FROM confirmation WHERE gate_id IN ({marks}) GROUP BY gate_id)",
        tuple(gate_ids),
    ).fetchall()
    return {row["gate_id"]: row for row in rows}


def confirmation_history(conn: sqlite3.Connection, gate_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM confirmation WHERE gate_id = ? ORDER BY id DESC LIMIT ?", (gate_id, limit)
    ).fetchall()


# ---------------------------------------------------------------- evidence items

def list_items(conn: sqlite3.Connection, gate_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    result: dict[int, list[sqlite3.Row]] = {gid: [] for gid in gate_ids}
    if not gate_ids:
        return result
    marks = ",".join("?" * len(gate_ids))
    rows = conn.execute(
        f"SELECT * FROM evidence_item WHERE gate_id IN ({marks}) AND closed_at IS NULL ORDER BY sort, id",
        tuple(gate_ids),
    ).fetchall()
    for row in rows:
        result[row["gate_id"]].append(row)
    return result


def get_item(conn: sqlite3.Connection, project_id: int, item_id: int) -> sqlite3.Row:
    return _one(
        conn,
        "SELECT i.* FROM evidence_item i JOIN gate g ON g.id = i.gate_id WHERE i.id = ? AND g.project_id = ?",
        (item_id, project_id),
    )


def create_item(conn: sqlite3.Connection, gate_id: int, text: object) -> int:
    item_text = _text(text, "Evidence item", required=True, max_len=500)
    with transaction(conn):
        top = conn.execute("SELECT coalesce(max(sort), 0) FROM evidence_item WHERE gate_id = ?", (gate_id,)).fetchone()
        iid = conn.execute(
            "INSERT INTO evidence_item(gate_id, text, sort) VALUES (?, ?, ?)", (gate_id, item_text, top[0] + 1)
        ).lastrowid
    return int(iid)


def update_item_text(conn: sqlite3.Connection, item_id: int, text: object) -> None:
    item_text = _text(text, "Evidence item", required=True, max_len=500)
    with transaction(conn):
        conn.execute("UPDATE evidence_item SET text = ? WHERE id = ?", (item_text, item_id))


def set_item_state(conn: sqlite3.Connection, item_id: int, state: object, note: object = "", url: object = "") -> None:
    """R-E1: MET and WAIVED are set by the user only. WAIVED needs a note."""
    value = _choice(state, ITEM_HAND_STATES, "State")
    note_text = _text(note, "Note", max_len=1000)
    link = _https_url(url)
    if value == "WAIVED" and not note_text:
        raise InputError("Write a note to waive an item: say why it no longer applies.")
    with transaction(conn):
        conn.execute(
            "UPDATE evidence_item SET hand_state = ?, note = ?, evidence_url = ? WHERE id = ?",
            (value, note_text, link, item_id),
        )


def remove_item(conn: sqlite3.Connection, item_id: int) -> str:
    """A MET item is closed and hidden, never deleted. Others are deleted."""
    with transaction(conn):
        row = _one(conn, "SELECT hand_state FROM evidence_item WHERE id = ?", (item_id,))
        if row["hand_state"] == "MET":
            conn.execute(
                "UPDATE evidence_item SET closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?", (item_id,)
            )
            return "closed"
        conn.execute("DELETE FROM evidence_item WHERE id = ?", (item_id,))
        return "deleted"


def move_item(conn: sqlite3.Connection, item_id: int, direction: object) -> None:
    step = {"up": -1, "down": 1}.get(str(direction))
    if step is None:
        raise InputError("Direction is not valid.")
    with transaction(conn):
        row = _one(conn, "SELECT gate_id FROM evidence_item WHERE id = ?", (item_id,))
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM evidence_item WHERE gate_id = ? AND closed_at IS NULL ORDER BY sort, id",
                (row["gate_id"],),
            )
        ]
        pos = ids.index(item_id)
        other = pos + step
        if 0 <= other < len(ids):
            ids[pos], ids[other] = ids[other], ids[pos]
        for sort, iid in enumerate(ids, start=1):
            conn.execute("UPDATE evidence_item SET sort = ? WHERE id = ? AND sort IS NOT ?", (sort, iid, sort))


# ---------------------------------------------------------------- people

def list_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM person ORDER BY display_name COLLATE NOCASE").fetchall()


def _person_id(conn: sqlite3.Connection, name: object) -> int | None:
    text = _text(name, "Owner", max_len=80)
    if not text:
        return None
    row = conn.execute("SELECT id FROM person WHERE display_name = ? COLLATE NOCASE", (text,)).fetchone()
    if row:
        return int(row["id"])
    return int(conn.execute("INSERT INTO person(display_name) VALUES (?)", (text,)).lastrowid)


def add_person(conn: sqlite3.Connection, name: object) -> int:
    text = _text(name, "Name", required=True, max_len=80)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM person WHERE display_name = ? COLLATE NOCASE", (text,)).fetchone():
            raise InputError(f"{text} is in the list already.")
        return int(conn.execute("INSERT INTO person(display_name) VALUES (?)", (text,)).lastrowid)


def rename_person(conn: sqlite3.Connection, person_id: int, name: object) -> None:
    text = _text(name, "Name", required=True, max_len=80)
    with transaction(conn):
        clash = conn.execute(
            "SELECT 1 FROM person WHERE display_name = ? COLLATE NOCASE AND id <> ?", (text, person_id)
        ).fetchone()
        if clash:
            raise InputError(f"Another person is called {text} already.")
        conn.execute("UPDATE person SET display_name = ? WHERE id = ?", (text, person_id))


# ---------------------------------------------------------------- hand tasks

def list_tasks(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, p.display_name AS owner FROM hand_task t LEFT JOIN person p ON p.id = t.owner_person_id "
        "WHERE t.project_id = ? ORDER BY t.key",
        (project_id,),
    ).fetchall()


def get_task(conn: sqlite3.Connection, project_id: int, task_id: int) -> sqlite3.Row:
    return _one(conn, "SELECT * FROM hand_task WHERE id = ? AND project_id = ?", (task_id, project_id))


def _task_values(conn: sqlite3.Connection, project_id: int, fields: dict) -> dict:
    gate_key = _text(fields.get("gate_key"), "Gate", max_len=16)
    if gate_key and not conn.execute(
        "SELECT 1 FROM gate WHERE project_id = ? AND key = ? AND closed_at IS NULL", (project_id, gate_key)
    ).fetchone():
        raise InputError(f"There is no open gate {gate_key} in this project.")
    start = _date(fields.get("start"), "Start", required=False)
    finish = _date(fields.get("finish"), "Finish", required=False)
    if start and finish and finish < start:
        raise InputError("Finish is before start.")
    depends = [k for k in re.split(r"[;,\s]+", _text(fields.get("depends_on"), "Depends on", max_len=500)) if k]
    for key in depends:
        if not TASK_KEY_RE.fullmatch(key):
            raise InputError(f"{key} is not a task key.")
    return {
        "title": _text(fields.get("title"), "Title", required=True, max_len=200),
        "owner_person_id": _person_id(conn, fields.get("owner")),
        "gate_key": gate_key,
        "start": start,
        "finish": finish,
        "status": _choice(fields.get("status") or "NOT_STARTED", TASK_STATUSES, "Status"),
        "depends_on": "; ".join(depends),
        "note": _text(fields.get("note"), "Note", max_len=1000),
    }


def create_task(conn: sqlite3.Connection, project_id: int, key: object, **fields: object) -> int:
    key_text = _text(key, "Task key", required=True, max_len=24)
    if not TASK_KEY_RE.fullmatch(key_text):
        raise InputError("Task key must be 1 to 24 letters, digits, dots, dashes or underscores.")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM hand_task WHERE project_id = ? AND key = ?", (project_id, key_text)).fetchone():
            raise InputError(f"Task key {key_text} is used already in this project.")
        values = _task_values(conn, project_id, fields)
        tid = conn.execute(
            "INSERT INTO hand_task(project_id, key, title, owner_person_id, gate_key, start, finish, status, "
            "depends_on, note) VALUES (:project_id, :key, :title, :owner_person_id, :gate_key, :start, :finish, "
            ":status, :depends_on, :note)",
            {"project_id": project_id, "key": key_text, **values},
        ).lastrowid
    return int(tid)


def update_task(conn: sqlite3.Connection, project_id: int, task_id: int, **fields: object) -> None:
    with transaction(conn):
        values = _task_values(conn, project_id, fields)
        conn.execute(
            "UPDATE hand_task SET title = :title, owner_person_id = :owner_person_id, gate_key = :gate_key, "
            "start = :start, finish = :finish, status = :status, depends_on = :depends_on, note = :note "
            "WHERE id = :id AND project_id = :project_id",
            {"id": task_id, "project_id": project_id, **values},
        )


def delete_task(conn: sqlite3.Connection, project_id: int, task_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM hand_task WHERE id = ? AND project_id = ?", (task_id, project_id))


# ---------------------------------------------------------------- holidays

def list_holidays(conn: sqlite3.Connection, project_id: int | None = None) -> list[sqlite3.Row]:
    if project_id is None:
        return conn.execute("SELECT * FROM holiday WHERE project_id IS NULL ORDER BY date").fetchall()
    return conn.execute("SELECT * FROM holiday WHERE project_id = ? ORDER BY date", (project_id,)).fetchall()


def holiday_dates(conn: sqlite3.Connection, project_id: int) -> set[date]:
    rows = conn.execute(
        "SELECT date FROM holiday WHERE project_id IS NULL OR project_id = ?", (project_id,)
    ).fetchall()
    return {date.fromisoformat(r["date"]) for r in rows}


def add_holiday(conn: sqlite3.Connection, day: object, label: object, project_id: int | None = None) -> int:
    values = (_date(day, "Date"), _text(label, "Label", max_len=80), project_id)
    with transaction(conn):
        clash = conn.execute(
            "SELECT 1 FROM holiday WHERE date = ? AND ifnull(project_id, 0) = ifnull(?, 0)", (values[0], project_id)
        ).fetchone()
        if clash:
            raise InputError(f"{values[0]} is in the list already.")
        return int(conn.execute("INSERT INTO holiday(date, label, project_id) VALUES (?, ?, ?)", values).lastrowid)


def remove_holiday(conn: sqlite3.Connection, holiday_id: int, project_id: int | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "DELETE FROM holiday WHERE id = ? AND ifnull(project_id, 0) = ifnull(?, 0)", (holiday_id, project_id)
        )


# ---------------------------------------------------------------- sources (Phase 2)

SOURCE_ROLES = ("DOCS", "CODE", "OTHER")
SOURCE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
GITHUB_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")


def _source_location(value: object) -> tuple[str, str]:
    """(git_url, github_repo) from a GitHub URL, any https git URL, or an absolute host path (F1 = A)."""
    text = _text(value, "Repository", required=True, max_len=500)
    m = GITHUB_URL_RE.fullmatch(text)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}.git", f"{m.group(1)}/{m.group(2)}"
    if text.startswith("https://"):
        return text, ""
    if text.startswith("/"):
        return text.rstrip("/") or "/", ""
    raise InputError("A source is a GitHub URL, an https:// git URL, or an absolute path to a git repository on this machine.")


def suggest_source_key(git_url: str) -> str:
    name = git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    key = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-._") or "source"
    return key[:40]


def list_sources(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()


def get_source(conn: sqlite3.Connection, project_id: int, source_id: int) -> sqlite3.Row:
    return _one(conn, "SELECT * FROM source WHERE id = ? AND project_id = ?", (source_id, project_id))


def add_source(conn: sqlite3.Connection, project_id: int, location: object, *, key: object = "", role: object = "DOCS",
               branches: object = "") -> int:
    git_url, repo = _source_location(location)
    role_value = _choice(role or "DOCS", SOURCE_ROLES, "Role")
    branch_list = ", ".join(b for b in (x.strip() for x in _text(branches, "Branches", max_len=300).split(",")) if b)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM source WHERE project_id = ? AND git_url = ?", (project_id, git_url)).fetchone():
            raise InputError("This repository is a source of the project already.")
        key_text = _text(key, "Source key", max_len=40) or suggest_source_key(git_url)
        base, n = key_text, 2
        while conn.execute("SELECT 1 FROM source WHERE project_id = ? AND key = ?", (project_id, key_text)).fetchone():
            key_text, n = f"{base}-{n}"[:40], n + 1
        if not SOURCE_KEY_RE.fullmatch(key_text):
            raise InputError("Source key must be letters, digits, dots, dashes or underscores.")
        source_id = _new_id(conn, "source")
        conn.execute("INSERT INTO source(id, project_id, key, role, git_url, github_repo, branches) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (source_id, project_id, key_text, role_value, git_url, repo, branch_list))
        return source_id


def update_source(conn: sqlite3.Connection, project_id: int, source_id: int, *, role: object, branches: object, enabled: object) -> None:
    role_value = _choice(role, SOURCE_ROLES, "Role")
    branch_list = ", ".join(b for b in (x.strip() for x in _text(branches, "Branches", max_len=300).split(",")) if b)
    with transaction(conn):
        conn.execute("UPDATE source SET role = ?, branches = ?, enabled = ? WHERE id = ? AND project_id = ?",
                     (role_value, branch_list, 1 if str(enabled) in ("1", "on", "true") else 0, source_id, project_id))


def remove_source(conn: sqlite3.Connection, project_id: int, source_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM source WHERE id = ? AND project_id = ?", (source_id, project_id))


def unique_project_key(conn: sqlite3.Connection, wanted: str) -> str:
    base = re.sub(r"[^A-Z0-9-]", "", wanted.upper())[:12] or "PROJ"
    if len(base) < 2:
        base = (base + "PROJ")[:12]
    key, n = base, 2
    while conn.execute("SELECT 1 FROM project WHERE key = ?", (key,)).fetchone():
        suffix = str(n)
        key, n = base[: 12 - len(suffix)] + suffix, n + 1
    return key


# ---------------------------------------------------------------- reading settings of a project

def project_setting(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    return _one(conn, "SELECT * FROM project_setting WHERE project_id = ?", (project_id,))


def _source_id_or_none(conn: sqlite3.Connection, project_id: int, value: object) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if not text.isdigit() or not conn.execute("SELECT 1 FROM source WHERE id = ? AND project_id = ?", (int(text), project_id)).fetchone():
        raise InputError("Pick one of the project's sources.")
    return int(text)


def _relative_path(value: object, label: str) -> str:
    path = _text(value, label, max_len=300).lstrip("./") if value else ""
    if path.startswith("/") or ".." in path.split("/"):
        raise InputError(f"{label} must be a path inside the repository.")
    return path


def set_reading(conn: sqlite3.Connection, project_id: int, *, wbs_source: object = "", wbs_path: object = "", wbs_mapping: object = "",
                dashboard_source: object = "", dashboard_path: object = "", branch_pattern: object = "", task_key_pattern: object = "",
                prose_reading: object = "1") -> None:
    mapping = _text(wbs_mapping, "Column mapping", max_len=8000)
    if mapping:
        import json

        try:
            parsed = json.loads(mapping)
        except json.JSONDecodeError:
            raise InputError("The column mapping is not valid JSON.") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("columns"), dict):
            raise InputError("The column mapping needs a \"columns\" object.")
    pattern = _text(task_key_pattern, "Task-key pattern", max_len=200)
    if pattern:
        try:
            re.compile(pattern)
        except re.error:
            raise InputError("The task-key pattern is not a valid regular expression.") from None
    values = {
        "wbs_source_id": _source_id_or_none(conn, project_id, wbs_source),
        "wbs_path": _relative_path(wbs_path, "WBS path"),
        "wbs_mapping": mapping,
        "dashboard_source_id": _source_id_or_none(conn, project_id, dashboard_source),
        "dashboard_json_path": _relative_path(dashboard_path, "Dashboard file path"),
        "branch_unit_pattern": _text(branch_pattern, "Branch pattern", max_len=200),
        "task_key_pattern": pattern,
        "prose_reading": 1 if str(prose_reading) in ("1", "on", "true") else 0,
        "project_id": project_id,
    }
    if values["wbs_path"] and values["wbs_source_id"] is None:
        raise InputError("Pick the source that holds the WBS file.")
    if values["dashboard_json_path"] and values["dashboard_source_id"] is None:
        raise InputError("Pick the source that holds the dashboard file.")
    with transaction(conn):
        conn.execute(
            "UPDATE project_setting SET wbs_source_id = :wbs_source_id, wbs_path = :wbs_path, wbs_mapping = :wbs_mapping, "
            "dashboard_source_id = :dashboard_source_id, dashboard_json_path = :dashboard_json_path, "
            "branch_unit_pattern = :branch_unit_pattern, task_key_pattern = :task_key_pattern, prose_reading = :prose_reading "
            "WHERE project_id = :project_id",
            values,
        )


def _update_setting_fields(conn: sqlite3.Connection, project_id: int, **fields: object) -> None:
    allowed = {"wbs_source_id", "wbs_path", "wbs_mapping", "dashboard_source_id", "dashboard_json_path", "branch_unit_pattern", "task_key_pattern"}
    assert set(fields) <= allowed
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE project_setting SET {sets} WHERE project_id = ?", (*fields.values(), project_id))


# ---------------------------------------------------------------- evidence links and aliases

LINK_KINDS = ("TASK", "TASK_PREFIX", "UNIT", "PR", "PR_SEARCH")


def list_links(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {i: [] for i in item_ids}
    if item_ids:
        marks = ",".join("?" * len(item_ids))
        for row in conn.execute(f"SELECT * FROM evidence_link WHERE evidence_item_id IN ({marks}) ORDER BY id", tuple(item_ids)):
            out[row["evidence_item_id"]].append(row)
    return out


def add_link(conn: sqlite3.Connection, item_id: int, kind: object, value: object) -> int:
    kind_value = _choice(kind, LINK_KINDS, "Link kind")
    text = _text(value, "Link value", required=True, max_len=200)
    with transaction(conn):
        return int(conn.execute("INSERT INTO evidence_link(evidence_item_id, kind, value) VALUES (?, ?, ?)",
                                (item_id, kind_value, text)).lastrowid)


def remove_link(conn: sqlite3.Connection, project_id: int, link_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "DELETE FROM evidence_link WHERE id = ? AND evidence_item_id IN "
            "(SELECT i.id FROM evidence_item i JOIN gate g ON g.id = i.gate_id WHERE g.project_id = ?)",
            (link_id, project_id),
        )


def add_alias(conn: sqlite3.Connection, person_id: int, kind: object, value: object) -> None:
    kind_value = _choice(kind, ("GITHUB", "GIT", "DOC", "WBS"), "Alias kind")
    text = _text(value, "Alias", required=True, max_len=200)
    with transaction(conn):
        clash = conn.execute("SELECT person_id FROM person_alias WHERE kind = ? AND value = ? COLLATE NOCASE", (kind_value, text)).fetchone()
        if clash and clash["person_id"] != person_id:
            raise InputError(f"{text} belongs to another person already.")
        if not clash:
            conn.execute("INSERT INTO person_alias(person_id, kind, value) VALUES (?, ?, ?)", (person_id, kind_value, text))


def attach_name(conn: sqlite3.Connection, name: object, kind: object, person_name: object) -> None:
    """Attach a name the run could not match to a person, new or existing (Design §6-4)."""
    with transaction(conn):
        pid = _person_id(conn, person_name)
        if pid is None:
            raise InputError("Name the person to attach it to.")
        add_alias(conn, pid, kind, name)


# ---------------------------------------------------------------- onboarding decisions (Design §3-4, Part 2 §6-7)

def _proposal(conn: sqlite3.Connection, project_id: int, proposal_id: int) -> sqlite3.Row:
    return _one(conn, "SELECT * FROM onboarding_proposal WHERE id = ? AND project_id = ?", (proposal_id, project_id))


def proposal_decision(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM onboarding_decision WHERE proposal_id = ? ORDER BY id DESC LIMIT 1", (proposal_id,)).fetchone()


def decide_proposal(conn: sqlite3.Connection, project_id: int, proposal_id: int, action: object, edits: dict | None = None) -> str:
    """Accept, accept with edits, reject, or undo a rejection. Accepting writes hand rows and the
    decision in one transaction (R-O1). Returns a message for the user."""
    import json

    action_value = _choice(action, ("accept", "edit", "reject", "undo"), "Action")
    with transaction(conn):
        p = _proposal(conn, project_id, proposal_id)
        last = proposal_decision(conn, proposal_id)
        if action_value == "undo":
            if not last or last["decision"] != "REJECTED":
                raise InputError("Only a rejection can be undone.")
            conn.execute("INSERT INTO onboarding_decision(proposal_id, decision) VALUES (?, 'UNDONE')", (proposal_id,))
            return "Rejection undone."
        if last and last["decision"] in ("ACCEPTED", "EDITED"):
            raise InputError("This proposal was accepted already.")
        if last and last["decision"] == "REJECTED":
            raise InputError("This proposal was rejected already. Undo its rejection to review it again.")
        if action_value == "reject":
            conn.execute("INSERT INTO onboarding_decision(proposal_id, decision) VALUES (?, 'REJECTED')", (proposal_id,))
            return "Rejected."
        payload = json.loads(p["payload"])
        if action_value == "edit":
            payload.update({k: v for k, v in (edits or {}).items() if k in payload})
        created = _apply_proposal(conn, project_id, p["kind"], payload)
        conn.execute(
            "INSERT INTO onboarding_decision(proposal_id, decision, edited_payload, created_rows) VALUES (?, ?, ?, ?)",
            (proposal_id, "EDITED" if action_value == "edit" else "ACCEPTED",
             json.dumps(payload, ensure_ascii=False) if action_value == "edit" else "", " ".join(created)),
        )
        return "Accepted." if action_value == "accept" else "Accepted with your edits."


def _apply_proposal(conn: sqlite3.Connection, project_id: int, kind: str, payload: dict) -> list[str]:
    import json

    if kind == "PROJECT":
        current = _one(conn, "SELECT * FROM project WHERE id = ?", (project_id,))
        update_project(conn, project_id, payload.get("name") or current["name"],
                       payload.get("description") or current["description"], payload.get("scope_note") or current["scope_note"])
        return [f"project:{project_id}"]
    if kind == "GATE":
        gid = create_gate(conn, project_id, payload.get("key"), payload.get("name"), payload.get("date"), payload.get("description", ""))
        return [f"gate:{gid}"]
    if kind == "EVIDENCE":
        gate = conn.execute("SELECT id FROM gate WHERE project_id = ? AND key = ? AND closed_at IS NULL",
                            (project_id, str(payload.get("gate_key", "")))).fetchone()
        if gate is None:
            raise InputError(f"Accept gate {payload.get('gate_key')} first, or edit this item to name an open gate.")
        return [f"evidence_item:{create_item(conn, gate['id'], payload.get('text'))}"]
    if kind in ("WBS", "DASHBOARD_FILE"):
        source = conn.execute("SELECT id FROM source WHERE project_id = ? AND key = ?", (project_id, str(payload.get("source_key", "")))).fetchone()
        if source is None:
            raise InputError("The source of this file is no longer in the project.")
        path = _relative_path(payload.get("path"), "Path")
        if kind == "WBS":
            mapping = payload.get("mapping")
            _update_setting_fields(conn, project_id, wbs_source_id=source["id"], wbs_path=path,
                                   wbs_mapping=mapping if isinstance(mapping, str) else json.dumps(mapping))
        else:
            _update_setting_fields(conn, project_id, dashboard_source_id=source["id"], dashboard_json_path=path)
        return [f"project_setting:{project_id}"]
    if kind == "PATTERN":
        which = payload.get("which")
        pattern = _text(payload.get("pattern"), "Pattern", required=True, max_len=200)
        if which == "TASK_KEY":
            try:
                re.compile(pattern)
            except re.error:
                raise InputError("The task-key pattern is not a valid regular expression.") from None
            _update_setting_fields(conn, project_id, task_key_pattern=pattern)
        elif which == "BRANCH_UNIT":
            _update_setting_fields(conn, project_id, branch_unit_pattern=pattern)
        else:
            raise InputError("Unknown pattern kind.")
        return [f"project_setting:{project_id}"]
    if kind == "PERSON":
        pid = _person_id(conn, payload.get("display_name"))
        if pid is None:
            raise InputError("A person needs a name.")
        for alias in payload.get("aliases", []) or []:
            if isinstance(alias, dict) and alias.get("value"):
                add_alias(conn, pid, alias.get("kind", "DOC"), alias["value"])
        return [f"person:{pid}"]
    if kind == "HOLIDAY":
        return [f"holiday:{add_holiday(conn, payload.get('date'), payload.get('label', ''), project_id)}"]
    raise InputError("Unknown proposal kind.")
