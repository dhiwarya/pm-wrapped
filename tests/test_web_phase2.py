"""Web flow of Phases 2 and 2b: add from a repository, review, run, read the views."""

import json
import re
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
    r = post(live, "/projects", {"sources": str(live.repo)})
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
    r = post(live, f"/p/WIDGETS/onboarding/{oid}/accept-kind", {"kind": "PERSON"})
    assert re.search(r"Accepted \d+ people", r.text)
    r = post(live, f"/p/WIDGETS/onboarding/{oid}/accept-kind", {"kind": "GATE"})
    assert "one by one" in r.text
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
    post(live, "/projects", {"sources": str(live.repo)})
    post(live, "/run", {})
    assert "Showing an old snapshot" not in live.get("/").text
    page_before_run_times = live.get("/p/WIDGETS/onboarding/1").text
    assert "Showing an old snapshot" not in page_before_run_times
    live.clock["now"] = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)  # Thursday 08:00 local, after 07:00 + 30 min
    page = live.get("/").text
    assert "Showing an old snapshot from" in page and "since the scheduled run time" in page


def test_sources_reading_links_and_names(live, conn):
    post(live, "/projects", {"sources": str(live.repo)})
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
    post(live, "/projects", {"sources": str(live.repo)})
    page = live.get("/p/WIDGETS/onboarding?after=1").text  # onboarding 1 exists; 2 not yet
    assert "The onboarding agent is starting" in page and 'http-equiv="refresh"' in page
    assert "Setup review" in live.get("/p/WIDGETS/onboarding?after=0").text


def test_pmd_js_reads_the_action_attribute():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "pmd/web/static/pmd.js").read_text()
    assert 'form.getAttribute("action")' in js and "fetch(form.action" not in js
