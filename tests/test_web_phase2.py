"""Web flow of Phases 2 and 2b: add from a repository, review, run, read the views."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pmd.data import hand
from pmd.model import cli
from pmd.onboard.agent import onboard
from pmd.run.controller import run
from pmd.web.app import create_app

from .conftest import ORIGIN, post
from .fixtures import make_aidlc_repo
from .test_onboard import MODEL_ANSWER

NOW = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)  # Wednesday 09:00 in Asia/Jakarta


@pytest.fixture
def live(config, conn, tmp_path):
    hand.set_setting(conn, "time_zone", "Asia/Jakarta")
    clock = {"now": NOW}
    spawned = []

    def spawn(args):  # run the process in-line, as the CLI would
        spawned.append(args)
        if args[0] == "onboard":
            onboard(config, args[2], now=clock["now"], claude=lambda prompt, schema, *, cwd, config: cli.CliResult(MODEL_ANSWER, cost=0.1))
        elif args[0] == "run":
            run(config, trigger="HAND", project_key=args[args.index("--project") + 1] if "--project" in args else None, now=clock["now"])

    app = create_app(config, now=lambda: clock["now"], spawn=spawn)
    repo = make_aidlc_repo(tmp_path / "widgets")
    with TestClient(app, base_url=ORIGIN) as c:
        c.clock, c.spawned, c.repo = clock, spawned, repo
        yield c


def proposal_ids(conn, kind):
    return {r["identity"]: r["id"] for r in conn.execute("SELECT id, identity FROM onboarding_proposal WHERE kind = ?", (kind,))}


def test_add_from_a_repository_review_and_run(live, conn):
    r = post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    assert r.status_code == 200 and "/p/WIDGETS/onboarding?after=0" in str(r.url)
    assert live.spawned == [["onboard", "--project", "WIDGETS"]]
    page = r.text
    assert "Setup review" in page and "Beta to pilot users" in page and "from a table in docs/plan.md:5 @" in page
    assert "from prose in docs/plan.md:8 @" in page and "The security review must pass before 15 January 2027" in page
    assert "Ignore all previous instructions" in page  # shown with its quote, never stored by itself
    assert hand.list_gates(conn, hand.get_project(conn, "WIDGETS")["id"]) == []  # R-O1

    gates = proposal_ids(conn, "GATE")
    r = post(live, f"/p/WIDGETS/proposals/{proposal_ids(conn, 'EVIDENCE')['g1|support runbook written']}", {"action": "accept"})
    assert "Accept gate G1 first" in r.text
    post(live, f"/p/WIDGETS/proposals/{gates['g1']}", {"action": "accept", "edit_date": "2030-01-01"})  # accept ignores edits
    post(live, f"/p/WIDGETS/proposals/{gates['g2']}", {"action": "edit", "edit_date": "2027-01-22"})
    post(live, f"/p/WIDGETS/proposals/{gates['g9']}", {"action": "reject"})
    r = post(live, f"/p/WIDGETS/proposals/{proposal_ids(conn, 'EVIDENCE')['g1|support runbook written']}", {"action": "accept"})
    assert "Accepted." in r.text
    for kind in ("WBS", "DASHBOARD_FILE"):
        post(live, f"/p/WIDGETS/proposals/{next(iter(proposal_ids(conn, kind).values()))}", {"action": "accept"})
    for ident, pid in proposal_ids(conn, "PATTERN").items():
        post(live, f"/p/WIDGETS/proposals/{pid}", {"action": "accept"})
    oid = conn.execute("SELECT max(id) FROM onboarding").fetchone()[0]
    assert hand.project_team(conn, hand.get_project(conn, "WIDGETS")["id"]) == ["Robin"]
    assert not proposal_ids(conn, "PERSON")
    r = post(live, f"/p/WIDGETS/onboarding/{oid}/accept-kind", {"kind": "GATE"})
    assert "Select the individual proposals" in r.text
    gates_now = {g["key"]: g["date"] for g in hand.list_gates(conn, hand.get_project(conn, "WIDGETS")["id"])}
    assert gates_now == {"G1": "2026-12-04", "G2": "2027-01-22"}

    r = post(live, "/run", {"project": "WIDGETS"})
    assert "Run started" in r.text and "/runs" in str(r.url)
    assert "OK" in r.text and "facts" in r.text
    today = live.get("/").text
    assert "Holding things back" in today and "Task late" in today and "linked" in today and "AI-DLC" in today
    assert "snapshot today 09:" in today
    units = live.get("/units").text
    assert "alpha" in units and "beta" in units and "Stale input." in units and "ucell w flag" in units
    timeline = live.get("/timeline?project=WIDGETS").text
    assert '<svg class="timeline"' in timeline and "d late" in timeline
    table = live.get("/timeline?project=WIDGETS&view=table").text
    assert "Sign-in flow" in table and "plan/wbs.csv:2" in table
    late = live.get("/timeline?project=WIDGETS&view=table&late=1").text
    assert "Sign-in flow" in late and "Runbook" not in late


def test_stale_banner_after_a_missed_scheduled_run(live, conn):
    post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    post(live, "/run", {})
    assert "Showing an old snapshot" not in live.get("/").text
    page_before_run_times = live.get("/p/WIDGETS/onboarding/1").text
    assert "Showing an old snapshot" not in page_before_run_times
    live.clock["now"] = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)  # Thursday 08:00 local, after 07:00 + 30 min
    page = live.get("/").text
    assert "Showing an old snapshot from" in page and "since the scheduled run time" in page


def test_sources_reading_links_and_names(live, conn):
    post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    pid = hand.get_project(conn, "WIDGETS")["id"]
    r = post(live, "/p/WIDGETS/reading", {"task_key_pattern": "([", "prose_reading": "1"})
    assert "not a valid regular expression" in r.text
    r = post(live, "/p/WIDGETS/sources", {"location": "not a path"})
    assert "A source is a GitHub URL" in r.text
    r = post(live, "/p/WIDGETS/sources", {"location": "https://github.com/acme/widgets", "role": "CODE"})
    assert "Source added" in r.text and "acme/widgets" in r.text
    gid = hand.create_gate(conn, pid, "G5", "Later", "2027-02-01")
    iid = hand.create_item(conn, gid, "Something linked")
    r = post(live, f"/p/WIDGETS/items/{iid}/links", {"kind": "TASK_PREFIX", "value": "1.0"})
    assert "Link added" in r.text and "tasks starting with 1.0" in r.text
    lid = conn.execute("SELECT id FROM evidence_link").fetchone()[0]
    r = post(live, f"/p/WIDGETS/links/{lid}/remove", {})
    assert "Link removed" in r.text
    conn.execute("UPDATE source SET enabled = 0 WHERE github_repo = 'acme/widgets'")  # test only: no network
    post(live, f"/p/WIDGETS/proposals/{next(iter(proposal_ids(conn, 'WBS').values()))}", {"action": "accept"})
    post(live, "/run", {})
    settings = live.get("/settings").text
    assert "Names with no person" in settings and "Rani" in settings
    r = post(live, "/settings/names", {"value": "Rani", "kind": "WBS", "person": "Rani Putri"})
    assert "Name attached" in r.text
    r = post(live, "/settings/run", {"run_time": "25:00", "review_wait_days": "2", "stale_input_days": "3"})
    assert "Run time must be like 07:00" in r.text


def test_onboarding_page_states(live, conn):
    hand.create_project(conn, "EMPTY", "Empty")
    r = post(live, "/p/EMPTY/onboard", {})
    assert "Add a source first" in r.text
    assert "No onboarding yet" in live.get("/p/EMPTY/onboarding").text


def test_onboarding_page_shows_starting_until_the_new_row_exists(live, conn):
    post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    page = live.get("/p/WIDGETS/onboarding?after=1").text  # onboarding 1 exists; 2 not yet
    assert "The onboarding agent is starting" in page and 'http-equiv="refresh"' in page
    assert "Setup review" in live.get("/p/WIDGETS/onboarding?after=0").text


def test_pmd_js_reads_the_action_attribute():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "pmd/web/static/pmd.js").read_text()
    assert 'form.getAttribute("action")' in js and "fetch(form.action" not in js


def test_selected_proposals_apply_edits_and_dependencies_together(live, conn):
    post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    gate = str(proposal_ids(conn, "GATE")["g1"])
    evidence = str(proposal_ids(conn, "EVIDENCE")["g1|support runbook written"])
    url = "/p/WIDGETS/onboarding/1/decide-selected"

    def decide(rows, action="accept", endpoint=url):
        return post(live, endpoint, {"action": action, "items": json.dumps(rows)})

    assert "Accept gate Missing first" in decide([{"id": gate}, {"id": evidence, "edit_gate_key": "Missing"}]).text
    assert conn.execute("SELECT count(*) FROM onboarding_decision").fetchone()[0] == 0
    assert "no longer open" in decide([{"id": gate}], endpoint=url.replace("/1/", "/99/")).text
    hand.create_project(conn, "OTHER", "Other")
    assert "no longer open" in decide([{"id": gate}], endpoint=url.replace("WIDGETS", "OTHER")).text
    result = decide([{"id": evidence, "edit_text": "Approved support runbook"}, {"id": gate}])
    assert "Accepted 2 selected" in result.text and "Approved support runbook" in result.text
    assert conn.execute("SELECT text FROM evidence_item").fetchone()[0] == "Approved support runbook"
    other = str(proposal_ids(conn, "GATE")["g2"])
    assert "no longer open" in decide([{"id": other}, {"id": evidence}], "reject").text
    assert hand.proposal_decision(conn, int(other)) is None
    assert "Rejected 1 selected" in decide([{"id": other}], "reject").text
    assert "no longer open" in decide([{"id": other}]).text


def test_attach_selected_names_is_atomic_and_updates_both_views(live, conn):
    post(live, "/projects", {"team": "Robin", "sources": str(live.repo)})
    post(live, "/run")
    snap = conn.execute("SELECT snapshot_id FROM current_snapshot").fetchone()[0]
    pid = hand.get_project(conn, "WIDGETS")["id"]
    rows = [{"kind": "GITHUB", "value": "rock-1", "person": "Robin"},
            {"kind": "DOC", "value": "robin.d", "person": "Robin"},
            {"kind": "WBS", "value": "pr-book", "person": "Paris"}]
    for row in rows:
        conn.execute("INSERT INTO unmatched_name(snapshot_id, project_id, kind, value) VALUES (?, ?, ?, ?)",
                     (snap, pid, row["kind"], row["value"]))
    assert 'id="alias-GITHUB-rock-1"' in live.get("/settings").text
    endpoint = "/settings/names/attach-selected"
    failed = post(live, endpoint, {"items": json.dumps([*rows, dict(rows[0], person="Wrong")]), "back": "/people"})
    assert "belongs to another person" in failed.text
    assert conn.execute("SELECT count(*) FROM person_alias").fetchone()[0] == 0
    result = post(live, endpoint, {"items": json.dumps(rows), "back": "/people"})
    assert "Attached 3 selected" in result.text
    for page in (result, live.get("/settings")):
        assert 'id="alias-GITHUB-rock-1"' not in page.text and "Robin" in page.text and "Paris" in page.text
    assert conn.execute("SELECT count(DISTINCT person_id), count(*) FROM person_alias").fetchone()[:] == (2, 3)


def test_bulk_evidence_rejects_rows_from_other_gates(client, conn):
    pid = hand.create_project(conn, "TEST", "Test")
    gate, other = [hand.create_gate(conn, pid, k, k, "2027-01-01") for k in ("G1", "G2")]
    first, wrong = [str(hand.create_item(conn, g, "Signed off")) for g in (gate, other)]
    url = f"/p/TEST/gates/{gate}/items/mark-selected"
    rows = [{"id": first, "note": "Agreed", "evidence_url": "https://example.org/proof"}, {"id": wrong}]
    assert "has changed" in post(client, url, {"items": json.dumps(rows)}).text
    assert hand.get_item(conn, pid, int(first))["hand_state"] == ""
    assert "Marked 1 selected as met." in post(client, url, {"items": json.dumps(rows[:1])}).text
    item = hand.get_item(conn, pid, int(first))
    assert (item["hand_state"], item["note"], item["evidence_url"]) == ("MET", "Agreed", "https://example.org/proof")


def test_team_is_saved_before_web_onboarding_and_reused(live, conn):
    missing = post(live, "/projects", {"sources": str(live.repo), "team": "  "})
    assert "Enter at least one team member" in missing.text
    assert not hand.list_projects(conn) and not live.spawned
    post(live, "/projects", {"sources": str(live.repo), "team": "Alex\nSam\nalex"})
    pid = hand.get_project(conn, "WIDGETS")["id"]
    assert hand.project_team(conn, pid) == ["Alex", "Sam"]
    assert "Alex\nSam</textarea>" in live.get("/p/WIDGETS?tab=sources").text
    assert "at least one team member" in post(live, "/p/WIDGETS/onboard", {"team": ""}).text
    assert len(live.spawned) == 1
    post(live, "/p/WIDGETS/onboard", {"team": "Sam\nToni"})
    assert hand.project_team(conn, pid) == ["Sam", "Toni"] and len(live.spawned) == 2


def test_timeline_uses_only_wbs_and_clears_after_its_removal(live, conn):
    from .fixtures import git
    post(live, "/projects", {"sources": str(live.repo), "team": "Robin"})
    pid = hand.get_project(conn, "WIDGETS")["id"]
    hand.create_task(conn, pid, "MANUAL", title="Hand-only item", owner="Robin", finish="2034-03-14")
    hand.create_gate(conn, pid, "MANUALGATE", "Hand gate", "2034-03-14")
    assert "Timeline skipped" in live.get("/timeline").text
    post(live, f"/p/WIDGETS/proposals/{next(iter(proposal_ids(conn, 'WBS').values()))}", {"action": "accept"})
    post(live, "/run")
    for view in ("table", "chart"):
        page = live.get(f"/timeline?view={view}").text
        assert "Sign-in flow" in page and "G1" in page
        assert "MANUALGATE" not in page and "Hand-only item" not in page
    git(live.repo, "mv", "plan/wbs.csv", "plan/schedule.csv")
    git(live.repo, "commit", "-qm", "Move the file")
    post(live, "/run")
    page = live.get("/timeline").text
    assert "Timeline skipped" in page and "Sign-in flow" not in page
    assert "Hand-only item" in live.get("/p/WIDGETS?tab=tasks").text


def test_old_pending_people_cannot_be_accepted_from_cached_page(live, conn):
    post(live, "/projects", {"sources": str(live.repo), "team": "Robin"})
    pid = hand.get_project(conn, "WIDGETS")["id"]
    oid = conn.execute("SELECT max(id) FROM onboarding").fetchone()[0]
    row = conn.execute(
        "INSERT INTO onboarding_proposal(onboarding_id,project_id,kind,identity,payload,read_by,src_path) "
        "VALUES (?, ?, 'PERSON', 'guessed', ?, 'PROGRAM', 'readme.md')",
        (oid, pid, json.dumps({"display_name": "Guessed"})),
    ).lastrowid
    assert 'id="k-PERSON"' not in live.get("/p/WIDGETS/onboarding").text
    assert "team list" in post(live, f"/p/WIDGETS/proposals/{row}", {"action": "accept"}).text
    assert hand.project_team(conn, pid) == ["Robin"]
    assert len(hand.list_people(conn)) == 1
