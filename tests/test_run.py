"""The run (Part 3 §10) end to end on an invented repository; PR-2, PR-10, PR-12."""

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pmd.data import hand
from pmd.run.controller import run
from pmd.run.lock import exclusive
from pmd.sources.github import ReadOnlyGitHub

from .fixtures import commit, make_aidlc_repo, saved_pulls

WED = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)  # Wednesday morning in Asia/Jakarta
SAT = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)


@pytest.fixture
def linked(conn, config, tmp_path):
    hand.set_setting(conn, "time_zone", "Asia/Jakarta")
    repo = make_aidlc_repo(tmp_path / "widgets")
    pid = hand.create_project(conn, "WIDGET", "Widget portal")
    sid = hand.add_source(conn, pid, str(repo))
    hand.create_gate(conn, pid, "G1", "Beta to pilot users", "2026-12-04")
    mapping = {"columns": {"key": "WBS No", "title": "Task", "gate": "Milestone", "owner": "Owner", "start": "Start",
                           "finish": "Finish", "status": "Status", "depends_on": "Depends on"},
               "date_format": "YYYY-MM-DD", "status": {"In progress": "IN_PROGRESS", "Not started": "NOT_STARTED", "Done": "DONE"}}
    hand.set_reading(conn, pid, wbs_source=str(sid), wbs_path="plan/wbs.csv", wbs_mapping=json.dumps(mapping),
                     dashboard_source=str(sid), dashboard_path="aidlc-docs/construction/dashboard/dashboard-state.json",
                     branch_pattern="unit/<unit>; <unit>/<topic>", task_key_pattern=r"\b\d\.\d{3}\b")
    gid = hand.list_gates(conn, pid)[0]["id"]
    item = hand.create_item(conn, gid, "Sign-in and invoices work")
    hand.add_link(conn, item, "TASK_PREFIX", "1.00")
    done_item = hand.create_item(conn, gid, "Runbook written")
    hand.add_link(conn, done_item, "TASK", "1.003")
    unlinked = hand.create_item(conn, gid, "Pilot list agreed")
    return {"pid": pid, "sid": sid, "repo": repo, "items": (item, done_item, unlinked)}


def rows(conn, table, snap):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE snapshot_id = ?", (snap,))]


def test_run_reads_a_repository_and_publishes_one_snapshot(config, conn, linked):
    out = run(config, now=WED)
    assert out.result == "OK", out
    snap = conn.execute("SELECT snapshot_id FROM current_snapshot").fetchone()[0]
    assert snap == out.snapshot_id
    tasks = {t["key"]: t for t in rows(conn, "task_state", snap)}
    assert tasks["1.001"]["status"] == "IN_PROGRESS" and tasks["1.001"]["src_line_from"] == 2
    units = {u["unit"] for u in rows(conn, "unit_state", snap)}
    assert units == {"alpha", "beta"}
    kinds = [(b["kind"], b["subject_key"]) for b in rows(conn, "blocker", snap)]
    assert ("TASK_LATE", "1.001") in kinds and ("DEPENDENCY_LATE", "1.002") in kinds and ("UNIT_BLOCKED", "beta") in kinds
    facts = {f["kind"]: f for f in rows(conn, "fact", snap)}
    assert json.loads(facts["source.disagreement"]["value"]) == {"field": "stage", "folders": "code-generation", "dashboard": "nfr-design",
                                                                 "dashboard_updated": "2026-11-10T07:00:00Z"}
    assert "source.stale_input" in facts
    states = {s["evidence_item_id"]: s["run_state"] for s in rows(conn, "item_state", snap)}
    item, done_item, unlinked = linked["items"]
    assert states == {item: "IN_PROGRESS", done_item: "CANDIDATE", unlinked: "NOT_STARTED"}
    problems = [r["reason"] for r in conn.execute("SELECT reason FROM run_problem WHERE run_id = ?", (out.run_id,))]
    assert any("unknown gate key G9" in p for p in problems)
    # every typed row carries a source reference (PR-3)
    for table in ("task_state", "unit_state", "unit_stage_state", "fact"):
        assert all(r["src_path"] or r["src_url"] for r in rows(conn, table, snap))
    unmatched = {u["value"] for u in rows(conn, "unmatched_name", snap)}
    assert {"Rani", "Bayu"} <= unmatched


def test_names_match_people_by_alias(config, conn, linked):
    pid = hand.add_person(conn, "Rani Putri")
    hand.add_alias(conn, pid, "WBS", "Rani")
    out = run(config, now=WED)
    task = conn.execute("SELECT owner_person_id FROM task_state WHERE snapshot_id = ? AND key = '1.001'", (out.snapshot_id,)).fetchone()
    assert task[0] == pid


def test_a_failed_run_keeps_the_current_snapshot(config, conn, linked):
    first = run(config, now=WED)
    conn.execute("UPDATE source SET git_url = '/nonexistent/repo'")  # test only: break the source
    import shutil
    shutil.rmtree(config.data_dir / "mirrors")
    second = run(config, now=WED)
    assert second.result == "FAILED"
    assert conn.execute("SELECT snapshot_id FROM current_snapshot").fetchone()[0] == first.snapshot_id  # PR-12


def test_one_broken_source_is_partial_and_carries_rows_over(config, conn, linked, tmp_path):
    first = run(config, now=WED)
    other = make_aidlc_repo(tmp_path / "second")
    sid2 = hand.add_source(conn, linked["pid"], str(other), role="CODE")
    run(config, now=WED)
    conn.execute("UPDATE source SET git_url = '/nonexistent/repo' WHERE id = ?", (linked["sid"],))
    import shutil
    shutil.rmtree(config.data_dir / "mirrors" / f"{linked['sid']}.git")
    third = run(config, now=WED)
    assert third.result == "PARTIAL"
    carried = conn.execute("SELECT count(*), min(carried_over) FROM task_state WHERE snapshot_id = ?", (third.snapshot_id,)).fetchone()
    assert carried[0] == 4 and carried[1] == 1
    del first, sid2


def test_scheduled_run_skips_weekends_and_runs_once_a_day(config, conn, linked):
    assert run(config, trigger="SCHEDULE", now=SAT).result == "SKIPPED"  # PR-10
    assert conn.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 0
    assert run(config, trigger="SCHEDULE", now=WED).result == "OK"
    assert run(config, trigger="SCHEDULE", now=WED).result == "SKIPPED"  # R-R3
    assert run(config, trigger="HAND", now=WED).result == "OK"


def test_only_one_run_at_a_time(config, conn, linked):
    with exclusive(config.data_dir / "run.lock", "test"):
        out = run(config, now=WED)
    assert out.result == "REFUSED" and "in progress" in out.message


def test_github_answers_become_prs_reviews_and_blockers(config, conn, linked, client):
    conn.execute("UPDATE source SET github_repo = 'acme/widgets' WHERE id = ?", (linked["sid"],))  # test only
    secrets = config.data_dir / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    (secrets / "github-token").write_text("test-token")
    pulls = saved_pulls()
    ready_pr = {**pulls[1]["pr"], "number": 14, "title": "Pilot notes", "head": {"ref": "pilot", "sha": "listed-sha"},
                "html_url": "https://github.com/acme/widgets/pull/14"}
    pulls.append({"pr": ready_pr, "reviews": [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": "current-sha"}]})
    by_number = {p["pr"]["number"]: p for p in pulls}
    head_sha = "current-sha"
    deny_checks = False

    def handler(request: httpx.Request):
        path, q = request.url.path, dict(request.url.params)
        assert request.method == "GET"
        if path.endswith("/pulls"):
            want = q.get("state")
            return httpx.Response(200, json=[p["pr"] for p in pulls if (p["pr"]["state"] == want)] if q.get("page") == "1" else [])
        if path.split("/")[-2] == "pulls":
            number = int(path.split("/")[-1])
            pr = by_number[number]["pr"]
            head = {"ref": "pilot", "sha": head_sha} if number == 14 else pr["head"]
            return httpx.Response(200, json={**pr, "head": head, "mergeable": True, "mergeable_state": "clean"})
        if "/reviews" in path:
            n = int(path.split("/")[-2])
            return httpx.Response(200, json=by_number[n]["reviews"] if q.get("page") == "1" else [])
        if "/timeline" in path:
            n = int(path.split("/")[-2])
            events = [{"event": "review_requested", "requested_reviewer": {"login": r["login"]}, "created_at": r["at"]} for r in by_number[n]["requests"]]
            return httpx.Response(200, json=events if q.get("page") == "1" else [])
        if path.endswith("/check-runs"):
            sha = path.split("/")[-2]
            if deny_checks and sha == "current-sha":
                return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
            assert sha != "listed-sha"
            return httpx.Response(200, json={"check_runs": [{"status": "completed", "conclusion": "failure" if sha == "aaa" else "success"}]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    factory = lambda token, log: ReadOnlyGitHub(token, log=log, transport=httpx.MockTransport(handler))  # noqa: E731
    out = run(config, now=WED, github_factory=factory)
    assert out.result == "OK"
    prs = {p["number"]: p for p in rows(conn, "pr_state", out.snapshot_id)}
    assert prs[12]["checks_state"] == "FAILING" and prs[13]["review_state"] == "CHANGES_REQUESTED"
    kinds = {(b["kind"], b["subject_key"]): b for b in rows(conn, "blocker", out.snapshot_id)}
    assert kinds[("REVIEW_WAITING", "12")]["age_days"] == 5 and kinds[("REVIEW_WAITING", "12")]["person_raw"] == "bayu-dev"
    assert ("CHECK_FAILING", "12") in kinds and ("CHANGES_REQUESTED", "13") in kinds
    methods = {r[0] for r in conn.execute("SELECT method FROM request_log WHERE run_id = ?", (out.run_id,))}
    assert methods == {"GET"}  # acceptance point 4
    assert "test-token" not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM request_log")])
    assert prs[14]["approved_head"] and prs[14]["head_sha"] == "current-sha"
    for page in ("/", "/pulls"):
        html = client.get(page).text
        assert "Ready to merge · 1" in html and "Needs attention · 2" in html
        assert "Fix the failing checks." in html and 'href="https://github.com/acme/widgets/pull/14"' in html
    assert "runbook 1.003" not in client.get("/pulls").text

    assert run(config, now=WED, trigger="SCHEDULE", github_factory=factory).result == "OK"
    digest = client.get("/changes").text
    assert "Ready to merge · acme/widgets#14 · Pilot notes" in digest
    assert "Needs attention · acme/widgets#12" in digest
    (secrets / "github-token").unlink()
    assert run(config, now=WED, github_factory=factory).result == "PARTIAL"
    queue = client.get("/pulls").text
    assert "Unverified · 3" in queue and "Ready to merge" not in queue and "Awaiting GitHub data" in queue
    (secrets / "github-token").write_text("test-token")
    deny_checks = True
    outcome = run(config, now=WED, github_factory=factory)
    assert outcome.result == "PARTIAL"
    queue = client.get("/pulls").text
    assert "GitHub denied access to CI results." in queue and "Ready to merge" not in queue
    assert "Unverified · 1" in queue and "Needs attention · 2" in queue
    deny_checks = False
    head_sha = "newer-commit"
    assert run(config, now=WED, github_factory=factory).result == "OK"
    queue = client.get("/pulls").text
    assert "Needs attention · 3" in queue and "Get approval for the latest commit." in queue


def test_new_commits_are_read_on_the_next_run(config, conn, linked):
    run(config, now=WED)
    commit(linked["repo"], {"plan/wbs.csv": "WBS No,Task,Milestone,Owner,Start,Finish,Status,Depends on\n1.001,Sign-in flow,G1,Rani,2026-11-10,2026-11-20,Done,\n"})
    out = run(config, now=WED)
    tasks = rows(conn, "task_state", out.snapshot_id)
    assert [(t["key"], t["status"]) for t in tasks] == [("1.001", "DONE")]
    assert not [b for b in rows(conn, "blocker", out.snapshot_id) if b["kind"] == "TASK_LATE"]


def _imports(package: Path) -> set[str]:
    names = set()
    for f in package.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                level = "." * node.level
                names.add(level + base)
                names.update(f"{level}{base}.{a.name}".replace("..", ".") for a in node.names)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
    return names


def test_import_rule():
    """PR-2: run and onboard never import data.hand; web never imports data.run."""
    root = Path(__file__).resolve().parents[1] / "pmd"
    for pkg in ("run", "onboard", "adapters", "sources", "rules", "model"):
        found = _imports(root / pkg)
        assert not any(n.endswith("data.hand") or n.endswith("data import hand") or n == "..data.hand" for n in found), (pkg, found)
        assert not any(n.endswith(".hand") and "data" in n for n in found), (pkg, found)
    web = _imports(root / "web")
    assert not any(n.endswith("data.run") or n.endswith(".data.run") for n in web), web
