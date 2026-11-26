"""Read functions over run tables, for the web process (Part 1 §4-1).

The web never imports ``data.run`` (which writes run tables). Every function here
only reads.
"""

from __future__ import annotations

import json
import sqlite3

SOURCE_TABLES = {"task": "task_state", "unit": "unit_state", "stage": "unit_stage_state", "pr": "pr_state",
                 "review": "review_state", "blocker": "blocker", "fact": "fact"}


def current_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.*, r.result AS run_result, r.trigger AS run_trigger FROM current_snapshot c "
        "JOIN snapshot s ON s.id = c.snapshot_id JOIN run r ON r.id = s.run_id WHERE c.only = 1"
    ).fetchone()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run WHERE result <> 'SKIPPED' ORDER BY id DESC LIMIT 1").fetchone()


def running(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM run WHERE result = 'RUNNING' UNION SELECT 1 FROM onboarding WHERE result = 'RUNNING'"
    ).fetchone() is not None


def runs(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run_record(conn, run_id):
    return conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()


def kept_snapshot(conn, run_id):
    return conn.execute("SELECT * FROM snapshot WHERE run_id = ? AND published_at IS NOT NULL", (run_id,)).fetchone()


def run_sources(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {i: [] for i in run_ids}
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        for r in conn.execute(
            f"SELECT rs.*, s.key AS source_key, p.key AS project_key FROM run_source rs "
            f"LEFT JOIN source s ON s.id = rs.source_id LEFT JOIN project p ON p.id = s.project_id "
            f"WHERE rs.run_id IN ({marks}) ORDER BY rs.id", tuple(run_ids)
        ):
            out[r["run_id"]].append(r)
    return out


def run_problems(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {i: [] for i in run_ids}
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        for r in conn.execute(f"SELECT * FROM run_problem WHERE run_id IN ({marks}) ORDER BY id", tuple(run_ids)):
            out[r["run_id"]].append(r)
    return out


def run_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    snap = conn.execute("SELECT id FROM snapshot WHERE run_id = ?", (run_id,)).fetchone()
    counts = {"requests": conn.execute("SELECT count(*) FROM request_log WHERE run_id = ?", (run_id,)).fetchone()[0]}
    if snap:
        for table in ("fact", "task_state", "pr_state", "blocker"):
            counts[table] = conn.execute(f"SELECT count(*) FROM {table} WHERE snapshot_id = ?", (snap["id"],)).fetchone()[0]
    return counts


def snapshot_rows(conn: sqlite3.Connection, table: str, snapshot_id: int | None, project_id: int) -> list[sqlite3.Row]:
    allowed = {"unit_state", "unit_stage_state", "task_state", "pr_state", "review_state", "issue_state", "fact", "blocker", "unmatched_name"}
    if table not in allowed or snapshot_id is None:
        return []
    return conn.execute(f"SELECT * FROM {table} WHERE snapshot_id = ? AND project_id = ? ORDER BY id", (snapshot_id, project_id)).fetchall()


def pull_sources(conn, run_id):
    return conn.execute(
        "SELECT s.id, s.github_repo, p.name AS project, EXISTS (SELECT 1 FROM run_source rs "
        "WHERE rs.run_id = ? AND rs.source_id = s.id AND rs.github_read = 1) AS refreshed "
        "FROM source s JOIN project p ON p.id = s.project_id "
        "WHERE s.enabled AND s.github_repo <> '' AND p.state = 'ACTIVE' ORDER BY p.name, s.github_repo", (run_id,)
    ).fetchall()


def open_pulls(conn, snapshot_id):
    return conn.execute(
        "SELECT pr.*, s.github_repo, p.name AS project FROM pr_state pr JOIN source s ON s.id = pr.source_id "
        "JOIN project p ON p.id = s.project_id WHERE pr.snapshot_id = ? AND pr.state = 'OPEN' "
        "AND s.enabled AND s.github_repo <> '' AND p.state = 'ACTIVE' ORDER BY p.name, s.github_repo, pr.number", (snapshot_id,)
    ).fetchall()


def facts(conn: sqlite3.Connection, snapshot_id: int | None, project_id: int, kind: str) -> list[dict]:
    if snapshot_id is None:
        return []
    rows = conn.execute("SELECT * FROM fact WHERE snapshot_id = ? AND project_id = ? AND kind = ? ORDER BY id",
                        (snapshot_id, project_id, kind)).fetchall()
    return [dict(r, value=json.loads(r["value"] or "{}")) for r in rows]


def item_states(conn: sqlite3.Connection, snapshot_id: int | None) -> dict[int, sqlite3.Row]:
    if snapshot_id is None:
        return {}
    return {r["evidence_item_id"]: r for r in conn.execute("SELECT * FROM item_state WHERE snapshot_id = ?", (snapshot_id,))}


def health_proposals(conn, snapshot_id):
    return {r["gate_id"]: r for r in conn.execute("SELECT * FROM proposal WHERE snapshot_id = ?", (snapshot_id,))}


def health_proposal(conn, proposal_id, gate_id):
    return conn.execute("SELECT p.* FROM proposal p JOIN snapshot s ON s.id = p.snapshot_id "
                        "WHERE p.id = ? AND p.gate_id = ? AND s.published_at IS NOT NULL", (proposal_id, gate_id)).fetchone()


def fact(conn, fact_id):
    return conn.execute("SELECT f.* FROM fact f JOIN snapshot s ON s.id = f.snapshot_id "
                        "WHERE f.id = ? AND s.published_at IS NOT NULL", (fact_id,)).fetchone()


def source_row(conn, kind, row_id):
    table = SOURCE_TABLES.get(kind)
    if table is None:
        return None
    return conn.execute(f"SELECT t.* FROM {table} t JOIN snapshot s ON s.id = t.snapshot_id "
                        "WHERE t.id = ? AND s.published_at IS NOT NULL", (row_id,)).fetchone()


def project_commits(conn, project_id, run_id):
    """Most recent successful read of each branch at or before this snapshot."""
    return conn.execute("SELECT rs.*, source.key AS source_key FROM run_source rs JOIN source ON source.id = rs.source_id "
                        "JOIN run ON run.id = rs.run_id WHERE source.project_id = ? AND run.id <= ? "
                        "AND run.result IN ('OK', 'PARTIAL') AND rs.ok = 1 ORDER BY run.id DESC, rs.id DESC", (project_id, run_id))


def digest(conn, day=None):
    query = "SELECT d.* FROM digest d LEFT JOIN snapshot s ON s.id = d.snapshot_id WHERE (d.snapshot_id IS NULL OR s.published_at IS NOT NULL)"
    if day:
        return conn.execute(query + " AND d.day = ?", (day,)).fetchone()
    return conn.execute(query + " ORDER BY d.day DESC LIMIT 1").fetchone()


def digest_days(conn):
    return [r[0] for r in conn.execute("SELECT d.day FROM digest d LEFT JOIN snapshot s ON s.id = d.snapshot_id "
                                      "WHERE d.snapshot_id IS NULL OR s.published_at IS NOT NULL ORDER BY d.day DESC")]


def changes(conn, snapshot_id):
    return conn.execute("SELECT c.*, p.name AS project FROM change c LEFT JOIN project p ON p.id = c.project_id "
                        "WHERE c.snapshot_id = ? ORDER BY p.name, c.id", (snapshot_id,)).fetchall()


def model_reads(conn, run_id):
    return conn.execute("SELECT m.*, p.key AS project FROM model_read m LEFT JOIN project p ON p.id = m.project_id "
                        "WHERE m.run_id = ? ORDER BY m.id", (run_id,)).fetchall()


def last_source_reads(conn: sqlite3.Connection, project_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT rs.* FROM run_source rs JOIN source s ON s.id = rs.source_id WHERE s.project_id = ? AND rs.run_id IS NOT NULL "
        "AND rs.id IN (SELECT max(id) FROM run_source WHERE run_id IS NOT NULL GROUP BY source_id)", (project_id,)
    ).fetchall()
    return {r["source_id"]: r for r in rows}


def unmatched_names(conn: sqlite3.Connection, snapshot_id: int | None) -> list[sqlite3.Row]:
    if snapshot_id is None:
        return []
    rows = conn.execute(
        "SELECT u.kind, u.value, group_concat(DISTINCT p.key) AS projects FROM unmatched_name u JOIN project p ON p.id = u.project_id "
        "WHERE u.snapshot_id = ? GROUP BY u.kind, u.value ORDER BY u.value COLLATE NOCASE", (snapshot_id,)
    ).fetchall()
    names = {r[0].strip().casefold() for r in conn.execute("SELECT display_name FROM person")}
    aliases = {(r[0], r[1].strip().casefold()) for r in conn.execute("SELECT kind, value FROM person_alias")}
    return [r for r in rows if r["value"].strip().casefold() not in names and (r["kind"], r["value"].strip().casefold()) not in aliases]


# ---------------------------------------------------------------- onboarding

def onboardings(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM onboarding WHERE project_id = ? ORDER BY id DESC", (project_id,)).fetchall()


def onboarding(conn: sqlite3.Connection, project_id: int, onboarding_id: int | None = None) -> sqlite3.Row | None:
    if onboarding_id is None:
        return conn.execute("SELECT * FROM onboarding WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    return conn.execute("SELECT * FROM onboarding WHERE id = ? AND project_id = ?", (onboarding_id, project_id)).fetchone()


def onboarding_sources(conn: sqlite3.Connection, onboarding_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rs.*, s.key AS source_key, s.github_repo FROM run_source rs LEFT JOIN source s ON s.id = rs.source_id "
        "WHERE rs.onboarding_id = ? ORDER BY rs.id", (onboarding_id,)
    ).fetchall()


def onboarding_problems(conn: sqlite3.Connection, onboarding_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM run_problem WHERE onboarding_id = ? ORDER BY id", (onboarding_id,)).fetchall()


def proposals_with_decisions(conn: sqlite3.Connection, onboarding_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, d.decision, d.edited_payload FROM onboarding_proposal p LEFT JOIN onboarding_decision d "
        "ON d.id = (SELECT max(id) FROM onboarding_decision WHERE proposal_id = p.id) WHERE p.onboarding_id = ? ORDER BY p.id",
        (onboarding_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(r["edited_payload"] or r["payload"])
        item["open"] = r["decision"] in (None, "UNDONE")
        out.append(item)
    return out


def open_proposal_count(conn: sqlite3.Connection, project_id: int) -> int:
    latest = onboarding(conn, project_id)
    if latest is None:
        return 0
    return sum(1 for p in proposals_with_decisions(conn, latest["id"]) if p["open"])
