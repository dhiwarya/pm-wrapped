"""Phase 3 through the runner and dashboard, with real git and fake model CLIs."""

from dataclasses import replace
from datetime import timedelta
import json
import os

from pmd.data import hand, views
from pmd.model import judge, reader
from pmd.model.cli import CliResult
from pmd.model.provider import call as invoke_provider
from pmd.run.controller import run

from .conftest import post
from .fixtures import STATE, commit, make_aidlc_repo
from .test_run import WED


def make_project(conn, directory, key="WIDGET"):
    repo = make_aidlc_repo(directory / key)
    project = hand.create_project(conn, key, key.title())
    source = hand.add_source(conn, project, str(repo), key="docs")
    hand.set_reading(conn, project, dashboard_source=str(source),
                     dashboard_path="aidlc-docs/construction/dashboard/dashboard-state.json")
    gate = hand.create_gate(conn, project, "G1", "Pilot", "2026-12-18")
    item = hand.create_item(conn, gate, "Pilot list agreed")
    hand.set_item_state(conn, item, "MET")
    return repo, project, gate


def model_answer(prompt, schema, **kwargs):
    if schema == reader.SCHEMA:
        sample = "alpha waits for the base image review."
        payload = json.loads(prompt.split("Report unread inputs by ID.\n", 1)[1])
        source = next((item for item in payload["inputs"] if sample in item["text"]), None)
        facts = []
        if source:
            facts.append({"input_id": source["input_id"], "kind": "blocker.stated", "subject_type": "project",
                          "subject_key": payload["known"]["project"][0], "fact_date": "", "text": "Base image needs review",
                          "people": ["Dhiwa"], "quote": sample})
        return CliResult({"facts": facts, "not_read": []}, provider="codex", seconds=1.25)
    assert schema == judge.SCHEMA
    gates = json.loads(prompt.split("\n", 1)[1])
    return CliResult({"proposals": [{"gate_id": g["gate_id"], "value": "AT_RISK",
                                     "reasons": [{"text": "A review is pending", "facts": [g["facts"][0]["id"]]},
                                                 {"text": "Unverified claim", "facts": [900000]}]} for g in gates]}, provider="codex")


def test_scheduled_run_story_and_confirmation(config, conn, client, tmp_path):
    _, pid, gid = make_project(conn, tmp_path)
    outcome = run(config, now=WED, trigger="SCHEDULE", model_call=model_answer)
    assert outcome.result == "OK"
    proposal = views.health_proposals(conn, outcome.snapshot_id)[gid]
    assert (proposal["value"], proposal["floor"], proposal["produced_by"]) == ("AT_RISK", "ON_TRACK", "MODEL")
    reasons = json.loads(proposal["reasons"])
    assert len(reasons) == 1
    fact = views.fact(conn, reasons[0]["facts"][0])
    assert fact["read_by"] == "MODEL" and fact["src_line_from"] == fact["src_line_to"]
    assert views.snapshot_rows(conn, "blocker", outcome.snapshot_id, pid)[-1]["kind"] == "STATED"
    assert "alpha waits for the base image review." in client.get(f"/facts/{fact['id']}").text
    today = client.get("/")
    assert "Proposed by PM Dash" in today.text and "A review is pending" in today.text
    assert "Codex fallback" in client.get("/runs").text
    digest = views.digest(conn, "2026-11-25")
    cards = json.loads(digest["cards"])
    assert all(line in digest["text"] for card in cards for line in card["lines"])
    assert (config.data_dir / "digests" / "2026-11-25.txt").read_text() == digest["text"]
    assert "Read the full digest" in client.get("/story/2026-11-25").text
    assert "Base image" in client.get("/changes/2026-11-25").text
    response = post(client, f"/p/WIDGET/gates/{gid}/health", {"proposal_id": proposal["id"], "confirm": "1"})
    assert response.status_code == 200
    saved = hand.latest_confirmations(conn, [gid])[gid]
    assert saved["value"] == "AT_RISK" and saved["answered_proposal_id"] == proposal["id"]
    assert "Currently confirmed at risk." in client.get("/story/2026-11-25").text
    assert ">Confirm</button>" not in client.get("/story/2026-11-25").text
    assert "Nothing needs your call" in client.get("/").text


def test_scoped_snapshot_and_daily_diff_keep_other_projects_and_early_changes(config, conn, tmp_path):
    repo, _, gid = make_project(conn, tmp_path, "ALPHA")
    _, other, other_gate = make_project(conn, tmp_path, "BETA")
    first = run(config, now=WED, trigger="SCHEDULE", model_call=model_answer)
    assert first.result == "OK"
    assert len(views.snapshot_rows(conn, "unit_state", first.snapshot_id, other)) == 2
    note = "A new checkpoint was recorded."
    commit(repo, {"aidlc-docs/aidlc-state.md": STATE + f"- 2026-11-26: {note}\n"})
    hand_run = run(config, now=WED + timedelta(days=1, hours=-1), project_key="ALPHA", model_call=model_answer)
    assert hand_run.result == "OK"
    assert len(views.digest_days(conn)) == 1
    kept = views.snapshot_rows(conn, "unit_state", hand_run.snapshot_id, other)
    assert len(kept) == 2 and all(row["carried_over"] for row in kept)
    for gate_id in (gid, other_gate):
        proposal = views.health_proposals(conn, hand_run.snapshot_id)[gate_id]
        assert all(views.fact(conn, fid)["snapshot_id"] == hand_run.snapshot_id
                   for reason in json.loads(proposal["reasons"]) for fid in reason["facts"])
    scheduled = run(config, now=WED + timedelta(days=1), trigger="SCHEDULE", model_call=model_answer)
    assert scheduled.result == "OK"
    assert note in views.digest(conn, "2026-11-26")["text"]
    assert note in views.digest(conn, "2026-11-26")["changes"]


def test_model_failure_is_partial_and_rules_only(config, conn, tmp_path):
    _, _, gid = make_project(conn, tmp_path)
    def limited(*args, **kwargs):
        return CliResult(None, error="Codex unavailable", provider="codex")
    outcome = run(config, now=WED, model_call=limited)
    assert outcome.result == "PARTIAL"
    assert views.health_proposals(conn, outcome.snapshot_id)[gid]["produced_by"] == "RULES"
    assert views.model_reads(conn, outcome.run_id)[0]["error"] == "Codex unavailable"
    assert views.current_snapshot(conn)["id"] == outcome.snapshot_id


def test_manual_gate_uses_date_floor_without_invoking_model(config, conn):
    project = hand.create_project(conn, "MANUAL", "Manual")
    gate = hand.create_gate(conn, project, "G1", "Past gate", "2026-11-23")
    def unexpected(*args, **kwargs):
        raise AssertionError("manual projects do not send content to a model")
    outcome = run(config, now=WED, model_call=unexpected)
    proposal = views.health_proposals(conn, outcome.snapshot_id)[gate]
    assert proposal["value"] == "LATE" and proposal["floor_rule"] == "R-J1"


def test_reader_rejects_invented_quote_and_judge_cannot_improve_floor():
    known = {"project": ["WIDGET"]}
    answer = {"facts": [{"input_id": 0, "kind": "decision", "subject_type": "project", "subject_key": "WIDGET",
                        "quote": "This was not in the source", "text": "Invented", "people": [], "fact_date": ""}], "not_read": []}
    kept, dropped, error = reader.checked(answer, [reader.Passage("A different thing is in the source", "source", {})], known)
    assert (kept, dropped) == ([], 1) and error
    proposal = judge.proposal({"id": 1}, "AT_RISK", "R-J2", [3])
    assert judge.checked({"proposals": [{"gate_id": 1, "value": "ON_TRACK", "reasons": [{"text": "Optimistic", "facts": [3]}]}]},
                         [proposal], {1: {3}})
    assert proposal["value"] == "AT_RISK" and proposal["produced_by"] == "RULES"


def test_codex_cli_is_invoked_only_for_a_claude_limit(config, tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.write_text('#!/bin/sh\nprintf \'%s\\n\' "{\\"subtype\\":\\"error_during_execution\\",\\"is_error\\":true,\\"result\\":\\"You\'ve hit your limit\\"}"\n')
    codex.write_text('''#!/usr/bin/env python3
import json
from pathlib import Path
import sys
args = sys.argv
schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
assert schema["additionalProperties"] is False
assert args[args.index("--sandbox") + 1] == "read-only"
assert "--ignore-user-config" in args and "shell_tool" in args and "unified_exec" in args and "--ephemeral" in args
assert sys.stdin.read() == "bounded test input"
Path(args[args.index("--output-last-message") + 1]).write_text('{"facts": [], "not_read": []}')
''')
    for binary in (claude, codex):
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    answer = invoke_provider("bounded test input", reader.SCHEMA, config=config, cwd=tmp_path, timeout=10, budget_usd=0.1)
    assert answer.provider == "codex" and answer.data == {"facts": [], "not_read": []}
    opted_out = invoke_provider("bounded test input", reader.SCHEMA, config=replace(config, codex_fallback=False),
                               cwd=tmp_path, timeout=10, budget_usd=0.1)
    assert opted_out.provider == "claude" and opted_out.limited
    claude.write_text("#!/bin/sh\necho 'authentication required' >&2\nexit 1\n")
    primary_failure = invoke_provider("bounded test input", reader.SCHEMA, config=config, cwd=tmp_path, timeout=10, budget_usd=0.1)
    assert primary_failure.provider == "claude" and not primary_failure.limited
