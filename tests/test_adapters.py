"""Adapters (Part 2 §8, §9). They read files and saved answers; no network, no database."""

import json
from pathlib import Path

from pmd.adapters import aidlc, github, wbs

from .fixtures import aidlc_files, saved_pulls, write


def checkout(tmp_path) -> Path:
    root = tmp_path / "checkout"
    write(root, aidlc_files())
    return root


def facts(result, kind):
    return [r for r in result.rows.get("fact", []) if r["kind"] == kind]


def test_aidlc_reads_shapes_not_meaning(tmp_path):
    root = checkout(tmp_path)
    r = aidlc.extract(root, source_id=1, commit="abc", project_id=1, snapshot_id=1)
    stages = facts(r, "stage.status")
    assert [json.loads(f["value"])["checked"] for f in stages] == [True, True, False]
    status = {json.loads(f["value"])["key"]: json.loads(f["value"])["value"] for f in facts(r, "project.status_block")}
    assert status["Lifecycle Phase"] == "CONSTRUCTION"
    notes = facts(r, "note")
    assert [n["fact_date"] for n in notes] == ["2026-11-20", "2026-11-18", "2026-11-23"]  # out of order is fine
    assert all(n["src_path"] == aidlc.STATE_FILE and n["src_line_from"] for n in notes)
    present = {(s["unit"], s["stage"]) for s in r.rows["unit_stage_state"]}
    assert ("alpha", "code-generation") in present and ("beta", "functional-design") in present
    plan = json.loads(facts(r, "plan.steps")[0]["value"])
    assert plan == {"done": 2, "total": 3}
    assert any("scratch-notes" in p.path and "not known" in p.reason for p in r.problems)
    assert {f["subject_key"] for f in facts(r, "unit.exists")} == {"alpha", "beta"}


def test_dashboard_file(tmp_path):
    root = checkout(tmp_path)
    path = "aidlc-docs/construction/dashboard/dashboard-state.json"
    r, updated = aidlc.extract_dashboard(root, path, source_id=1, commit="abc", project_id=1, snapshot_id=1)
    assert updated == "2026-11-10T07:00:00Z"
    units = {u["unit"]: u for u in r.rows["unit_state"]}
    assert units["alpha"]["blocked"] == "" and units["beta"]["blocked"] == "waits for the contract review"
    approved = {(s["unit"], s["stage"]) for s in r.rows["unit_stage_state"] if s["state"] == "APPROVED"}
    assert approved == {("alpha", "functional-design"), ("alpha", "nfr-requirements")}
    assert units["alpha"]["src_line_from"]
    r2, _ = aidlc.extract_dashboard(root, "nope.json", source_id=1, commit="abc", project_id=1, snapshot_id=1)
    assert r2.problems and not r2.rows


def test_stage_of_text():
    assert aidlc.stage_of_text("In Progress: Code Generation").key == "code-generation"
    assert aidlc.stage_of_text("NFR Design").key == "nfr-design"
    assert aidlc.stage_of_text("Pending") is None


def test_wbs_mapping_guess_and_read(tmp_path):
    root = checkout(tmp_path)
    text = (root / "plan/wbs.csv").read_text()
    header, rows = wbs.read_rows(text)
    mapping = wbs.guess_mapping(header, rows)
    assert mapping["columns"]["key"] == "WBS No" and mapping["columns"]["gate"] == "Milestone"
    assert mapping["status"]["In progress"] == "IN_PROGRESS" and "Someday" not in mapping["status"]
    r = wbs.extract(root, "plan/wbs.csv", json.dumps(mapping), source_id=1, commit="abc", project_id=1, snapshot_id=1, gate_keys={"G1"})
    tasks = {t["key"]: t for t in r.rows["task_state"]}
    assert tasks["1.001"]["status"] == "IN_PROGRESS" and tasks["1.001"]["finish"] == "2026-11-20"
    assert tasks["1.002"]["depends_on"] == "1.001"
    assert tasks["2.001"]["gate_key"] == "" and tasks["2.001"]["status"] == ""
    reasons = " | ".join(p.reason for p in r.problems)
    assert "unknown gate key G9" in reasons and "Someday" in reasons
    assert tasks["1.003"]["src_line_from"] == 4
    assert wbs.guess_mapping(["Name", "Email"], [["a", "b"]]) is None
    bad = wbs.extract(root, "plan/wbs.csv", json.dumps({"columns": {"key": "Nope"}}), source_id=1, commit="", project_id=1, snapshot_id=1, gate_keys=set())
    assert bad.problems and "header lacks" in bad.problems[0].reason


def test_github_adapter(tmp_path):
    folder = tmp_path / "gh"
    folder.mkdir()
    (folder / "pulls.json").write_text(json.dumps(saved_pulls()))
    (folder / "issues.json").write_text("[]")
    r = github.extract(folder, source_id=2, project_id=1, snapshot_id=1, branch_pattern="unit/<unit>; <unit>/<topic>", task_key_pattern=r"\b\d\.\d{3}\b")
    prs = {p["number"]: p for p in r.rows["pr_state"]}
    assert prs[12]["unit"] == "alpha" and prs[12]["task_keys"] == "1.001" and prs[12]["review_state"] == "PENDING"
    assert prs[12]["checks_state"] == "FAILING"
    assert prs[13]["unit"] == "beta" and prs[13]["review_state"] == "CHANGES_REQUESTED"
    assert prs[9]["state"] == "MERGED" and prs[9]["checks_state"] == "NONE"
    pending = [x for x in r.rows["review_state"] if x["state"] == "PENDING"]
    assert pending[0]["reviewer_raw"] == "bayu-dev" and pending[0]["requested_at"] == "2026-11-18T02:00:00Z"
    assert all(p["src_url"].startswith("https://github.com/") for p in r.rows["pr_state"])
