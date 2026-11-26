"""The People view combines current repository facts with hand-entered tasks."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..data import hand, views
from ..rules.gates import FINISHED_TASK
from ..rules.workdays import is_working_day, working_days_between
from . import present


def _due_through(day, holidays):
    count = 0
    while count < 5:
        day += timedelta(days=1)
        count += is_working_day(day, holidays)
    return day


def _started(stamp, fallback, tz):
    for candidate in (stamp, fallback):
        if candidate:
            try:
                return present.local_stamp(candidate, tz).date()
            except ValueError:
                continue
    return None


def build(conn, now: datetime):
    snapshot = views.current_snapshot(conn)
    snap_id = snapshot["id"] if snapshot else None
    day, tz = present.today(conn, now), present.zone(conn)
    repos = present.repos_of(conn)
    people = hand.list_people(conn)
    by_name = {p["display_name"].strip().casefold(): p["id"] for p in people}
    aliases = {(r["kind"], r["value"].strip().casefold()): r["person_id"] for r in conn.execute("SELECT * FROM person_alias")}
    cards = {p["id"]: {"id": p["id"], "name": p["display_name"], "colour": hand.COLOURS[index % len(hand.COLOURS)],
                       "reviews": [], "prs": [], "in_work": [], "late": [], "due": []}
             for index, p in enumerate(people)}

    def person(kind, raw, stored):
        name = raw.strip().casefold()
        return aliases.get((kind, name), by_name.get(name, stored))

    for project in hand.list_projects(conn):
        pid = project["id"]
        holidays = hand.holiday_dates(conn, pid)
        horizon = _due_through(day, holidays)
        pulls = {(r["source_id"], r["number"]): r for r in views.snapshot_rows(conn, "pr_state", snap_id, pid) if r["state"] == "OPEN"}
        for pr in pulls.values():
            author = person("GITHUB", pr["author_raw"], pr["author_person_id"])
            if author in cards:
                cards[author]["prs"].append({"project": project["key"], "title": pr["title"], "src": present.citation(pr, repos, "pr"),
                                             "carried": pr["carried_over"]})
        for review in views.snapshot_rows(conn, "review_state", snap_id, pid):
            pr = pulls.get((review["source_id"], review["pr_number"]))
            if review["state"] != "PENDING" or pr is None:
                continue
            reviewer = person("GITHUB", review["reviewer_raw"], review["reviewer_person_id"])
            if reviewer in cards:
                started = _started(review["requested_at"], pr["created_at"], tz)
                cards[reviewer]["reviews"].append({"project": project["key"], "title": pr["title"], "started": started,
                                                    "age": working_days_between(started, day, holidays) if started else None,
                                                    "src": present.citation(review, repos, "review"), "carried": review["carried_over"]})
        for task in present.timeline_tasks(conn, project, day):
            row = task["row"]
            owner = row["owner_person_id"] if task["origin"] == "HAND" else person("WBS", task["owner"], row["owner_person_id"])
            if owner not in cards or row["status"] in FINISHED_TASK:
                continue
            src = present.hand_task_citation(project["key"], row) if task["origin"] == "HAND" else present.citation(row, repos, "task")
            entry = {"project": project["key"], "title": f"{row['key']} · {row['title']}", "src": src,
                     "finish": row["finish"], "carried": task["origin"] == "WBS" and row["carried_over"],
                     "late": task["late"]}
            if row["status"] == "IN_PROGRESS":
                cards[owner]["in_work"].append(entry)
            if entry["late"]:
                cards[owner]["late"].append(entry)
            if row["finish"] and day.isoformat() < row["finish"] <= horizon.isoformat():
                cards[owner]["due"].append(entry)

    for card in cards.values():
        card["reviews"].sort(key=lambda r: (-(r["age"] if r["age"] is not None else -1), r["started"] or date.max, r["title"]))
        card["late"].sort(key=lambda t: (-t["late"], t["title"]))
        card["due"].sort(key=lambda t: (t["finish"], t["title"]))
    return {"cards": sorted(cards.values(), key=lambda c: (-len(c["reviews"]), c["name"].casefold())),
            "people": people, "unmatched": views.unmatched_names(conn, snap_id)}
