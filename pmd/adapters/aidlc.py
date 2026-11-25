"""The ``aidlc`` and ``aidlc-dashboard`` adapters (Part 2 §8-3, §8-4).

They read the shape of the files, not their meaning. Whether a stage is approved
is stated in prose (read by the model in Phase 3) or by the dashboard file.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import Result, file_ref

STATE_FILE = "aidlc-docs/aidlc-state.md"
CONSTRUCTION = "aidlc-docs/construction"
PLANS = "aidlc-docs/construction/plans"

CHECKBOX = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*\S)\s*$")
STATUS_LINE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*\s*:\s*(.*\S)\s*$")
NOTE_LINE = re.compile(r"^\s*[-*]\s+\*{0,2}(\d{4}-\d{2}-\d{2})\b(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    folder: str
    names: tuple[str, ...]


def load_stages() -> list[Stage]:
    raw = tomllib.loads(resources.files("pmd.adapters").joinpath("stages.toml").read_text(encoding="utf-8"))
    return [Stage(s["key"], s["label"], s["folder"], tuple(n.lower() for n in s["names"])) for s in raw["stage"]]


STAGES = load_stages()


def probe(root: Path) -> bool:
    return (root / STATE_FILE).is_file()


def stage_of_text(text: str) -> Stage | None:
    """The stage named in free text such as "In Progress: Code Generation". Longest name wins."""
    lowered = text.lower()
    best: tuple[int, Stage] | None = None
    for stage in STAGES:
        for name in stage.names:
            if re.search(rf"\b{re.escape(name)}\b", lowered) and (best is None or len(name) > best[0]):
                best = (len(name), stage)
    return best[1] if best else None


def extract(root: Path, *, source_id: int, commit: str, project_id: int, snapshot_id: int) -> Result:
    result = Result()
    base = {"snapshot_id": snapshot_id, "project_id": project_id}
    state = root / STATE_FILE
    if not state.is_file():
        return result
    lines = state.read_text(encoding="utf-8", errors="replace").splitlines()
    heading = ""
    found_stage = found_status = False
    for number, line in enumerate(lines, start=1):
        h = HEADING.match(line)
        if h:
            heading = h.group(2)
            continue
        box = CHECKBOX.match(line)
        if box:
            found_stage = True
            result.add("fact", {**base, "subject_type": "stage", "subject_key": box.group(2)[:120], "kind": "stage.status",
                                "value": json.dumps({"phase": heading, "checked": box.group(1).lower() == "x", "text": box.group(2)}),
                                **file_ref(source_id, commit, STATE_FILE, number)})
            continue
        status = STATUS_LINE.match(line)
        if status and "status" in heading.lower():
            found_status = True
            result.add("fact", {**base, "subject_type": "project", "subject_key": status.group(1)[:80], "kind": "project.status_block",
                                "value": json.dumps({"key": status.group(1), "value": status.group(2)}),
                                **file_ref(source_id, commit, STATE_FILE, number)})
            continue
        note = NOTE_LINE.match(line)
        if note:
            text = line.strip().lstrip("-* ").strip()
            result.add("fact", {**base, "subject_type": "project", "subject_key": "", "kind": "note", "fact_date": note.group(1),
                                "value": json.dumps({"text": text[:4000], "hash": hashlib.sha256(text.encode()).hexdigest()[:16]}),
                                **file_ref(source_id, commit, STATE_FILE, number)})
    if not found_stage:
        result.problem("aidlc", STATE_FILE, "no stage checkboxes found")
    if not found_status:
        result.problem("aidlc", STATE_FILE, "no status block found under a heading named Status")

    construction = root / CONSTRUCTION
    known_folders = {s.folder: s for s in STAGES}
    units = sorted(p for p in construction.iterdir() if (p / "unit-context.md").is_file()) if construction.is_dir() else []
    for unit_dir in units:
        unit = unit_dir.name
        result.add("fact", {**base, "subject_type": "unit", "subject_key": unit, "kind": "unit.exists", "value": "{}",
                            **file_ref(source_id, commit, f"{CONSTRUCTION}/{unit}/unit-context.md")})
        for child in sorted(unit_dir.iterdir()):
            if not child.is_dir():
                continue
            stage = known_folders.get(child.name)
            if stage is None:
                result.problem("aidlc", f"{CONSTRUCTION}/{unit}/{child.name}", "stage folder name not known")
                continue
            if any(child.rglob("*")):
                result.add("unit_stage_state", {**base, "unit": unit, "stage": stage.key, "state": "ARTIFACT_PRESENT",
                                                "read_by": "PROGRAM", **file_ref(source_id, commit, f"{CONSTRUCTION}/{unit}/{child.name}")})
    plans = root / PLANS
    if plans.is_dir():
        for plan in sorted(plans.glob("*-plan.md")):
            text = plan.read_text(encoding="utf-8", errors="replace").splitlines()
            boxes = [CHECKBOX.match(t) for t in text]
            boxes = [b for b in boxes if b]
            rel = f"{PLANS}/{plan.name}"
            if not boxes:
                result.problem("aidlc", rel, "plan file has no checkboxes")
                continue
            done = sum(1 for b in boxes if b.group(1).lower() == "x")
            result.add("fact", {**base, "subject_type": "plan", "subject_key": plan.stem, "kind": "plan.steps",
                                "value": json.dumps({"done": done, "total": len(boxes)}), **file_ref(source_id, commit, rel)})
    return result


def extract_dashboard(root: Path, path: str, *, source_id: int, commit: str, project_id: int, snapshot_id: int) -> tuple[Result, str]:
    """Returns rows and the file's own ``updated`` value."""
    result = Result()
    target = root / path
    if not target.is_file():
        result.problem("aidlc-dashboard", path, "file not found at the read commit")
        return result, ""
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.problem("aidlc-dashboard", path, f"not valid JSON: {exc.__class__.__name__}")
        return result, ""
    text_lines = target.read_text(encoding="utf-8").splitlines()

    def line_of(needle: str) -> int | None:
        for i, t in enumerate(text_lines, start=1):
            if needle in t:
                return i
        return None

    base = {"snapshot_id": snapshot_id, "project_id": project_id}
    updated = str(data.get("updated", "")) if isinstance(data, dict) else ""
    units = data.get("units", []) if isinstance(data, dict) else []
    for row in units:
        if not isinstance(row, dict) or not row.get("unit"):
            result.problem("aidlc-dashboard", path, "a unit row has no unit name")
            continue
        unit = str(row["unit"])
        line = line_of(f'"unit": "{unit}"') or line_of(f'"unit":"{unit}"')
        for key in ("lead", "status", "stage"):
            if key not in row:
                result.problem("aidlc-dashboard", path, f"unit {unit} has no {key}", line)
        blocked = str(row.get("blocked", "") or "")
        if blocked.strip().lower() in ("none", "no", "false", "-", "n/a"):
            blocked = ""
        result.add("unit_state", {**base, "unit": unit, "lead_raw": str(row.get("lead", ""))[:200], "branch": str(row.get("branch", ""))[:200],
                                  "stage": str(row.get("stage", ""))[:500], "status": str(row.get("status", ""))[:200], "blocked": blocked[:500],
                                  **file_ref(source_id, commit, path, line)})
        # Stages before the current one count as approved by this file (Part 2 §8-3).
        current = stage_of_text(str(row.get("status", ""))) or stage_of_text(str(row.get("stage", "")))
        merged = str(row.get("status", "")).lower().startswith("merged")
        if current or merged:
            keys = [s.key for s in STAGES]
            upto = len(keys) if merged else keys.index(current.key)
            for stage in STAGES[:upto]:
                result.add("unit_stage_state", {**base, "unit": unit, "stage": stage.key, "state": "APPROVED", "read_by": "PROGRAM",
                                                **file_ref(source_id, commit, path, line)})
    return result, updated
