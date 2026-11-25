"""The WBS file contract and the ``wbs`` adapter (Part 2 §9).

One CSV file per project; the project keeps its own column names and a mapping
says which column is which PMD field. ``guess_mapping`` proposes a mapping from a
header, for onboarding (Part 3 §10-9, OB-2).
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

from . import Result, file_ref

FIELDS = ("key", "title", "gate", "owner", "start", "finish", "status", "reviewer", "depends_on", "remaining", "deliverable", "evidence")
REQUIRED = ("key", "title", "gate", "owner", "start", "finish", "status")
STATUSES = ("NOT_STARTED", "IN_PROGRESS", "DONE", "BLOCKED", "DEFERRED")
DATE_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y", "D MMM YYYY": "%d %b %Y", "D MMMM YYYY": "%d %B %Y"}

SYNONYMS = {
    "key": ["wbs no", "wbs no.", "wbs", "wbs id", "id", "key", "task id", "task no", "no", "no.", "#", "code"],
    "title": ["task", "title", "name", "task name", "activity", "work item", "summary"],
    "gate": ["milestone", "gate", "step", "phase", "release"],
    "owner": ["owner", "assignee", "responsible", "pic", "accountable owner", "lead"],
    "start": ["start", "start date", "begin", "planned start"],
    "finish": ["finish", "end", "due", "due date", "end date", "finish date", "planned finish", "deadline"],
    "status": ["status", "state", "progress"],
    "reviewer": ["reviewer", "review"],
    "depends_on": ["depends on", "dependencies", "dependency", "predecessors", "depends"],
    "remaining": ["remaining", "remaining (days)", "remaining effort", "effort left", "remaining days"],
    "deliverable": ["deliverable", "output"],
    "evidence": ["evidence", "acceptance evidence"],
}
STATUS_WORDS = {
    "DONE": ["done", "complete", "completed", "closed", "finished", "merged", "accepted"],
    "IN_PROGRESS": ["in progress", "ongoing", "wip", "started", "active", "doing", "in review"],
    "NOT_STARTED": ["not started", "todo", "to do", "open", "planned", "pending", "new", ""],
    "BLOCKED": ["blocked", "on hold", "stuck", "waiting"],
    "DEFERRED": ["deferred", "postponed", "moved", "later", "cancelled", "canceled"],
}


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().replace("_", " ").split())


def read_rows(text: str) -> tuple[list[str], list[list[str]]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader]
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def guess_date_format(values: list[str]) -> str:
    for name, fmt in DATE_FORMATS.items():
        filled = [v.strip() for v in values if v.strip()]
        if filled and all(_parse(v, fmt) for v in filled[:30]):
            return name
    return "YYYY-MM-DD"


def _parse(value: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def guess_mapping(header: list[str], rows: list[list[str]]) -> dict | None:
    """A mapping when the header looks like a WBS: key, title and at least two more fields."""
    columns: dict[str, str] = {}
    lowered = {_norm(h): h for h in header if h.strip()}
    for field in FIELDS:
        for word in SYNONYMS[field]:
            if word in lowered and lowered[word] not in columns.values():
                columns[field] = lowered[word]
                break
    if "key" not in columns or "title" not in columns or len(columns) < 4:
        return None
    index = {h: i for i, h in enumerate(header)}
    dates = [r[index[columns[f]]] for f in ("start", "finish") if f in columns for r in rows if len(r) > index[columns[f]]]
    status_map: dict[str, str] = {}
    if "status" in columns:
        for r in rows:
            if len(r) > index[columns["status"]]:
                word = r[index[columns["status"]]].strip()
                for status, words in STATUS_WORDS.items():
                    if _norm(word) in words:
                        status_map[word] = status
                        break
    return {"columns": columns, "date_format": guess_date_format(dates), "status": status_map, "remaining_unit": "days"}


def extract(root: Path, path: str, mapping_json: str, *, source_id: int, commit: str, project_id: int,
            snapshot_id: int, gate_keys: set[str]) -> Result:
    result = Result()
    target = root / path
    if not target.is_file():
        result.problem("wbs", path, "WBS file not found at the read commit")
        return result
    try:
        mapping = json.loads(mapping_json) if mapping_json else {}
    except json.JSONDecodeError:
        result.problem("wbs", path, "the column mapping is not valid JSON")
        return result
    columns = mapping.get("columns", {})
    header, rows = read_rows(target.read_text(encoding="utf-8-sig", errors="replace"))
    missing = [f for f in REQUIRED if columns.get(f) not in header]
    if missing:
        result.problem("wbs", path, f"header lacks the mapped columns for: {', '.join(missing)}")
        return result
    index = {h: i for i, h in enumerate(header)}
    fmt = DATE_FORMATS.get(mapping.get("date_format", "YYYY-MM-DD"), "%Y-%m-%d")
    status_map = {k.strip().lower(): v for k, v in mapping.get("status", {}).items()}
    seen: set[str] = set()
    base = {"snapshot_id": snapshot_id, "project_id": project_id}

    def cell(row: list[str], field: str) -> str:
        col = columns.get(field)
        if col is None or col not in index or index[col] >= len(row):
            return ""
        return row[index[col]].strip()

    for number, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        key = cell(row, "key")
        if not key:
            result.problem("wbs", path, "row has no key", number)
            continue
        if key in seen:
            result.problem("wbs", path, f"duplicate key {key}, first row used", number)
            continue
        seen.add(key)
        gates = [g.strip() for g in re.split(r"[;,]", cell(row, "gate")) if g.strip()]
        unknown = [g for g in gates if g not in gate_keys]
        if unknown:
            result.problem("wbs", path, f"unknown gate key {', '.join(unknown)}, task read with no gate for it", number)
            gates = [g for g in gates if g in gate_keys]
        dates = {}
        for field in ("start", "finish"):
            raw = cell(row, field)
            parsed = _parse(raw, fmt) if raw else None
            if raw and parsed is None:
                result.problem("wbs", path, f"{field} {raw!r} is not a date in the form {mapping.get('date_format')}", number)
            dates[field] = parsed.isoformat() if parsed else ""
        raw_status = cell(row, "status")
        status = status_map.get(raw_status.lower())
        if status is None and raw_status.upper().replace(" ", "_") in STATUSES:
            status = raw_status.upper().replace(" ", "_")
        if status is None:
            result.problem("wbs", path, f"status word {raw_status!r} is not in the mapping", number)
            status = ""
        result.add("task_state", {**base, "key": key[:60], "title": cell(row, "title")[:300], "owner_raw": cell(row, "owner")[:120],
                                  "gate_key": ";".join(gates), "start": dates["start"], "finish": dates["finish"], "status": status,
                                  "remaining": cell(row, "remaining")[:30], "depends_on": "; ".join(k for k in re.split(r"[;,\s]+", cell(row, "depends_on")) if k),
                                  **file_ref(source_id, commit, path, number)})
    return result


def preview(text: str, mapping: dict) -> dict:
    """Rows read and problems, for the mapping preview (Part 2 §9-4)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "wbs.csv").write_text(text, encoding="utf-8")
        result = extract(Path(tmp), "wbs.csv", json.dumps(mapping), source_id=0, commit="", project_id=0,
                         snapshot_id=0, gate_keys=set(_gate_values(text, mapping)))
    return {"rows": len(result.rows.get("task_state", [])), "problems": [f"line {p.line}: {p.reason}" for p in result.problems][:20]}


def _gate_values(text: str, mapping: dict) -> list[str]:
    header, rows = read_rows(text)
    col = mapping.get("columns", {}).get("gate")
    if col not in header:
        return []
    i = header.index(col)
    return sorted({g.strip() for r in rows if len(r) > i for g in re.split(r"[;,]", r[i]) if g.strip()})
