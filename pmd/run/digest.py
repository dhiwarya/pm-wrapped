"""ST-6 and ST-7: snapshot changes and the weekday digest."""

from __future__ import annotations

import json
from datetime import date

from ..data import run as R
from ..rules.changes import highlights
from ..rules.pulls import assess
from ..rules.workdays import working_days_left

# Identity and displayed fields. Source locations may move without a change to the value.
TABLES = {
    "task_state": (("key",), ("title", "owner_raw", "gate_key", "start", "finish", "status", "depends_on")),
    "unit_state": (("unit",), ("stage", "status", "blocked", "lead_raw")),
    "unit_stage_state": (("unit", "stage"), ("state",)),
    "pr_state": (("number",), ("title", "state", "review_state", "checks_state", "merged_at",
                               "draft", "merge_status", "approved_head", "head_sha", "ci_unread")),
    "review_state": (("pr_number", "reviewer_raw"), ("state", "submitted_at")),
    "issue_state": (("number",), ("title", "state", "assignees", "labels")),
    "blocker": (("kind", "subject_type", "subject_key"), ("detail", "person_raw")),
    "fact": (("subject_type", "subject_key", "kind"), ("value", "fact_date")),
}


def _source(row):
    return {key: row[key] for key in R.SRC_COLUMNS if key in row.keys() and row[key] is not None}


def _keyed(rows, table):
    keys, columns = TABLES[table]
    keyed = {}
    for row in rows:
        key = (row["src_source_id"], *(row[k] for k in keys))
        if table == "fact" and row["kind"] == "note":
            value = json.loads(row["value"])
            key += (value.get("hash", row["value"]),)
        elif table == "fact" and row["read_by"] == "MODEL":
            key += (row["src_path"] or row["src_url"], " ".join(row["quote"].split()))
        keyed[key] = (row, {column: row[column] for column in columns})
    return keyed


def changes(conn, snapshot, previous, projects, *, hand_through):
    found = []
    prior_id = previous["id"] if previous else None
    for p in projects:
        pid = p["id"]
        failed = {r[0] for r in conn.execute("SELECT rs.source_id FROM run_source rs JOIN snapshot s ON s.run_id = rs.run_id "
                                             "WHERE s.id = ? GROUP BY rs.source_id HAVING min(rs.ok) = 0", (snapshot,))}
        for table in TABLES:
            old = _keyed(R.previous_rows(conn, table, prior_id, pid), table)
            new = _keyed(R.rows(conn, table, snapshot, pid), table)
            for key in sorted(old.keys() | new.keys(), key=str):
                before, first = old.get(key, (None, None))
                after, last = new.get(key, (None, None))
                if first == last or key[0] in failed or (after and "carried_over" in after.keys() and after["carried_over"]):
                    continue
                if after is None and table in ("pr_state", "review_state", "issue_state"):
                    continue  # the GitHub window omits older objects
                row = after or before
                if table == "fact" and row["kind"] == "note":
                    last = {**json.loads(last["value"]), "date": last["fact_date"]} if last else None
                    first = {**json.loads(first["value"]), "date": first["fact_date"]} if first else None
                found.append({"snapshot_id": snapshot, "project_id": pid, "grp": table, "subject_type": table,
                              "subject_key": str(key[1]), "kind": row["kind"] if "kind" in row.keys() else table,
                              "change": "ADDED" if before is None else "REMOVED" if after is None else "CHANGED",
                              "old": json.dumps(first) if first is not None else None, "new": json.dumps(last) if last is not None else None,
                              "source": json.dumps(_source(row))})
    if previous:
        found.extend(_hand_changes(conn, snapshot, previous["created_at"], hand_through, {p["id"] for p in projects},
                                   not conn.execute("SELECT scope FROM run JOIN snapshot s ON s.run_id = run.id WHERE s.id = ?", (snapshot,)).fetchone()[0]))
    return found


PROJECT_LINKS = {
    "project": ("project", "id"), "project_setting": ("project_setting", "project_id"),
    "source": ("source", "project_id"), "gate": ("gate", "project_id"), "hand_task": ("hand_task", "project_id"),
    "holiday": ("holiday", "project_id"), "evidence_item": ("evidence_item JOIN gate ON gate.id = evidence_item.gate_id", "gate.project_id"),
    "evidence_link": ("evidence_link JOIN evidence_item e ON e.id = evidence_link.evidence_item_id JOIN gate g ON g.id = e.gate_id", "g.project_id"),
    "confirmation": ("confirmation JOIN gate ON gate.id = confirmation.gate_id", "gate.project_id"),
    "onboarding_decision": ("onboarding_decision JOIN onboarding_proposal p ON p.id = onboarding_decision.proposal_id", "p.project_id"),
}


def _hand_changes(conn, snapshot, since, through, pids, include_global):
    for row in conn.execute("SELECT * FROM hand_change_log WHERE at > ? AND at <= ? ORDER BY id", (since, through)):
        table = row["table_name"]
        project_id = None
        if table in PROJECT_LINKS:
            join, column = PROJECT_LINKS[table]
            p = conn.execute(f"SELECT {column} FROM {join} WHERE {table}.rowid = ?", (row["row_id"],)).fetchone()
            if p:
                project_id = p[0]
            elif row["field"] == "*":
                values = json.loads(row["new"] or row["old"])
                project_id = values.get("project_id")
                if project_id is not None and not conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone():
                    project_id = None
                if project_id is None and values.get("gate_id"):
                    gate = conn.execute("SELECT project_id FROM gate WHERE id = ?", (values["gate_id"],)).fetchone()
                    project_id = gate[0] if gate else None
        if project_id not in pids and not (include_global and project_id is None):
            continue
        kind = "ADDED" if row["field"] == "*" and row["old"] is None else "REMOVED" if row["field"] == "*" else "CHANGED"
        yield {"snapshot_id": snapshot, "project_id": project_id, "grp": "hand", "subject_type": table,
               "subject_key": str(row["row_id"]), "kind": row["field"], "change": kind,
               "old": json.dumps(row["old"]) if row["old"] is not None else None,
               "new": json.dumps(row["new"]) if row["new"] is not None else None}


def previous_scheduled(conn):
    return conn.execute("SELECT s.* FROM snapshot s JOIN run r ON r.id = s.run_id "
                        "WHERE r.trigger = 'SCHEDULE' AND s.published_at IS NOT NULL ORDER BY s.id DESC LIMIT 1").fetchone()


def _confirmation(conn, gate_id):
    row = conn.execute("SELECT value FROM confirmation WHERE gate_id = ? ORDER BY id DESC LIMIT 1", (gate_id,)).fetchone()
    return row[0] if row else None


def _word(value):
    return value.replace("_", " ").capitalize()


def _card(kind, title, lines, **fields):
    return {"kind": kind, "title": title, "lines": lines, **fields}


def build(conn, run_id, snapshot, projects, day, changes_, at):
    summary = {p["id"]: [] for p in projects}
    for item in changes_:
        if item["project_id"] in summary:
            summary[item["project_id"]].append(item)
    entries, waiting = [], []
    pull_sections = []
    refreshed = {r[0] for r in conn.execute("SELECT DISTINCT source_id FROM run_source WHERE run_id = ? AND github_read = 1", (run_id,))}
    owner = R.setting(conn, "owner_person_id", "")
    needs_call = 0
    for p in projects:
        pid = p["id"]
        gates = [g for g in R.gates(conn, pid) if not g["outcome"]]
        others = summary[pid]
        groups = highlights(others)
        counts = " · ".join(f"{len(items)} {label if label == 'PRs merged' else label.lower()}" for label, items in groups.items())
        change_line = counts if any(groups.values()) else f"{len(others)} other changes" if others else "No changes."
        change_lines = [change_line]
        change_lines.extend(f"Merged PR #{r['subject_key']} · {json.loads(r['new'])['title']}" for r in groups["PRs merged"])
        change_lines.extend(json.loads(row["new"])["text"] for row in groups["New notes"])
        # Include the day's actionable queue even when a manual run already saw it.
        repos = {s["id"]: s["github_repo"] for s in R.sources(conn, pid) if s["github_repo"]}
        pr_lines = [f"PR readiness unavailable for {repo}. See Runs for the GitHub read status."
                    for source_id, repo in repos.items() if source_id not in refreshed]
        for pr in R.rows(conn, "pr_state", snapshot, pid):
            if pr["state"] != "OPEN" or pr["source_id"] not in repos:
                continue
            result = assess(pr, stale=pr["carried_over"] or pr["source_id"] not in refreshed)
            if result.label in ("Ready to merge", "Needs attention") or (pr["ci_unread"] and not pr["carried_over"]):
                repo = repos[pr["source_id"]]
                pr_lines.append(f"{result.label} · {repo}#{pr['number']} · {pr['title']}\n  {result.reason}\n  {pr['src_url']}")
        if pr_lines:
            pull_sections.append(f"{p['name']} · Pull requests\n" + "\n".join(sorted(pr_lines)))
        blockers = R.rows(conn, "blocker", snapshot, pid)
        if blockers:
            count = sum(r["grp"] == "blocker" and r["change"] == "ADDED" for r in others)
            change_lines.append(f"{len(blockers)} project blockers · {count} new")
        if owner:
            waiting.extend(f"{p['name']} · {b['detail']}" for b in blockers if str(b["person_id"]) == owner)
            aliases = {r[0] for r in conn.execute("SELECT value FROM person_alias WHERE person_id = ? UNION SELECT display_name FROM person WHERE id = ?", (owner, owner))}
            waiting.extend(f"{p['name']} · {v['text']}" for row in R.rows(conn, "fact", snapshot, pid)
                           if row["kind"] == "question_waiting" and aliases.intersection((v := json.loads(row["value"])).get("people", [])))
        proposals = {r["gate_id"]: r for r in conn.execute("SELECT * FROM proposal WHERE snapshot_id = ? AND gate_id IN (SELECT id FROM gate WHERE project_id = ?)", (snapshot, pid))}
        open_proposals = [g for g in gates if g["id"] in proposals and proposals[g["id"]]["value"] != _confirmation(conn, g["id"])]
        needs_call += len(open_proposals)
        title = p["name"]
        detail = []
        if gates:
            gate = gates[0]
            next_date = date.fromisoformat(gate["date"])
            left = working_days_left(day, next_date, {date.fromisoformat(d) for d in R.holidays(conn, pid)})
            days = f"{left} working days to {gate['name']} · {gate['date']}" if next_date >= day else f"{gate['name']} · due {gate['date']}"
            detail.append(days)
            if len(gates) > 1:
                detail.append(f"Then {gates[1]['name']} · {gates[1]['date']}")
            if gate["id"] in proposals:
                proposal = proposals[gate["id"]]
                confirmed = _confirmation(conn, gate["id"])
                health = f"Proposed {_word(proposal['value'])}." + (f" Confirmed {_word(confirmed)}." if confirmed else "")
                reasons = [reason["text"] for reason in json.loads(proposal["reasons"])]
                detail.extend([health, *reasons])
                if proposal["produced_by"] == "MODEL" and gate in open_proposals:
                    entries.append(_card("DAYS", title, detail[:2 if len(gates) > 1 else 1], hero=left))
                    entries.append(_card("DECISION", title, [health, *reasons], gate_id=gate["id"], proposal_id=proposal["id"],
                                         project_key=p["key"], value=proposal["value"]))
                    if others:
                        entries.append(_card("CHANGES", title, change_lines))
                    detail = []
        remaining = [f"{g['name']} · proposed {_word(proposals[g['id']]['value'])}. Needs your decision." for g in open_proposals if g != gates[0]]
        if detail or remaining or not gates:
            entries.append(_card("PROJECT", title, [*detail, *remaining, *change_lines]))
    sources = conn.execute("SELECT count(DISTINCT source_id) FROM run_source WHERE run_id = ? AND ok = 1", (run_id,)).fetchone()[0]
    problems = conn.execute("SELECT reason FROM run_problem WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    kept, dropped = conn.execute("SELECT coalesce(sum(kept),0), coalesce(sum(dropped),0) FROM model_read WHERE run_id = ?", (run_id,)).fetchone()
    opening = _card("OPENING", f"{day:%A %d %B %Y}", [f"{len(projects)} projects · {needs_call} need your call", f"Snapshot {at:%H:%M}"], hero=len(projects))
    closing = _card("CLOSING", "Run summary", [f"{sources} sources read · {len(problems)} problems", f"Model facts · {kept} kept · {dropped} dropped", *(r[0] for r in problems)])
    wait = [_card("WAITING", "Waiting on you", waiting, hero=len(waiting))] if waiting else []
    full = [opening, *entries, *wait, closing]
    lines = [f"PMD digest · {day.isoformat()}", *(f"{c['title']}\n" + "\n".join(c["lines"]) for c in full), *pull_sections]
    budget = 10 - len(wait)
    if len(entries) > budget:
        extra = entries[budget - 1:]
        more = _card("MORE", "More project updates", [f"{c['title']} · {c['lines'][0]}" for c in extra])
        lines.append(more["title"] + "\n" + "\n".join(more["lines"]))
        full = [opening, *entries[:budget - 1], more, *wait, closing]
    return "\n\n".join(lines) + "\n", full
