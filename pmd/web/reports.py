"""Markdown export of the gate's confirmed record and its snapshot evidence."""

from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import quote, urljoin

from ..data import hand, views
from ..rules.links import evidence_subjects, pr_subject
from . import present


def _text(value):
    return re.sub(r"([\\`*_\[\]#|~])", r"\\\1", escape(" ".join(str(value).split()), quote=False))


def _link(source, base):
    label, url = source["label"], source["url"]
    if not url:
        return _text(label)
    location = quote(urljoin(base, url), safe="/:?#[]@!$&'()*+,;=%~-._")
    return f"[{_text(label)}](<{location}>)"


def describe(kind, row):
    if kind == "fact":
        value = json.loads(row["value"])
        text = value.get("text") or ", ".join(f"{key}: {value[key]}" for key in value)
        return f"{row['kind']} · {text}" + (" (model reading)" if row["read_by"] == "MODEL" else "")
    if kind == "task":
        return f"Task {row['key']} · {row['title']} · {present.TASK_STATUS_WORD.get(row['status'], row['status'])}"
    if kind == "pr":
        checks = f" · checks {row['checks_state'].lower()}" if row["checks_state"] != "NONE" else ""
        review = f" · review {row['review_state'].lower().replace('_', ' ')}" if row["review_state"] != "NONE" else ""
        return f"PR #{row['number']} · {row['title']} · {row['state'].lower()}{review}{checks}"
    if kind == "review":
        return f"Review on PR #{row['pr_number']} · {row['reviewer_raw']} · {row['state'].lower().replace('_', ' ')}"
    if kind == "unit":
        return f"Unit {row['unit']} · {row['status'] or row['stage']}"
    if kind == "stage":
        return f"Unit {row['unit']} · {row['stage']} · {row['state'].lower().replace('_', ' ')}"
    return row["detail"]


def _item_records(subjects, records):
    for kind, rows, subject_of in records:
        for row in rows:
            if subject_of(row) in subjects:
                yield kind, row


def gate_markdown(conn, project, gate, base_url, include_proposal=False):
    pid, gid = project["id"], gate["id"]
    snapshot = views.current_snapshot(conn)
    snap = snapshot["id"] if snapshot else None
    repos = present.repos_of(conn)
    confirmed = hand.latest_confirmations(conn, [gid]).get(gid)
    items = hand.list_items(conn, [gid])[gid]
    links = hand.list_links(conn, [item["id"] for item in items])
    states = views.item_states(conn, snap)

    def rows(table):
        return views.snapshot_rows(conn, table, snap, pid)

    tasks, prs = rows("task_state"), rows("pr_state")
    hand_tasks = hand.list_tasks(conn, pid)
    all_tasks = [*tasks, *hand_tasks]
    records = (("task", tasks, lambda r: ("TASK", r["key"])),
               ("hand_task", hand_tasks, lambda r: ("TASK", r["key"])),
               ("pr", prs, lambda r: pr_subject(r["src_source_id"], r["number"])),
               ("unit", rows("unit_state"), lambda r: ("UNIT", r["unit"])),
               ("stage", rows("unit_stage_state"), lambda r: ("UNIT", r["unit"])),
               ("fact", rows("fact"), lambda r: (r["subject_type"].upper(), r["subject_key"])))

    def record(kind, row):
        hand_work = kind == "hand_task"
        src = present.hand_task_citation(project["key"], row) if hand_work else present.citation(row, repos, kind)
        source = _link(src, base_url)
        text = _text(describe("task" if hand_work else kind, row))
        status = " (carried over)" if "carried_over" in row.keys() and row["carried_over"] else ""
        return f"{text}{status} · {source}" if source else text + status

    lines = [f"# {_text(project['name'])} · {_text(gate['key'])} {_text(gate['name'])}", "", f"Gate date: {gate['date']}"]
    if gate["description"]:
        lines.append(_text(gate["description"]))
    if gate["outcome"]:
        lines.append(f"Outcome: {present.OUTCOME[gate['outcome']]['word']}")
        if gate["outcome_note"]:
            lines.append(_text(gate["outcome_note"]))
        if gate["outcome"] == "CONDITIONAL_PASS":
            lines.extend([f"Open scope: {_text(gate['open_scope'])}", f"Owner: {_text(gate['open_owner'])}", f"Close by: {gate['close_date']}"])
    elif confirmed:
        lines.append(f"Confirmed health: {present.STATUS[confirmed['value']]['word']} ({confirmed['created_at']})")
        if confirmed["note"]:
            lines.append(_text(confirmed["note"]))
    else:
        lines.append("Confirmed health: Unset")

    lines.extend(["", "## Acceptance evidence", ""])
    if not items:
        lines.append("No evidence items recorded.")
    for item in items:
        state = present.item_state(item, states.get(item["id"]))
        lines.append(f"- **{present.ITEM_STATE[state]['word']}** · {_text(item['text'])}")
        if item["note"]:
            lines.append(f"  - {_text(item['note'])}")
        if item["evidence_url"]:
            lines.append("  - " + _link({"label": "Evidence", "url": item["evidence_url"]}, base_url))
        subjects = evidence_subjects(links[item["id"]], all_tasks, prs)
        lines.extend(f"  - {record(kind, row)}" for kind, row in _item_records(subjects, records))

    lines.extend(["", "## Open blockers", ""])
    subjects = evidence_subjects([link for item in items for link in links[item["id"]]], all_tasks, prs, gate["key"])
    blockers = []
    for row in rows("blocker"):
        kind, key = row["subject_type"], row["subject_key"]
        subject = pr_subject(row["src_source_id"], key) if kind == "PR" else ("TASK" if kind == "HAND_TASK" else kind, key)
        if subject in subjects or kind == "PROJECT":
            if kind == "HAND_TASK":
                task = next((t for t in hand_tasks if t["key"] == key), None)
                if task is not None:
                    blockers.append(f"{_text(row['detail'])} · {_link(present.hand_task_citation(project['key'], task), base_url)}")
                    continue
            blockers.append(record("blocker", row))
    lines.extend([f"- {blocker}" for blocker in blockers] or ["No gate-linked blockers recorded in the snapshot."])

    lines.extend(["", "## Snapshot and commits", ""])
    if snapshot:
        tz = present.zone(conn)
        stamp = present.local_stamp(snapshot["created_at"], tz)
        lines.append(f"Snapshot {snapshot['id']} · {stamp:%Y-%m-%d %H:%M %Z} · {snapshot['run_result']}")
        seen = set()
        for row in views.project_commits(conn, pid, snapshot["run_id"]):
            key = row["source_id"], row["branch"]
            if key in seen:
                continue
            seen.add(key)
            sha = row["commit_sha"]
            repo = repos.get(row["source_id"])
            ref = _link({"label": sha, "url": f"https://github.com/{repo}/commit/{sha}" if repo else ""}, base_url)
            carried = " (last successful read)" if row["run_id"] != snapshot["run_id"] else ""
            lines.append(f"- {_text(row['source_key'])} · {_text(row['branch'])} · {ref}{carried}")
        if not seen:
            lines.append("No repository commits recorded for this project.")
    else:
        lines.append("No run has published a snapshot.")

    if include_proposal and not gate["outcome"] and not gate["closed_at"]:
        proposal = views.health_proposals(conn, snap).get(gid)
        if proposal and (not confirmed or confirmed["value"] != proposal["value"]):
            lines.extend(["", "## Not confirmed", "", f"Proposed health: {present.STATUS[proposal['value']]['word']}"])
            for reason in json.loads(proposal["reasons"]):
                citations = [views.fact(conn, fid) for fid in reason["facts"]]
                sources = [_link(present.citation(fact, repos, "fact"), base_url) for fact in citations if fact is not None]
                lines.append("- " + _text(reason["text"]) + (" · " + ", ".join(sources) if sources else ""))
    return "\n".join(lines) + "\n"
