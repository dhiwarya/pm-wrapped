"""ST-3 Compare (Design §5-2, §5-3, §5-5; Part 3 §10-4). Pure functions over rows."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from .gates import task_days_late
from .links import pr_matches_link
from .workdays import working_days_between

Row = Mapping[str, Any]
SRC = ("src_source_id", "src_commit", "src_path", "src_line_from", "src_line_to", "src_object", "src_number", "src_url")


def _src(row: Row) -> dict:
    return {k: row[k] for k in SRC if k in row.keys()}


def _day(stamp: str) -> date | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(stamp[:10])
        except ValueError:
            return None


def blockers(*, prs: Sequence[Row], reviews: Sequence[Row], tasks: Sequence[Row], hand_tasks: Sequence[Row],
             units: Sequence[Row], today: date, holidays: Collection[date], review_wait_days: int = 2) -> list[dict]:
    """The blockers a program can find (Design §5-3). ``since`` is the day it started."""
    out: list[dict] = []
    def source(row):
        return row["src_source_id"] if "src_source_id" in row.keys() else None

    open_prs = {(source(p), p["number"]): p for p in prs if p["state"] == "OPEN"}
    for r in reviews:
        pr = open_prs.get((source(r), r["pr_number"]))
        if pr is None or r["state"] != "PENDING":
            continue
        start = _day(r["requested_at"]) or _day(pr["created_at"])
        if start is None:
            continue
        age = working_days_between(start, today, holidays)
        if age >= review_wait_days:
            out.append({"kind": "REVIEW_WAITING", "subject_type": "PR", "subject_key": str(pr["number"]),
                        "detail": f"Review waiting {age} working days: PR {pr['number']} {pr['title']}", "since": start.isoformat(),
                        "age_days": age, "person_raw": r["reviewer_raw"], "person_id": r.get("reviewer_person_id"), **_src(pr)})
    for pr in open_prs.values():
        if pr["review_state"] == "CHANGES_REQUESTED":
            out.append({"kind": "CHANGES_REQUESTED", "subject_type": "PR", "subject_key": str(pr["number"]),
                        "detail": f"Changes requested: PR {pr['number']} {pr['title']}", "since": (_day(pr["updated_at"]) or today).isoformat(),
                        "age_days": 0, "person_raw": pr["author_raw"], "person_id": pr.get("author_person_id"), **_src(pr)})
        if pr["checks_state"] == "FAILING":
            out.append({"kind": "CHECK_FAILING", "subject_type": "PR", "subject_key": str(pr["number"]),
                        "detail": f"Checks failing: PR {pr['number']} {pr['title']}", "since": (_day(pr["updated_at"]) or today).isoformat(),
                        "age_days": 0, "person_raw": pr["author_raw"], "person_id": pr.get("author_person_id"), **_src(pr)})
    all_tasks = [("TASK", t, t["owner_raw"], t.get("owner_person_id")) for t in tasks] + \
                [("HAND_TASK", t, t["owner"] or "", t["owner_person_id"]) for t in hand_tasks]
    late_keys: set[str] = set()
    for kind, t, owner, pid in all_tasks:
        late = task_days_late(t, today, holidays)
        if late:
            late_keys.add(t["key"])
            out.append({"kind": "TASK_LATE", "subject_type": kind, "subject_key": t["key"],
                        "detail": f"Task late {late} working days: {t['key']} {t['title']}", "since": t["finish"],
                        "age_days": late, "person_raw": owner, "person_id": pid, **(_src(t) if kind == "TASK" else {})})
    for kind, t, owner, pid in all_tasks:
        if t["status"] in ("DONE", "DEFERRED") or t["key"] in late_keys:
            continue
        deps = [d.strip() for d in (t["depends_on"] or "").split(";") if d.strip()]
        waiting = [d for d in deps if d in late_keys]
        if waiting:
            out.append({"kind": "DEPENDENCY_LATE", "subject_type": kind, "subject_key": t["key"],
                        "detail": f"Task {t['key']} depends on late {', '.join(waiting)}", "since": today.isoformat(),
                        "age_days": 0, "person_raw": owner, "person_id": pid, **(_src(t) if kind == "TASK" else {})})
    for u in units:
        if u["blocked"]:
            out.append({"kind": "UNIT_BLOCKED", "subject_type": "UNIT", "subject_key": u["unit"],
                        "detail": f"Unit {u['unit']} blocked: {u['blocked']}", "since": today.isoformat(),
                        "age_days": 0, "person_raw": u["lead_raw"], "person_id": u.get("lead_person_id"), **_src(u)})
    return out


def carry_since(new: list[dict], previous: Sequence[Row]) -> None:
    """A blocker seen in the last snapshot keeps its first ``since`` (Part 3 §10-4)."""
    def identity(row):
        return tuple(row[key] if key in row.keys() else None for key in ("kind", "subject_type", "subject_key", "src_source_id", "person_raw"))

    first = {identity(p): p["since"] for p in previous}
    for b in new:
        old = first.get(identity(b))
        if old and old < b["since"]:
            b["since"] = old


def item_state(links: Sequence[Row], *, tasks: Sequence[Row], hand_tasks: Sequence[Row], prs: Sequence[Row],
               unit_states: Sequence[Row], unit_stages: Sequence[Row]) -> tuple[str, str]:
    """``run_state`` of one evidence item from its links (Part 3 §10-4). Never MET (R-E1)."""
    if not links:
        return "NOT_STARTED", "no links"
    every = [*tasks, *hand_tasks]
    found = 0
    started = 0
    finished = 0
    for link in links:
        kind, value = link["kind"], link["value"].strip()
        objects: list[tuple[bool, bool]] = []  # (started, finished)
        if kind in ("TASK", "TASK_PREFIX"):
            for t in every:
                if (kind == "TASK" and t["key"] == value) or (kind == "TASK_PREFIX" and t["key"].startswith(value)):
                    objects.append((t["status"] in ("IN_PROGRESS", "DONE", "BLOCKED"), t["status"] == "DONE"))
        elif kind in ("PR", "PR_SEARCH"):
            for p in prs:
                if p["state"] != "CLOSED" and pr_matches_link(kind, value, p):
                    objects.append((True, p["state"] == "MERGED"))
        elif kind == "UNIT":
            status = next((u["status"] for u in unit_states if u["unit"] == value), "")
            artifacts = any(s["unit"] == value for s in unit_stages)
            if status or artifacts:
                done = status.lower().startswith("merged")
                objects.append((done or artifacts or status.lower().startswith("in progress"), done))
        for s, f in objects:
            found += 1
            started += s
            finished += f
    if found == 0:
        return "NOT_STARTED", "nothing linked was found"
    if finished == found:
        return "CANDIDATE", f"all {found} linked done"
    if started:
        return "IN_PROGRESS", f"{finished} of {found} linked done"
    return "NOT_STARTED", f"none of {found} linked started"


def current_stage_by_artifacts(unit: str, unit_stages: Sequence[Row], order: list[str]) -> str:
    present = {s["stage"] for s in unit_stages if s["unit"] == unit and s["state"] == "ARTIFACT_PRESENT"}
    keys = [k for k in order if k in present]
    return keys[-1] if keys else ""


def is_stale_input(file_updated: str, newest_commit: str, today: date, holidays: Collection[date], days: int = 3) -> bool:
    """R-D3: the file's own date is older than ``days`` working days and the repository is newer."""
    updated, newest = _day(file_updated), _day(newest_commit)
    if updated is None or newest is None or newest <= updated:
        return False
    return working_days_between(updated, today, holidays) > days
