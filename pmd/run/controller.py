"""``pm-dash run``: ST-1 Fetch, ST-2 Extract, ST-3 Compare, ST-8 Publish (Part 3 §10).

The model stages (ST-4, ST-5), the diff, the digest and the story come in Phase 3.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..adapters import Result, aidlc, github as gh_adapter, wbs
from ..config import Config
from ..data import db, run as R
from ..rules import compare
from ..sources import git
from ..sources.github import GitHubError, ReadOnlyGitHub, fetch_repo, read_token
from .lock import Busy, exclusive

GitHubFactory = Callable[[str | None, Callable], ReadOnlyGitHub]


@dataclass
class Outcome:
    run_id: int | None
    result: str
    message: str = ""
    snapshot_id: int | None = None
    problems: list[str] = field(default_factory=list)


def tz_of(conn: sqlite3.Connection):
    name = R.setting(conn, "time_zone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo


def local_day_bounds(day: date, tz) -> tuple[str, str]:
    start = datetime.combine(day, time(0), tzinfo=tz)
    return R.stamp(start), R.stamp(start + timedelta(days=1))


def default_github(token: str | None, log: Callable) -> ReadOnlyGitHub:
    return ReadOnlyGitHub(token, log=log)


def run(config: Config, *, trigger: str = "HAND", project_key: str | None = None, baseline: bool = False,
        now: datetime | None = None, github_factory: GitHubFactory = default_github) -> Outcome:
    now = now or datetime.now(UTC)
    try:
        with exclusive(config.data_dir / "run.lock", f"run ({trigger.lower()})"):
            conn = db.open_store(config.db_path)
            try:
                return _run(conn, config, trigger, project_key, baseline, now, github_factory)
            finally:
                conn.close()
    except Busy as exc:
        return Outcome(None, "REFUSED", str(exc))


def _run(conn, config, trigger, project_key, baseline, now, github_factory) -> Outcome:
    tz = tz_of(conn)
    local_now = now.astimezone(tz)
    today = local_now.date()
    if trigger == "SCHEDULE":
        if today.weekday() >= 5:  # R-R2
            return Outcome(None, "SKIPPED", f"{today:%A}: no scheduled run at the weekend")
        if R.scheduled_run_published_on(conn, *local_day_bounds(today, tz)):  # R-R3
            return Outcome(None, "SKIPPED", "a scheduled run already published a snapshot today")

    projects = R.projects(conn, project_key)
    if project_key and not projects:
        return Outcome(None, "REFUSED", f"no active project {project_key}")
    prev = R.current_snapshot(conn)
    prev_id = prev["id"] if prev else None
    since = datetime.fromisoformat(prev["created_at"].replace("Z", "+00:00")) if prev and not baseline else None
    run_id = R.start_run(conn, trigger, project_key or "", baseline or prev is None, now)
    snap = R.new_snapshot(conn, run_id, now)
    work = config.data_dir / "runs" / str(run_id)
    token = read_token(config.data_dir / "secrets" / "github-token")
    problems: list[str] = []

    def log_request(method, host, path, status):
        R.log_request(conn, run_id=run_id, onboarding_id=None, method=method, host=host, path=path, status=status)

    linked = read_any = failed_any = False
    try:
        for p in projects:
            holidays = {date.fromisoformat(d) for d in R.holidays(conn, p["id"])}
            sources = R.sources(conn, p["id"])
            linked = linked or bool(sources)
            result = Result()
            newest_commit = ""
            read_sources: dict[int, tuple[str, Path]] = {}
            for s in sources:
                try:
                    mirror = config.data_dir / "mirrors" / f"{s['id']}.git"
                    git.ensure_mirror(s["git_url"], mirror, token_file=config.data_dir / "secrets" / "github-token")
                    names = [b.strip() for b in s["branches"].split(",") if b.strip()] or [git.default_branch(mirror)]
                    first_sha = ""
                    for branch in names:
                        if not git.branch_exists(mirror, branch):
                            R.add_source_read(conn, run_id=run_id, source_id=s["id"], branch=branch, ok=False, error="branch not found")
                            R.add_problem(conn, run_id=run_id, project_id=p["id"], stage="ST-1", adapter="git", reason=f"{s['key']}: branch {branch} not found")
                            continue
                        sha, when = git.head(mirror, branch)
                        first_sha = first_sha or sha
                        newest_commit = max(newest_commit, when)
                        R.add_source_read(conn, run_id=run_id, source_id=s["id"], branch=branch, sha=sha, date=when, ok=True)
                    if not first_sha:
                        raise git.GitError("no readable branch")
                    checkout = work / "checkout" / s["key"]
                    if s["role"] == "DOCS":
                        paths = ["aidlc-docs"]
                        if p["wbs_source_id"] == s["id"] and p["wbs_path"]:
                            paths.append(p["wbs_path"])
                        if p["dashboard_source_id"] == s["id"] and p["dashboard_json_path"]:
                            paths.append(p["dashboard_json_path"])
                        git.export(mirror, first_sha, checkout, paths)
                    elif (p["wbs_source_id"] == s["id"] and p["wbs_path"]) or (p["dashboard_source_id"] == s["id"] and p["dashboard_json_path"]):
                        git.export(mirror, first_sha, checkout, [x for x in (p["wbs_path"], p["dashboard_json_path"]) if x])
                    read_sources[s["id"]] = (first_sha, checkout)
                    read_any = True
                    if s["github_repo"]:
                        if token:
                            client = github_factory(token, log_request)
                            try:
                                fetch_repo(client, s["github_repo"], work / "github" / s["key"], since=since)
                            finally:
                                client.close()
                        else:
                            R.add_problem(conn, run_id=run_id, project_id=p["id"], stage="ST-1", adapter="github",
                                          reason=f"{s['key']}: no GitHub token, so PRs, reviews and issues were not read")
                except (git.GitError, GitHubError, OSError) as exc:
                    failed_any = True
                    R.add_source_read(conn, run_id=run_id, source_id=s["id"], branch="", ok=False, error=str(exc))
                    R.add_problem(conn, run_id=run_id, project_id=p["id"], stage="ST-1", adapter="git", reason=f"{s['key']}: {exc}")
                    problems.append(f"{p['key']}/{s['key']}: {exc}")
                    read_sources.pop(s["id"], None)
                    if prev_id:
                        R.carry_over(conn, prev_id, snap, p["id"], s["id"])

            # ---------------------------------------------------------------- ST-2 Extract
            gate_keys = {g["key"] for g in R.gates(conn, p["id"])}
            dashboard_updated = ""
            for s in sources:
                if s["id"] not in read_sources:
                    continue
                sha, checkout = read_sources[s["id"]]
                if s["role"] == "DOCS" and aidlc.probe(checkout):
                    result.merge(aidlc.extract(checkout, source_id=s["id"], commit=sha, project_id=p["id"], snapshot_id=snap))
                if p["dashboard_source_id"] == s["id"] and p["dashboard_json_path"]:
                    rows, dashboard_updated = aidlc.extract_dashboard(checkout, p["dashboard_json_path"], source_id=s["id"], commit=sha,
                                                                      project_id=p["id"], snapshot_id=snap)
                    result.merge(rows)
                if p["wbs_source_id"] == s["id"] and p["wbs_path"]:
                    result.merge(wbs.extract(checkout, p["wbs_path"], p["wbs_mapping"], source_id=s["id"], commit=sha,
                                             project_id=p["id"], snapshot_id=snap, gate_keys=gate_keys))
                result.merge(gh_adapter.extract(work / "github" / s["key"], source_id=s["id"], project_id=p["id"], snapshot_id=snap,
                                                branch_pattern=p["branch_unit_pattern"], task_key_pattern=p["task_key_pattern"]))
            _match_people(conn, result, snap, p["id"])
            _carry_requested_at(conn, result, prev_id, p["id"])
            for table in ("unit_state", "unit_stage_state", "task_state", "pr_state", "review_state", "issue_state", "fact", "unmatched_name"):
                R.insert_many(conn, table, result.rows.get(table, []))
            for prob in result.problems:
                R.add_problem(conn, run_id=run_id, project_id=p["id"], stage="ST-2", adapter=prob.adapter, path=prob.path, line=prob.line, reason=prob.reason)

            # ---------------------------------------------------------------- ST-3 Compare
            _compare(conn, p, snap, prev_id, today, holidays, dashboard_updated, newest_commit)

        if linked and not read_any:  # §10-8: no source of any linked project could be read
            R.end_run(conn, run_id, "FAILED", "no source could be read: " + "; ".join(problems)[:1500])
            return Outcome(run_id, "FAILED", "no source could be read", problems=problems)
        R.publish(conn, snap)
        result_word = "PARTIAL" if failed_any else "OK"
        R.end_run(conn, run_id, result_word)
        return Outcome(run_id, result_word, snapshot_id=snap, problems=problems)
    except Exception as exc:  # noqa: BLE001 - a run that dies must still show on V-7 (§10-1)
        R.end_run(conn, run_id, "FAILED", f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        _prune_work(config.data_dir / "runs", keep_days=14)


def _match_people(conn, result: Result, snap: int, project_id: int) -> None:
    index = R.people_index(conn)
    unmatched: dict[tuple[str, str], str] = {}
    pairs = (("task_state", "owner_raw", "owner_person_id", "WBS"), ("pr_state", "author_raw", "author_person_id", "GITHUB"),
             ("review_state", "reviewer_raw", "reviewer_person_id", "GITHUB"), ("unit_state", "lead_raw", "lead_person_id", "DOC"))
    for table, raw, pid, kind in pairs:
        for row in result.rows.get(table, []):
            name = (row.get(raw) or "").strip()
            if not name:
                continue
            person = index.get(name.lower())
            row[pid] = person
            if person is None:
                unmatched.setdefault((kind, name), table)
    for (kind, name), seen in unmatched.items():
        result.add("unmatched_name", {"snapshot_id": snap, "project_id": project_id, "kind": kind, "value": name, "seen_in": seen})


def _carry_requested_at(conn, result: Result, prev_id: int | None, project_id: int) -> None:
    """When GitHub gave no request time, keep the first time a pending review was seen."""
    previous = {(r["source_id"], r["pr_number"], r["reviewer_raw"]): r["requested_at"]
                for r in R.previous_rows(conn, "review_state", prev_id, project_id) if r["state"] == "PENDING"}
    for row in result.rows.get("review_state", []):
        if row["state"] == "PENDING" and not row.get("requested_at"):
            row["requested_at"] = previous.get((row["source_id"], row["pr_number"], row["reviewer_raw"]), "") or R.stamp()


def _compare(conn, p, snap: int, prev_id: int | None, today: date, holidays: set[date], dashboard_updated: str, newest_commit: str) -> None:
    pid = p["id"]
    rows = {t: [dict(r) for r in R.rows(conn, t, snap, pid)] for t in ("pr_state", "review_state", "task_state", "unit_state", "unit_stage_state")}
    hand_tasks = [dict(r) for r in R.hand_tasks(conn, pid)]
    wait_days = int(R.setting(conn, "review_wait_days", "2") or 2)
    found = compare.blockers(prs=rows["pr_state"], reviews=rows["review_state"], tasks=rows["task_state"], hand_tasks=hand_tasks,
                             units=rows["unit_state"], today=today, holidays=holidays, review_wait_days=wait_days)
    compare.carry_since(found, R.previous_rows(conn, "blocker", prev_id, pid))
    R.insert_many(conn, "blocker", [{"snapshot_id": snap, "project_id": pid, **b} for b in found])

    # R-D1: the unit's stage from its folders against the stage the dashboard file names.
    order = [s.key for s in aidlc.STAGES]
    facts = []
    for u in rows["unit_state"]:
        by_folder = compare.current_stage_by_artifacts(u["unit"], rows["unit_stage_state"], order)
        named = aidlc.stage_of_text(f"{u['status']} {u['stage']}")
        if by_folder and named and named.key != by_folder and order.index(by_folder) > order.index(named.key):
            facts.append({"snapshot_id": snap, "project_id": pid, "subject_type": "unit", "subject_key": u["unit"],
                          "kind": "source.disagreement",
                          "value": json.dumps({"field": "stage", "folders": by_folder, "dashboard": named.key,
                                               "dashboard_updated": dashboard_updated}),
                          **{k: u[k] for k in R.SRC_COLUMNS}})
    if dashboard_updated and compare.is_stale_input(dashboard_updated, newest_commit, today, holidays,
                                                     int(R.setting(conn, "stale_input_days", "3") or 3)):
        src = next(({k: u[k] for k in R.SRC_COLUMNS} for u in rows["unit_state"]), None)
        if src:
            facts.append({"snapshot_id": snap, "project_id": pid, "subject_type": "file", "subject_key": p["dashboard_json_path"],
                          "kind": "source.stale_input", "value": json.dumps({"updated": dashboard_updated, "newest_commit": newest_commit}),
                          **{**src, "src_line_from": None, "src_line_to": None}})
    R.insert_many(conn, "fact", facts)

    items = R.evidence(conn, pid)
    links = R.evidence_links(conn, [i["id"] for i in items])
    states = []
    for item in items:
        state, reason = compare.item_state(links[item["id"]], tasks=rows["task_state"], hand_tasks=hand_tasks, prs=rows["pr_state"],
                                           unit_states=rows["unit_state"], unit_stages=rows["unit_stage_state"])
        states.append({"snapshot_id": snap, "evidence_item_id": item["id"], "run_state": state, "reason": reason})
    R.insert_many(conn, "item_state", states)


def _prune_work(root: Path, keep_days: int) -> None:
    if not root.is_dir():
        return
    limit = datetime.now().timestamp() - keep_days * 86400
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < limit:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass
