"""Read and write functions for hand tables (Design §4, Part 2 §6-2).

Only the web process imports this module. A future run process must never import
it (R-H1, property test PR-2).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from .db import transaction

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
        pid = conn.execute(
            "INSERT INTO project(key, name, description, scope_note) VALUES (?, ?, ?, ?)",
            (key_text, name_text, description_text, scope_text),
        ).lastrowid
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


def delete_project(conn: sqlite3.Connection, project_id: int, typed_key: object) -> None:
    """Part 3 §12-4: a separate action that needs the project key typed in."""
    with transaction(conn):
        row = _one(conn, "SELECT key FROM project WHERE id = ?", (project_id,))
        if _text(typed_key, "Key").upper() != row["key"]:
            raise InputError(f"Type the project key {row['key']} to delete the project.")
        conn.execute("DELETE FROM project WHERE id = ?", (project_id,))


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

def add_confirmation(conn: sqlite3.Connection, gate_id: int, value: object, note: object = "") -> int:
    """R-C1: only a user action creates a confirmation. Rows are never changed."""
    health = _choice(value, HEALTH_VALUES, "Health")
    note_text = _text(note, "Note", max_len=1000)
    with transaction(conn):
        cid = conn.execute(
            "INSERT INTO confirmation(gate_id, value, note) VALUES (?, ?, ?)", (gate_id, health, note_text)
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
