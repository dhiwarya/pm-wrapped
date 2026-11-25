"""Read functions for hand tables, write functions for run tables (Part 1 §4, R-H1).

The run and onboarding processes import this module and never ``data.hand``. The
web process reads run tables through ``data.views`` and never imports this module.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .db import RUN_TABLES, transaction

SRC_COLUMNS = ("src_source_id", "src_commit", "src_path", "src_line_from", "src_line_to", "src_object", "src_number", "src_url")
CARRY_TABLES = ("unit_state", "unit_stage_state", "task_state", "pr_state", "review_state", "issue_state", "fact")


def stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- hand reads

def setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def projects(conn: sqlite3.Connection, key: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT p.*, s.* FROM project p JOIN project_setting s ON s.project_id = p.id WHERE p.state = 'ACTIVE'"
    if key:
        return conn.execute(sql + " AND p.key = ?", (key,)).fetchall()
    return conn.execute(sql + " ORDER BY p.id").fetchall()


def project(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.* FROM project p JOIN project_setting s ON s.project_id = p.id WHERE p.key = ?", (key,)
    ).fetchone()


def sources(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source WHERE project_id = ? AND enabled = 1 ORDER BY id", (project_id,)
    ).fetchall()


def gates(conn: sqlite3.Connection, project_id: int, include_closed: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM gate WHERE project_id = ?" + ("" if include_closed else " AND closed_at IS NULL")
    return conn.execute(sql + " ORDER BY date, key", (project_id,)).fetchall()


def evidence(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.*, g.key AS gate_key FROM evidence_item i JOIN gate g ON g.id = i.gate_id "
        "WHERE g.project_id = ? AND i.closed_at IS NULL AND g.closed_at IS NULL",
        (project_id,),
    ).fetchall()


def evidence_links(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {i: [] for i in item_ids}
    if item_ids:
        marks = ",".join("?" * len(item_ids))
        for row in conn.execute(f"SELECT * FROM evidence_link WHERE evidence_item_id IN ({marks})", tuple(item_ids)):
            out[row["evidence_item_id"]].append(row)
    return out


def hand_tasks(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, p.display_name AS owner FROM hand_task t LEFT JOIN person p ON p.id = t.owner_person_id "
        "WHERE t.project_id = ?",
        (project_id,),
    ).fetchall()


def holidays(conn: sqlite3.Connection, project_id: int) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT date FROM holiday WHERE project_id IS NULL OR project_id = ?", (project_id,)
    )]


def people_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Lower-cased display names and aliases to person ids, for matching names in sources."""
    index: dict[str, int] = {}
    for row in conn.execute("SELECT id, display_name FROM person"):
        index[row["display_name"].strip().lower()] = row["id"]
    for row in conn.execute("SELECT person_id, value FROM person_alias"):
        index[row["value"].strip().lower()] = row["person_id"]
    return index


# ---------------------------------------------------------------- run lifecycle

def start_run(conn: sqlite3.Connection, trigger: str, scope: str, baseline: bool, now: datetime) -> int:
    with transaction(conn):
        return int(conn.execute(
            "INSERT INTO run(trigger, scope, baseline, started_at) VALUES (?, ?, ?, ?)",
            (trigger, scope, int(baseline), stamp(now)),
        ).lastrowid)


def end_run(conn: sqlite3.Connection, run_id: int, result: str, error: str = "", *, cost: float = 0, seconds: float = 0) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE run SET ended_at = ?, result = ?, error = ?, model_cost = model_cost + ?, "
            "model_seconds = model_seconds + ? WHERE id = ?",
            (stamp(), result, error[:2000], cost, seconds, run_id),
        )


def scheduled_run_published_on(conn: sqlite3.Connection, day_start_utc: str, day_end_utc: str) -> bool:
    """R-R3: has a scheduled run of this local day already published a snapshot?"""
    return conn.execute(
        "SELECT 1 FROM run WHERE trigger = 'SCHEDULE' AND result IN ('OK', 'PARTIAL') "
        "AND started_at >= ? AND started_at < ?",
        (day_start_utc, day_end_utc),
    ).fetchone() is not None


def add_source_read(conn: sqlite3.Connection, *, run_id: int | None = None, onboarding_id: int | None = None,
                    source_id: int, branch: str, sha: str = "", date: str = "", ok: bool, error: str = "") -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO run_source(run_id, onboarding_id, source_id, branch, commit_sha, commit_date, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, onboarding_id, source_id, branch, sha, date, int(ok), error[:1000]),
        )


def add_problem(conn: sqlite3.Connection, *, run_id: int | None = None, onboarding_id: int | None = None,
                project_id: int | None = None, stage: str, adapter: str = "", path: str = "",
                line: int | None = None, reason: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO run_problem(run_id, onboarding_id, project_id, stage, adapter, path, line, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, onboarding_id, project_id, stage, adapter, path, line, reason[:1000]),
        )


def log_request(conn: sqlite3.Connection, *, run_id: int | None, onboarding_id: int | None,
                method: str, host: str, path: str, status: int | None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO request_log(run_id, onboarding_id, method, host, path, status, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, onboarding_id, method, host, path, status, stamp()),
        )


# ---------------------------------------------------------------- snapshots

def current_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.* FROM current_snapshot c JOIN snapshot s ON s.id = c.snapshot_id WHERE c.only = 1"
    ).fetchone()


def new_snapshot(conn: sqlite3.Connection, run_id: int, now: datetime) -> int:
    with transaction(conn):
        return int(conn.execute(
            "INSERT INTO snapshot(run_id, created_at) VALUES (?, ?)", (run_id, stamp(now))
        ).lastrowid)


def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    if table not in RUN_TABLES:
        raise ValueError(f"{table} is not a run table")
    cols = list(row)
    marks = ",".join("?" * len(cols))
    names = ",".join(f'"{c}"' for c in cols)
    return int(conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', [row[c] for c in cols]).lastrowid)


def insert_many(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    with transaction(conn):
        for row in rows:
            insert(conn, table, row)


def carry_over(conn: sqlite3.Connection, from_snapshot: int, to_snapshot: int, project_id: int, source_id: int,
               *, github_only: bool = False) -> int:
    """Copy rows of one source from the last snapshot, marked "not read today" (Part 2 §6-6)."""
    copied = 0
    with transaction(conn):
        for table in CARRY_TABLES:
            cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")') if r["name"] not in ("id", "snapshot_id", "carried_over")]
            names = ",".join(f'"{c}"' for c in cols)
            cur = conn.execute(
                f'INSERT INTO "{table}" (snapshot_id, carried_over, {names}) '
                f'SELECT ?, 1, {names} FROM "{table}" WHERE snapshot_id = ? AND project_id = ? AND src_source_id = ?'
                + (' AND src_object IS NOT NULL' if github_only else ''),
                (to_snapshot, from_snapshot, project_id, source_id),
            )
            copied += cur.rowcount
        if not github_only:
            conn.execute("INSERT INTO model_source SELECT ?, source_id, commit_sha, read_at FROM model_source "
                         "WHERE snapshot_id = ? AND source_id = ?", (to_snapshot, from_snapshot, source_id))
    return copied


def previous_rows(conn: sqlite3.Connection, table: str, snapshot_id: int | None, project_id: int) -> list[sqlite3.Row]:
    if snapshot_id is None:
        return []
    return conn.execute(f'SELECT * FROM "{table}" WHERE snapshot_id = ? AND project_id = ?', (snapshot_id, project_id)).fetchall()


def carry_unselected(conn, previous, snapshot, selected):
    """A scoped run replaces one project in the global snapshot."""
    with transaction(conn):
        for p in projects(conn):
            pid = p["id"]
            if pid == selected:
                continue
            fact_ids = {}
            for table in (*CARRY_TABLES, "blocker", "unmatched_name"):
                for row in rows(conn, table, previous, pid):
                    data = dict(row)
                    old_id = data.pop("id")
                    data["snapshot_id"] = snapshot
                    if "carried_over" in data:
                        data["carried_over"] = 1
                    new_id = insert(conn, table, data)
                    if table == "fact":
                        fact_ids[old_id] = new_id
            for table, join in (("item_state", "JOIN evidence_item e ON e.id = x.evidence_item_id JOIN gate g ON g.id = e.gate_id"),
                                ("proposal", "JOIN gate g ON g.id = x.gate_id")):
                for row in conn.execute(f"SELECT x.* FROM {table} x {join} WHERE x.snapshot_id = ? AND g.project_id = ?", (previous, pid)):
                    data = dict(row)
                    del data["id"]
                    data["snapshot_id"] = snapshot
                    if table == "proposal":
                        reasons = json.loads(data["reasons"])
                        for reason in reasons:
                            reason["facts"] = [fact_ids[i] for i in reason["facts"] if i in fact_ids]
                        data["reasons"] = json.dumps(reasons)
                    insert(conn, table, data)
            conn.execute("INSERT INTO model_source SELECT ?, m.source_id, m.commit_sha, m.read_at FROM model_source m "
                         "JOIN source s ON s.id = m.source_id WHERE m.snapshot_id = ? AND s.project_id = ?", (snapshot, previous, pid))


def model_totals(conn, run_id):
    row = conn.execute("SELECT coalesce(sum(cost), 0), coalesce(sum(seconds), 0) FROM model_read WHERE run_id = ?", (run_id,)).fetchone()
    return {"cost": row[0], "seconds": row[1]}


def rows(conn: sqlite3.Connection, table: str, snapshot_id: int, project_id: int) -> list[sqlite3.Row]:
    return previous_rows(conn, table, snapshot_id, project_id)


def publish(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """ST-8: the snapshot becomes current in one step (NFR-02)."""
    with transaction(conn):
        conn.execute("UPDATE snapshot SET published_at = ? WHERE id = ?", (stamp(), snapshot_id))
        conn.execute(
            "INSERT INTO current_snapshot(only, snapshot_id) VALUES (1, ?) "
            "ON CONFLICT(only) DO UPDATE SET snapshot_id = excluded.snapshot_id",
            (snapshot_id,),
        )


# ---------------------------------------------------------------- onboarding

def start_onboarding(conn: sqlite3.Connection, project_id: int, now: datetime) -> int:
    with transaction(conn):
        return int(conn.execute(
            "INSERT INTO onboarding(project_id, started_at, progress) VALUES (?, ?, 'Starting')", (project_id, stamp(now))
        ).lastrowid)


def onboarding_progress(conn: sqlite3.Connection, onboarding_id: int, text: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE onboarding SET progress = ? WHERE id = ?", (text[:300], onboarding_id))


def end_onboarding(conn: sqlite3.Connection, onboarding_id: int, result: str, *, error: str = "", prose_read: bool = False,
                   dropped: int = 0, cost: float = 0, seconds: float = 0, used_codex: bool = False) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE onboarding SET ended_at = ?, result = ?, error = ?, prose_read = ?, dropped = ?, "
            "model_cost = ?, model_seconds = ?, used_codex = ?, progress = '' WHERE id = ?",
            (stamp(), result, error[:2000], int(prose_read), dropped, cost, seconds, int(used_codex), onboarding_id),
        )


def rejected_identities(conn: sqlite3.Connection, project_id: int) -> set[tuple[str, str, str]]:
    """(kind, identity, quote) of proposals whose newest decision is REJECTED (R-O5)."""
    rows_ = conn.execute(
        "SELECT p.kind, p.identity, p.quote, d.decision FROM onboarding_proposal p "
        "JOIN onboarding_decision d ON d.proposal_id = p.id "
        "WHERE p.project_id = ? AND d.id = (SELECT max(id) FROM onboarding_decision WHERE proposal_id = p.id)",
        (project_id,),
    ).fetchall()
    return {(r["kind"], r["identity"], r["quote"]) for r in rows_ if r["decision"] == "REJECTED"}


def existing_setup(conn: sqlite3.Connection, project_id: int) -> dict[str, set[str]]:
    """What the project already has, by proposal identity (Part 2 §6-7)."""
    setting_row = conn.execute("SELECT * FROM project_setting WHERE project_id = ?", (project_id,)).fetchone()
    proj = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    have: dict[str, set[str]] = {k: set() for k in ("PROJECT", "GATE", "EVIDENCE", "WBS", "DASHBOARD_FILE", "PATTERN", "PERSON", "HOLIDAY")}
    if proj["scope_note"].strip():
        have["PROJECT"].add("project")
    for g in gates(conn, project_id, include_closed=True):
        have["GATE"].add(g["key"].lower())
    for row in conn.execute(
        "SELECT i.text, g.key FROM evidence_item i JOIN gate g ON g.id = i.gate_id WHERE g.project_id = ?", (project_id,)
    ):
        have["EVIDENCE"].add(evidence_identity(row["key"], row["text"]))
    if setting_row["wbs_path"]:
        have["WBS"].add("wbs")
    if setting_row["dashboard_json_path"]:
        have["DASHBOARD_FILE"].add("dashboard")
    if setting_row["branch_unit_pattern"]:
        have["PATTERN"].add("BRANCH_UNIT")
    if setting_row["task_key_pattern"]:
        have["PATTERN"].add("TASK_KEY")
    have["PERSON"] = set(people_index(conn))
    have["HOLIDAY"] = {r["date"] for r in conn.execute("SELECT date FROM holiday WHERE project_id = ?", (project_id,))}
    return have


def evidence_identity(gate_key: str, text: str) -> str:
    return gate_key.lower() + "|" + " ".join(text.lower().split())


def store_proposals(conn: sqlite3.Connection, onboarding_id: int, project_id: int, proposals: list[dict]) -> None:
    with transaction(conn):
        for p in proposals:
            src = p.get("src", {})
            insert(conn, "onboarding_proposal", {
                "onboarding_id": onboarding_id,
                "project_id": project_id,
                "kind": p["kind"],
                "identity": p["identity"],
                "payload": json.dumps(p["payload"], ensure_ascii=False),
                "read_by": p["read_by"],
                "quote": p.get("quote", ""),
                "date_origin": p.get("date_origin", ""),
                **{c: src.get(c) for c in SRC_COLUMNS},
            })
