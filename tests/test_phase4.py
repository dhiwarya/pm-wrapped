"""People, reports, retention and recovery on invented projects."""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta

from pmd.adapters import github
from pmd.cli import main
from pmd.data import backup, db, hand, run as records, views
from pmd.run.controller import run
from pmd.web.people import build as build_people

from .conftest import post
from .fixtures import make_repo, saved_pulls
from .test_run import WED, linked


def test_people_matches_sources_and_applies_project_working_days(client, config, conn, linked, tmp_path):
    bayu = hand.add_person(conn, "Bayu")
    hand.add_alias(conn, bayu, "GITHUB", "bayu-dev")
    hand.add_holiday(conn, "2026-11-26", "Local holiday", linked["pid"])
    hand.create_task(conn, linked["pid"], "HAND-1", title="Manual launch task", owner="Bayu", status="IN_PROGRESS", finish="2026-12-03")
    hand.create_task(conn, linked["pid"], "HAND-2", title="Outside five days", owner="Bayu", finish="2026-12-04")
    hand.create_task(conn, linked["pid"], "HAND-3", title="Completed already", owner="Bayu", status="DONE", finish="2026-11-23")
    result = run(config, now=WED)
    assert result.result == "OK"
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "pulls.json").write_text(json.dumps(saved_pulls()))
    extracted = github.extract(answers, source_id=linked["sid"], project_id=linked["pid"], snapshot_id=result.snapshot_id,
                               branch_pattern="unit/<unit>", task_key_pattern="")
    for table, rows in extracted.rows.items():
        records.insert_many(conn, table, rows)
    # Another repository's PR number 12 does not determine whether the first one's review waits.
    records.insert(conn, "pr_state", {**extracted.rows["pr_state"][0], "source_id": linked["sid"] + 100,
                                    "src_source_id": linked["sid"] + 100, "state": "CLOSED"})
    records.insert(conn, "unmatched_name", {"snapshot_id": result.snapshot_id, "project_id": linked["pid"],
                                            "kind": "GITHUB", "value": "rani-p"})
    bayu_card = build_people(conn, WED)["cards"][0]
    assert bayu_card["name"] == "Bayu"
    assert [r["age"] for r in bayu_card["reviews"]] == [5]
    assert [r["title"] for r in bayu_card["prs"]] == ["beta: contract stubs"]
    assert any("Manual launch task" in t["title"] for t in bayu_card["due"])
    assert all("Outside five days" not in t["title"] for t in bayu_card["due"])
    assert all("Completed already" not in t["title"] for t in bayu_card["late"])
    response = client.get("/people")
    assert "Oldest wait" in response.text and "https://github.com/acme/widgets/pull/12" in response.text
    assert "Unmatched names" in response.text and "rani-p" in response.text
    result = post(client, "/settings/names", {"back": "/people", "kind": "GITHUB", "person": "Rani", "value": "rani-p"})
    assert result.url.path == "/people"
    assert "Unmatched names" not in result.text or 'value="rani-p"' not in result.text
    rani = next(card for card in build_people(conn, WED)["cards"] if card["name"] == "Rani")
    assert len(rani["prs"]) == 1 and len(rani["late"]) == 1


def test_gate_report_download_contains_confirmed_record_and_source_links(client, config, conn, linked):
    run_result = run(config, now=WED)
    gate = hand.list_gates(conn, linked["pid"])[0]
    hand.add_confirmation(conn, gate["id"], "ON_TRACK", "Reviewed by the owner")
    hand.set_item_state(conn, linked["items"][0], "MET", "Signed <record>", "https://example.test/pilot")
    response = client.get("/p/WIDGET/gates/G1/report.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == 'attachment; filename="WIDGET-G1-gate-report.md"'
    report = response.text
    assert "Confirmed health: On track" in report and "Reviewed by the owner" in report
    assert "**MET**" in report and "https://example.test/pilot" in report and "&lt;record&gt;" in report
    assert "## Not confirmed" not in report and "Proposed health" not in report
    assert "Task late" in report and "## Open blockers" in report
    task = views.snapshot_rows(conn, "task_state", run_result.snapshot_id, linked["pid"])[0]
    source = f"/sources/task/{task['id']}?snapshot={run_result.snapshot_id}"
    assert source in report and task["src_commit"] in report
    assert client.get(source).status_code == 200
    assert client.get(f"/sources/task/{task['id']}?snapshot=12345").status_code == 404
    with_proposal = client.get("/p/WIDGET/gates/G1/report.md?include_proposal=true").text
    assert "## Not confirmed" in with_proposal and "Proposed health: At risk" in with_proposal
    assert client.get("/p/WIDGET/gates/missing/report.md").status_code == 404


def test_retention_keeps_weekly_snapshot_digests_and_hand_history(client, config, conn):
    who = hand.create_project(conn, "MANUAL", "Invented project")
    gate = hand.create_gate(conn, who, "G1", "Pilot", WED.date().isoformat())
    hand.create_item(conn, gate, "Proof")
    hand.add_confirmation(conn, gate, "ON_TRACK")
    old = WED - timedelta(weeks=20)
    first = run(config, now=old, trigger="SCHEDULE")
    latest_same_week = run(config, now=old + timedelta(days=1), trigger="SCHEDULE")
    recent = run(config, now=WED - timedelta(days=7), trigger="SCHEDULE")
    for label in ("300", "onboard-7", "keep-me"):
        directory = config.data_dir / "runs" / label
        directory.mkdir(parents=True)
        os.utime(directory, (old.timestamp(), old.timestamp()))
    newest = run(config, now=WED)
    assert {row[0] for row in conn.execute("SELECT id FROM snapshot")} == {latest_same_week.snapshot_id, recent.snapshot_id, newest.snapshot_id}
    assert conn.execute("SELECT 1 FROM item_state WHERE snapshot_id = ?", (first.snapshot_id,)).fetchone() is None
    assert views.current_snapshot(conn)["id"] == newest.snapshot_id
    saved = views.digest(conn, old.date().isoformat())
    assert saved["snapshot_id"] is None and "Invented project" in saved["text"]
    assert client.get(f"/changes/{saved['day']}").status_code == 200
    assert client.get(f"/story/{saved['day']}").status_code == 200
    assert (config.data_dir / "digests" / f"{saved['day']}.txt").is_file()
    assert sorted(path.name for path in (config.data_dir / "runs").iterdir()) == ["keep-me"]
    assert hand.latest_confirmations(conn, [gate])[gate]["value"] == "ON_TRACK"
    assert conn.execute("SELECT count(*) FROM hand_change_log WHERE table_name = 'confirmation'").fetchone()[0] == 1


def test_backup_online_restore_and_rotation(tmp_path, config, conn):
    project = hand.create_project(conn, "DEMO", "Committed project")
    records.start_run(conn, "HAND", "", False, WED)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE project SET name = 'In progress' WHERE id = ?", (project,))
    try:
        first = backup.create(config.db_path, now=WED)
    finally:
        conn.rollback()
    recovery = tmp_path / "recovery" / "pmd.sqlite3"
    target = recovery.parent
    restore_config = tmp_path / "restore.toml"
    restore_config.write_text(f'data_dir = "{target}"\n')
    assert main(["--config", str(restore_config), "restore", str(first)]) == 0
    restored = db.open_store(recovery)
    try:
        assert restored.execute("SELECT name FROM project").fetchone()[0] == "Committed project"
        assert restored.execute("SELECT result FROM run").fetchone()[0] == "FAILED"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert main(["--config", str(restore_config), "restore", str(first)]) == 1
    stray = first.parent / "manual.sqlite3"
    stray.touch()
    for offset in range(1, 16):
        backup.create(config.db_path, now=WED + timedelta(days=offset))
    assert len(list(first.parent.glob("pmd-*.sqlite3"))) == 14
    assert stray.is_file() and not first.exists()


def test_source_removal_requires_app_request_and_cleans_its_mirror(client, config, conn, tmp_path):
    project = hand.create_project(conn, "DEMO", "Demo")
    repo = make_repo(tmp_path / "repo", {"README.md": "Invented"})
    source = hand.add_source(conn, project, str(repo))
    assert run(config, now=WED).result == "OK"
    mirror = config.data_dir / "mirrors" / f"{source}.git"
    assert mirror.is_dir()
    assert client.post(f"/p/DEMO/sources/{source}/remove").status_code == 403
    response = post(client, f"/p/DEMO/sources/{source}/remove")
    assert "Source and local mirror removed" in response.text
    assert not mirror.exists() and repo.is_dir()
    assert hand.add_source(conn, project, str(repo)) > source


def test_confirmed_project_deletion_cleans_reports_but_keeps_audit(client, config, conn, tmp_path):
    project = hand.create_project(conn, "DEMO", "Demo")
    repo = make_repo(tmp_path / "demo-project", {"README.md": "Invented"})
    source = hand.add_source(conn, project, str(repo))
    gate = hand.create_gate(conn, project, "G1", "Pilot", "2026-12-04")
    hand.create_item(conn, gate, "Proof")
    scheduled = run(config, now=WED, trigger="SCHEDULE")
    mirror = config.data_dir / "mirrors" / f"{source}.git"
    digest_file = config.data_dir / "digests" / f"{WED.date()}.txt"
    assert mirror.is_dir() and digest_file.is_file()
    assert client.get(f"/runs/{scheduled.run_id}").status_code == 200
    wrong = post(client, "/p/DEMO/delete", {"confirm_key": "WRONG"})
    assert "Type the project key" in wrong.text
    assert digest_file.exists() and mirror.exists()
    deleted = post(client, "/p/DEMO/delete", {"confirm_key": "DEMO"})
    assert deleted.url.path == "/"
    assert not digest_file.exists() and not mirror.exists()
    assert views.digest(conn) is None
    assert conn.execute("SELECT count(*) FROM item_state").fetchone()[0] == 0
    assert views.current_snapshot(conn)["id"] == scheduled.snapshot_id
    assert conn.execute("SELECT max(row_id) FROM hand_change_log WHERE table_name = 'project'").fetchone()[0] == project
    assert hand.create_project(conn, "NEW", "New project") > project
