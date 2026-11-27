"""Turns hand data and the current snapshot into what the templates show (Design §6, §6-9).

Pure reads: no writes. Run tables are read through ``data.views``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html import escape
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..adapters.aidlc import STAGES
from ..data import hand, views
from ..rules.changes import highlights
from ..adapters.wbs import is_wbs_path
from ..rules.pulls import assess
from ..rules.gates import need_call_count, split_next_gate, task_days_late
from ..rules.workdays import working_days_left, working_days_past

# Design §6-9 R-V2: icon + word for every status, and the tile colour of the value.
STATUS = {
    "ON_TRACK": {"cls": "good", "icon": "✓", "word": "On track", "tile": "t-mint"},
    "AT_RISK": {"cls": "warn", "icon": "▲", "word": "At risk", "tile": "t-butter"},
    "LATE": {"cls": "crit", "icon": "✕", "word": "Late", "tile": "t-red"},
    "UNKNOWN": {"cls": "unk", "icon": "?", "word": "Unknown", "tile": "t-night"},
}
RUN_RESULT = {
    "OK": {"cls": "good", "icon": "✓", "word": "OK"},
    "PARTIAL": {"cls": "warn", "icon": "▲", "word": "PARTIAL"},
    "FAILED": {"cls": "crit", "icon": "✕", "word": "FAILED"},
    "RUNNING": {"cls": "unk", "icon": "…", "word": "RUNNING"},
    "SKIPPED": {"cls": "unk", "icon": "–", "word": "SKIPPED"},
    "NONE": {"cls": "unk", "icon": "?", "word": "No run yet"},
}
OUTCOME = {
    "PASS": {"cls": "good", "icon": "✓", "word": "Pass"},
    "CONDITIONAL_PASS": {"cls": "warn", "icon": "◐", "word": "Conditional pass"},
    "REWORK": {"cls": "crit", "icon": "↺", "word": "Rework required"},
}
# R-V3: symbol + word for evidence states. WAIVED is a hand state outside the bar.
ITEM_STATE = {
    "MET": {"icon": "●", "word": "MET", "o": "o1"},
    "CANDIDATE": {"icon": "◕", "word": "CANDIDATE", "o": "o2"},
    "IN_PROGRESS": {"icon": "◑", "word": "IN PROGRESS", "o": "o3"},
    "NOT_STARTED": {"icon": "○", "word": "NOT STARTED", "o": "o4"},
    "WAIVED": {"icon": "⊘", "word": "WAIVED", "o": ""},
}
TASK_STATUS_WORD = {"NOT_STARTED": "Not started", "IN_PROGRESS": "In progress", "DONE": "Done", "BLOCKED": "Blocked", "DEFERRED": "Deferred", "": "Unknown"}
BLOCKER_WORD = {"REVIEW_WAITING": "Review waiting", "CHANGES_REQUESTED": "Changes requested", "CHECK_FAILING": "Checks failing",
                "TASK_LATE": "Task late", "DEPENDENCY_LATE": "Waits on a late task", "UNIT_BLOCKED": "Unit blocked", "STATED": "Stated"}
KIND_WORD = {"PROJECT": "Project", "GATE": "Gates", "EVIDENCE": "Evidence", "WBS": "WBS file", "DASHBOARD_FILE": "Dashboard file",
             "PATTERN": "Patterns", "PERSON": "People", "HOLIDAY": "Holidays"}
LINK_WORD = {"TASK": "task", "TASK_PREFIX": "tasks starting with", "UNIT": "unit", "PR": "PR", "PR_SEARCH": "PR title contains"}
COLOUR_TILE = {c: f"t-{c.lower()}" for c in hand.COLOURS}
COLOUR_WORD = {c: c.capitalize() for c in hand.COLOURS}


# ---------------------------------------------------------------- time

def zone(conn: sqlite3.Connection):
    name = hand.get_setting(conn, "time_zone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None  # host local time


def today(conn: sqlite3.Connection, now: datetime) -> date:
    tz = zone(conn)
    return (now.astimezone(tz) if tz else now.astimezone()).date()


def local_stamp(stamp: str, tz) -> datetime:
    """A stored UTC time (``2026-11-25T01:02:03Z``) in the user's time zone."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(tz) if tz else moment.astimezone()


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def long_date(iso: str | date) -> str:
    d = iso if isinstance(iso, date) else date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B}"


def full_date(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B} {d.year}"


def plural(n: int, word: str) -> str:
    return f"{word}" if n == 1 else f"{word}s"


def when_text(stamp: str | None, tz, now: datetime) -> str:
    if not stamp:
        return ""
    moment = local_stamp(stamp, tz)
    local_now = now.astimezone(tz) if tz else now.astimezone()
    if moment.date() == local_now.date():
        return f"today {moment:%H:%M}"
    return f"{moment:%a} {moment.day} {moment:%b}, {moment:%H:%M}"


# ---------------------------------------------------------------- sources (P-3)

def src_link(row, repos: dict[int, str]) -> dict:
    """Where a row was read: a link when the source is on GitHub, else a plain label."""
    keys = row.keys()
    url = row["src_url"] if "src_url" in keys else None
    path = row["src_path"] if "src_path" in keys else None
    line = row["src_line_from"] if "src_line_from" in keys else None
    line_to = row["src_line_to"] if "src_line_to" in keys else None
    commit = row["src_commit"] if "src_commit" in keys else ""
    if url:
        label = f"{row['src_object']} {row['src_number']}" if "src_object" in keys and row["src_object"] else "source"
        return {"url": url if url.startswith("https://") else "", "label": label}
    if not path:
        return {"url": "", "label": ""}
    label = path + (f":{line}" if line else "")
    repo = repos.get(row["src_source_id"]) if "src_source_id" in keys else None
    if repo and commit and not path.startswith("("):
        anchor = f"#L{line}" + (f"-L{line_to}" if line_to and line_to != line else "") if line else ""
        return {"url": f"https://github.com/{repo}/blob/{commit}/{url_quote(path)}{anchor}", "label": label}
    return {"url": "", "label": label + (f" @ {commit[:7]}" if commit else "")}


def repos_of(conn: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["github_repo"] for r in conn.execute("SELECT id, github_repo FROM source WHERE github_repo <> ''")}


def citation(row, repos, kind):
    src = src_link(row, repos)
    if not src["url"] and src["label"]:
        path = f"/facts/{row['id']}" if kind == "fact" else f"/sources/{kind}/{row['id']}"
        src["url"] = f"{path}?snapshot={row['snapshot_id']}"
    return src


def hand_task_citation(project_key, row):
    return {"url": f"/p/{project_key}?tab=tasks#task-{row['id']}", "label": f"Hand task {row['key']}"}


# ---------------------------------------------------------------- header and stale banner (Design §7-5)

def expected_run_time(now: datetime, tz, run_time: str) -> datetime:
    """The most recent weekday run time at or before ``now``."""
    local_now = now.astimezone(tz) if tz else now.astimezone()
    try:
        hh, mm = (int(x) for x in run_time.split(":"))
    except ValueError:
        hh, mm = 7, 0
    day = local_now.date()
    for _ in range(8):
        moment = datetime.combine(day, time(hh, mm), tzinfo=local_now.tzinfo)
        if day.weekday() < 5 and moment <= local_now:
            return moment
        day -= timedelta(days=1)
    return local_now


def header(conn: sqlite3.Connection, now: datetime) -> dict:
    tz = zone(conn)
    snap = views.current_snapshot(conn)
    last = views.last_run(conn)
    linked = conn.execute("SELECT 1 FROM source s JOIN project p ON p.id = s.project_id WHERE p.state = 'ACTIVE' AND s.enabled = 1").fetchone()
    result = RUN_RESULT[last["result"]] if last else RUN_RESULT["NONE"]
    stale = None
    if linked:
        expected = expected_run_time(now, tz, hand.get_setting(conn, "run_time", "07:00") or "07:00")
        grace = expected + timedelta(minutes=30)
        local_now = now.astimezone(tz) if tz else now.astimezone()
        snap_time = local_stamp(snap["created_at"], tz) if snap else None
        if last and last["result"] == "FAILED" and (not snap or last["id"] > snap["run_id"]):
            stale = {"age": when_text(snap["created_at"], tz, now) if snap else "", "reason": f"The last run failed: {last['error'] or 'see Runs'}"}
        elif snap_time is not None and local_now >= grace and snap_time < expected:
            stale = {"age": when_text(snap["created_at"], tz, now), "reason": "No run has published a snapshot since the scheduled run time."}
    return {"run": last, "result": result, "snapshot": snap, "snap_when": when_text(snap["created_at"], tz, now) if snap else "",
            "stale": stale, "running": views.running(conn), "linked": bool(linked)}


# ---------------------------------------------------------------- evidence

def item_state(row: sqlite3.Row, run_state: sqlite3.Row | None = None) -> str:
    """The hand state wins (R-E1); otherwise the run's state; with no run, NOT_STARTED (R-E2)."""
    if row["hand_state"]:
        return row["hand_state"]
    return run_state["run_state"] if run_state else "NOT_STARTED"


@dataclass
class EvidenceSummary:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    @property
    def segments(self) -> list[dict]:
        return [{"state": s, "n": self.counts[s], **ITEM_STATE[s]} for s in ("MET", "CANDIDATE", "IN_PROGRESS", "NOT_STARTED") if self.counts.get(s)]

    @property
    def line(self) -> str:
        if not self.total:
            return ""
        parts = [f"{self.counts.get('MET', 0)} of {self.total} met"]
        if self.counts.get("CANDIDATE"):
            parts.append(f"{self.counts['CANDIDATE']} candidate")
        if self.counts.get("WAIVED"):
            parts.append(f"{self.counts['WAIVED']} waived")
        return " · ".join(parts)


def summarise(states: list[str]) -> EvidenceSummary:
    summary = EvidenceSummary()
    for state in states:
        summary.counts[state] = summary.counts.get(state, 0) + 1
        summary.total += 1
    return summary


def gate_view(gate: sqlite3.Row, today_: date, holidays: set[date], confirmation: sqlite3.Row | None, tz=None) -> dict:
    gdate = date.fromisoformat(gate["date"])
    health = confirmation["value"] if confirmation else "UNKNOWN"
    return {
        "row": gate, "id": gate["id"], "key": gate["key"], "name": gate["name"], "date": gate["date"],
        "short": short_date(gate["date"]), "long": long_date(gate["date"]), "is_past": gdate < today_,
        "left": working_days_left(today_, gdate, holidays), "past": working_days_past(today_, gdate, holidays),
        "health": health, "status": STATUS[health], "confirmation": confirmation,
        "confirmed_on": short_date(local_stamp(confirmation["created_at"], tz).date().isoformat()) if confirmation else "",
        "outcome": OUTCOME.get(gate["outcome"]),
    }


def gate_proposal(view, proposal):
    if not proposal or view["outcome"]:
        return view
    view["proposal"] = dict(proposal, status=STATUS[proposal["value"]], reasons=json.loads(proposal["reasons"]))
    view["needs_call"] = not view["confirmation"] or view["confirmation"]["value"] != proposal["value"]
    view["status"] = STATUS[proposal["value"]] if view["needs_call"] else view["status"]
    return view


def blocker_view(b: sqlite3.Row, repos: dict[int, str]) -> dict:
    age = f"{b['age_days']}d" if b["age_days"] else "!"
    return {"row": b, "age": age, "word": BLOCKER_WORD.get(b["kind"], b["kind"]), "detail": b["detail"], "who": b["person_raw"],
            "model": b["kind"] == "STATED",
            "src": src_link(b, repos)}


# ---------------------------------------------------------------- V-1 Today

def pull_queue(conn, head):
    snap = head["snapshot"]
    sources = views.pull_sources(conn, snap["run_id"] if snap else None)
    refreshed = {s["id"] for s in sources if s["refreshed"]}
    groups = {label: [] for label in ("Needs attention", "Ready to merge", "Waiting", "Draft", "Unverified")}
    reviews = views.pull_reviews(conn, snap["id"] if snap else None)
    for pr in views.open_pulls(conn, snap["id"] if snap else None):
        stale = head["stale"] or pr["carried_over"] or pr["source_id"] not in refreshed
        result = assess(pr, stale=stale)
        groups[result.label].append({"pr": pr, "reason": result.reason, "url": src_link(pr, {})["url"],
                                     "reviews": reviews.get((pr["project_id"], pr["source_id"], pr["number"]), [])})
    return {"groups": groups, "unread": [s for s in sources if not s["refreshed"]], "sources": sources}


def build_today(conn: sqlite3.Connection, now: datetime, head: dict) -> dict:
    day = today(conn, now)
    tz = zone(conn)
    snap = head["snapshot"]
    snap_id = snap["id"] if snap else None
    states = views.item_states(conn, snap_id)
    proposals = views.health_proposals(conn, snap_id)
    confirmations = hand.latest_confirmations(conn, list(proposals))
    repos = repos_of(conn)
    changes = {}
    for row in views.changes(conn, snap_id):
        changes.setdefault(row["project_id"], []).append(row)
    blocks = []
    open_ids: list[int] = []
    for p in hand.list_projects(conn):
        holidays = hand.holiday_dates(conn, p["id"])
        gates = hand.list_gates(conn, p["id"])
        nxt, later = split_next_gate(gates)
        open_ids += [g["id"] for g in gates if not g["outcome"]]
        sources = [s for s in hand.list_sources(conn, p["id"]) if s["enabled"]]
        blockers = sorted(views.snapshot_rows(conn, "blocker", snap_id, p["id"]), key=lambda b: -b["age_days"])
        aidlc = bool(views.facts(conn, snap_id, p["id"], "unit.exists"))
        block = {
            "project": p, "tile": COLOUR_TILE[p["colour"]], "colour": p["colour"], "next": None,
            "later": [{"key": g["key"], "short": short_date(g["date"])} for g in later],
            "evidence": None, "sources": len(sources), "aidlc": aidlc,
            "blockers": [blocker_view(b, repos) for b in blockers[:4]], "blocker_count": len(blockers),
            "open_proposals": views.open_proposal_count(conn, p["id"]) if sources else 0,
            "changes": {label: len(items) for label, items in highlights(changes[p["id"]]).items()} if p["id"] in changes else {},
            "num_changes": len(changes.get(p["id"], [])),
        }
        if nxt is not None:
            conf = hand.latest_confirmations(conn, [nxt["id"]]).get(nxt["id"])
            block["next"] = gate_proposal(gate_view(nxt, day, holidays, conf, tz), proposals.get(nxt["id"]))
            items = hand.list_items(conn, [nxt["id"]])[nxt["id"]]
            block["evidence"] = summarise([item_state(i, states.get(i["id"])) for i in items])
        blocks.append(block)
    blocks.sort(key=lambda b: (b["next"] is None, b["next"]["date"] if b["next"] else "", b["project"]["name"].lower()))
    need = need_call_count(open_ids, {i: p["value"] for i, p in proposals.items()}, {i: c["value"] for i, c in confirmations.items()})
    problems = conn.execute("SELECT count(*) FROM run_problem WHERE run_id = ?", (snap["run_id"],)).fetchone()[0] if snap else 0
    read = conn.execute("SELECT count(DISTINCT source_id) FROM run_source WHERE run_id = ? AND ok = 1", (snap["run_id"],)).fetchone()[0] if snap else 0
    return {"day": day, "day_text": full_date(day), "blocks": blocks, "need": need, "count": len(blocks), "snapshot": snap,
            "pulls": pull_queue(conn, head), "head": head,
            "snap_when": when_text(snap["created_at"], tz, now) if snap else "", "sources_read": read, "problems": problems,
            "digest": views.digest(conn), "prose_failed": bool(snap and any(r["stage"] == "ST-4" and r["error"] for r in views.model_reads(conn, snap["run_id"])))}


# ---------------------------------------------------------------- V-5 Project

def build_project(conn: sqlite3.Connection, project: sqlite3.Row, now: datetime) -> dict:
    day = today(conn, now)
    tz = zone(conn)
    holidays = hand.holiday_dates(conn, project["id"])
    gates = hand.list_gates(conn, project["id"])
    closed = [g for g in hand.list_gates(conn, project["id"], include_closed=True) if g["closed_at"]]
    confs = hand.latest_confirmations(conn, [g["id"] for g in gates])
    items = hand.list_items(conn, [g["id"] for g in gates])
    links = hand.list_links(conn, [i["id"] for its in items.values() for i in its])
    snap = views.current_snapshot(conn)
    states = views.item_states(conn, snap["id"] if snap else None)
    proposals = views.health_proposals(conn, snap["id"] if snap else None)
    gate_views = []
    for g in gates:
        view = gate_proposal(gate_view(g, day, holidays, confs.get(g["id"]), tz), proposals.get(g["id"]))
        view["items"] = []
        for i in items[g["id"]]:
            state = item_state(i, states.get(i["id"]))
            run = states.get(i["id"])
            view["items"].append({"row": i, "state": state, "look": ITEM_STATE[state], "links": links[i["id"]],
                                  "reason": run["reason"] if run and not i["hand_state"] else ""})
        view["summary"] = summarise([it["state"] for it in view["items"]])
        view["history"] = [{"row": c, "when": f"{local_stamp(c['created_at'], tz):%a %d %b %Y, %H:%M}"}
                           for c in hand.confirmation_history(conn, g["id"])]
        gate_views.append(view)
    tasks = [{"row": t, "late": task_days_late(t, day, holidays), "status_word": TASK_STATUS_WORD[t["status"]]} for t in hand.list_tasks(conn, project["id"])]
    sources = hand.list_sources(conn, project["id"])
    reads = views.last_source_reads(conn, project["id"])
    return {
        "project": project, "tile": COLOUR_TILE[project["colour"]], "day": day, "gates": gate_views, "closed": closed,
        "tasks": tasks, "late_count": sum(1 for t in tasks if t["late"]), "holidays": hand.list_holidays(conn, project["id"]),
        "global_holidays": hand.list_holidays(conn), "people": hand.list_people(conn), "link_kinds": LINK_WORD,
        "sources": [{"row": s, "read": reads.get(s["id"]), "read_when": when_text(reads[s["id"]]["commit_date"], tz, now) if s["id"] in reads and reads[s["id"]]["commit_date"] else ""} for s in sources],
        "setting": hand.project_setting(conn, project["id"]),
        "team": hand.project_team(conn, project["id"]), "onboardings": views.onboardings(conn, project["id"])[:5],
        "open_proposals": views.open_proposal_count(conn, project["id"]),
    }


# ---------------------------------------------------------------- V-3 Units

def build_units(conn: sqlite3.Connection, project: sqlite3.Row) -> dict:
    snap = views.current_snapshot(conn)
    sid = snap["id"] if snap else None
    pid = project["id"]
    unit_names = [f["subject_key"] for f in views.facts(conn, sid, pid, "unit.exists")]
    states = views.snapshot_rows(conn, "unit_state", sid, pid)
    stages = views.snapshot_rows(conn, "unit_stage_state", sid, pid)
    names = sorted(set(unit_names) | {s["unit"] for s in states})
    plans = views.facts(conn, sid, pid, "plan.steps")
    prs = [p for p in views.snapshot_rows(conn, "pr_state", sid, pid) if p["state"] == "OPEN"]
    blockers = views.snapshot_rows(conn, "blocker", sid, pid)
    disagreements = {(f["subject_key"], f["value"].get("folders")): f for f in views.facts(conn, sid, pid, "source.disagreement")}
    stale = views.facts(conn, sid, pid, "source.stale_input")
    repos = repos_of(conn)
    rows = []
    counts = {"design": 0, "code": 0, "none": 0}
    for name in names:
        cells = []
        for stage in STAGES:
            here = [s for s in stages if s["unit"] == name and s["stage"] == stage.key]
            state = "APPROVED" if any(s["state"] == "APPROVED" for s in here) else ("ARTIFACT_PRESENT" if here else "NONE")
            flag = disagreements.get((name, stage.key))
            cells.append({"state": state, "flag": flag, "src": src_link(here[0], repos) if here else {"url": "", "label": ""}})
        unit_state = next((s for s in states if s["unit"] == name), None)
        plan = next((p for p in plans if p["subject_key"].startswith(name + "-")), None)
        unit_blockers = [b for b in blockers if (b["subject_type"] == "UNIT" and b["subject_key"] == name)
                         or (b["subject_type"] == "PR" and any(str(p["number"]) == b["subject_key"] and p["unit"] == name for p in prs))]
        started = [c for c in cells if c["state"] != "NONE"]
        if cells[-1]["state"] != "NONE":
            counts["code"] += 1
        elif started:
            counts["design"] += 1
        else:
            counts["none"] += 1
        rows.append({"unit": name, "lead": unit_state["lead_raw"] if unit_state else "", "status": unit_state["status"] if unit_state else "",
                     "cells": cells, "plan": plan["value"] if plan else None, "prs": sum(1 for p in prs if p["unit"] == name),
                     "blockers": len(unit_blockers)})
    return {"project": project, "stages": STAGES, "rows": rows, "counts": counts, "stale": stale[0] if stale else None,
            "stale_src": src_link(stale[0], repos) if stale else None, "snapshot": snap}


# ---------------------------------------------------------------- V-2 Timeline

TIMELINE_COLOURS = {"IN_PROGRESS": "#5b8cff", "DONE": "#8b8894", "NOT_STARTED": "#46424f", "BLOCKED": "#f0414f", "DEFERRED": "#2f2c36", "": "#46424f"}


def project_tasks(conn: sqlite3.Connection, project: sqlite3.Row, day: date, *, include_hand: bool = False) -> list[dict]:
    snap = views.current_snapshot(conn)
    holidays = hand.holiday_dates(conn, project["id"])
    out = []
    settings = hand.project_setting(conn, project["id"])
    enabled = {s["id"] for s in hand.list_sources(conn, project["id"]) if s["enabled"]}
    for t in views.snapshot_rows(conn, "task_state", snap["id"] if snap else None, project["id"]):
        if (not is_wbs_path(t["src_path"] or "") or t["src_source_id"] not in enabled or
                t["src_source_id"] != settings["wbs_source_id"] or t["src_path"] != settings["wbs_path"]):
            continue
        out.append({"key": t["key"], "title": t["title"], "owner": t["owner_raw"], "gate": (t["wbs_gates"] or t["gate_key"] or "").split(";")[0],
                    "start": t["start"], "finish": t["finish"], "status": t["status"], "origin": "WBS",
                    "late": task_days_late(t, day, holidays), "row": t})
    if not include_hand:
        return out
    for t in hand.list_tasks(conn, project["id"]):
        out.append({"key": t["key"], "title": t["title"], "owner": t["owner"] or "", "gate": t["gate_key"], "start": t["start"],
                    "finish": t["finish"], "status": t["status"], "origin": "HAND", "late": task_days_late(t, day, holidays), "row": t})
    return out


def timeline_svg(tasks: list[dict], day: date) -> str:
    """V-2 drawn on the server as SVG (Design §6-2). Late and blocked are labelled in text."""
    dated = [t for t in tasks if t["start"] or t["finish"]]
    if not dated:
        return ""
    dates = [date.fromisoformat(d) for t in dated for d in (t["start"], t["finish"]) if d] + [day]
    first = min(dates) - timedelta(days=min(dates).weekday())
    last = max(dates) + timedelta(days=3)
    span = max((last - first).days, 14)
    width, left, right, top, rh = 1000, 262, 985, 58, 30
    dw = (right - left) / span

    def x(d: date) -> float:
        return left + (d - first).days * dw

    groups: dict[str, list[dict]] = {}
    for t in sorted(dated, key=lambda t: (t["start"] or t["finish"], t["key"])):
        groups.setdefault(t["gate"] or "", []).append(t)
    order = list(groups)
    height = top + sum(len(groups[k]) + 1 for k in order) * rh + 10
    out = [f'<svg class="timeline" viewBox="0 0 {width} {height}" role="img" aria-label="Timeline of tasks and gates. The table view has the same rows.">']
    d = first
    while d <= last:
        if d.weekday() == 5:
            out.append(f'<rect x="{x(d):.1f}" y="{top}" width="{2 * dw:.1f}" height="{height - top - 6}" fill="rgba(255,255,255,.035)"/>')
        if d.weekday() == 0:
            out.append(f'<line x1="{x(d):.1f}" x2="{x(d):.1f}" y1="{top - 6}" y2="{height - 6}" stroke="rgba(255,255,255,.08)"/>')
            out.append(f'<text x="{x(d) + 4:.1f}" y="{top - 12}">{d.day} {d:%b}</text>')
        d += timedelta(days=1)
    tx = x(day) + dw / 2
    out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{top - 6}" y2="{height - 6}" stroke="#c9f23f" stroke-width="2.5"/>')
    out.append(f'<text x="{left - 12}" y="{top - 12}" text-anchor="end" class="today">Today · {day:%a} {day.day} {day:%b}</text>')
    y = top
    for key in order:
        title = key or "No gate"
        out.append(f'<text class="grp" x="8" y="{y + 19}">{escape(title[:40])}</text>')
        y += rh
        for t in groups[key]:
            start = date.fromisoformat(t["start"] or t["finish"])
            finish = date.fromisoformat(t["finish"] or t["start"])
            bx, bw = x(start), max((finish - start).days + 1, 1) * dw
            name = t["title"] if len(t["title"]) <= 29 else t["title"][:28] + "…"
            out.append(f'<text class="key" x="8" y="{y + 19}">{escape(t["key"][:10])}</text><text class="lab" x="62" y="{y + 19}">{escape(name)}</text>')
            colour = TIMELINE_COLOURS.get(t["status"], "#46424f")
            stroke = ' stroke="#5a5663"' if t["status"] == "DEFERRED" else ""
            out.append(f'<rect x="{bx:.1f}" y="{y + 8}" width="{max(bw, 6):.1f}" height="14" rx="7" fill="{colour}"{stroke}><title>{escape(t["key"])} {escape(t["title"])}: {TASK_STATUS_WORD.get(t["status"], "")}</title></rect>')
            end = bx + max(bw, 6)
            label = ""
            if t["late"]:
                lw = max(x(day + timedelta(days=1)) - end - 2, 6)
                out.append(f'<rect x="{end + 2:.1f}" y="{y + 8}" width="{lw:.1f}" height="14" rx="7" fill="#f0414f"/>')
                end += 2 + lw
                label = f"{t['late']}d late"
            if t["status"] == "BLOCKED":
                label = "blocked" + (f" · {label}" if label else "")
            elif t["status"] == "DONE":
                label = "✓ done"
            elif t["status"] == "DEFERRED":
                label = "deferred"
            if label:
                style = ' class="warnlabel"' if (t["late"] or t["status"] == "BLOCKED") else ""
                out.append(f'<text x="{end + 7:.1f}" y="{y + 19}"{style}>{escape(label)}</text>')
            y += rh
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- V-9 Onboarding review

def proposal_origin(p: dict, repos: dict[int, str]) -> dict:
    src = src_link(_RowLike(p), repos)
    if p["kind"] == "GATE":
        where = src["label"]
        words = {"PLAN_TABLE": f"from a table in {where}", "WBS": f"from the WBS, {where}", "PROSE": f"from prose in {where}"}
        src["words"] = words.get(p["date_origin"], f"from {where}")
    else:
        src["words"] = ("read by program, " if p["read_by"] == "PROGRAM" else "read from prose, ") + src["label"]
    return src


class _RowLike(dict):
    def keys(self):  # noqa: D401 - mimic sqlite3.Row for src_link
        return super().keys()


def build_review(conn: sqlite3.Connection, project: sqlite3.Row, onboarding: sqlite3.Row, now: datetime) -> dict:
    tz = zone(conn)
    repos = repos_of(conn)
    proposals = views.proposals_with_decisions(conn, onboarding["id"])
    gate_keys = {g["key"] for g in hand.list_gates(conn, project["id"])}
    groups: dict[str, list[dict]] = {k: [] for k in KIND_WORD}
    for p in proposals:
        p["origin"] = proposal_origin(p, repos)
        p["gate_ready"] = p["kind"] != "EVIDENCE" or p["payload"].get("gate_key") in gate_keys
        groups[p["kind"]].append(p)
    started = local_stamp(onboarding["started_at"], tz)
    ended = local_stamp(onboarding["ended_at"], tz) if onboarding["ended_at"] else None
    return {
        "project": project, "onboarding": onboarding, "groups": groups, "kind_word": KIND_WORD,
        "open": sum(1 for p in proposals if p["open"]), "total": len(proposals),
        "sources": views.onboarding_sources(conn, onboarding["id"]), "problems": views.onboarding_problems(conn, onboarding["id"]),
        "took": f"{int((ended - started).total_seconds())} s" if ended else "", "started": when_text(onboarding["started_at"], tz, now),
        "tile": COLOUR_TILE[project["colour"]],
    }


def json_pretty(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False)
