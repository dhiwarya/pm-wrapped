"""OB-2: what a program can find exactly, with no model (Part 3 §10-9)."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from pathlib import Path

from ..adapters import file_ref, wbs
from ..adapters.aidlc import HEADING

NAME_WORDS = ("milestone", "name", "step", "phase", "release", "checkpoint", "title")
DATE_WORDS = ("date", "due", "deadline", "acceptance", "target", "when")
KEY_WORDS = ("key", "id", "code", "gate", "#")
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
WORDY_DATE = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})\b", re.I)


def parse_any_date(text: str) -> date | None:
    for rx, make in DATE_PATTERNS:
        m = rx.search(text)
        if m:
            try:
                return make(m)
            except ValueError:
                return None
    m = WORDY_DATE.search(text)
    if m:
        try:
            return date(int(m[3]), MONTHS[m[2].lower()[:3]], int(m[1]))
        except ValueError:
            return None
    return None


def text_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes)


def wbs_candidates(root: Path, source_id: int, source_key: str, commit: str) -> list[dict]:
    found = []
    for path in text_files(root, (".csv", ".tsv")):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        header, rows = wbs.read_rows(text)
        mapping = wbs.guess_mapping(header, rows)
        if not mapping:
            continue
        rel = path.relative_to(root).as_posix()
        pv = wbs.preview(text, mapping)
        found.append({"kind": "WBS", "identity": "wbs", "read_by": "PROGRAM",
                      "payload": {"source_key": source_key, "path": rel, "mapping": mapping, "rows": pv["rows"], "problems": pv["problems"]},
                      "src": file_ref(source_id, commit, rel, 1)})
    found.sort(key=lambda f: -f["payload"]["rows"])
    return found[:1]


def dashboard_candidates(root: Path, source_id: int, source_key: str, commit: str) -> list[dict]:
    out = []
    for path in sorted(root.rglob("dashboard-state.json")):
        rel = path.relative_to(root).as_posix()
        out.append({"kind": "DASHBOARD_FILE", "identity": "dashboard", "read_by": "PROGRAM",
                    "payload": {"source_key": source_key, "path": rel}, "src": file_ref(source_id, commit, rel, 1)})
    return out[:1]


def branch_pattern(branches: list[str], source_id: int, commit: str, mirror_path: str) -> list[dict]:
    """``unit/<unit>`` and ``<unit>/<topic>`` when at least three branches fit."""
    unit_branches = [b for b in branches if re.fullmatch(r"unit/[A-Za-z0-9._-]+", b)]
    if len(unit_branches) < 3:
        prefixes = Counter(b.split("/", 1)[0] for b in branches if "/" in b)
        common = [p for p, n in prefixes.items() if n >= 3 and p not in ("feature", "fix", "bugfix", "hotfix", "release", "chore", "docs", "dependabot")]
        if not common:
            return []
        pattern, examples = "<unit>/<topic>", [b for b in branches if b.split("/", 1)[0] in common][:5]
    else:
        pattern = "unit/<unit>; <unit>/<topic>"
        examples = unit_branches[:5]
    return [{"kind": "PATTERN", "identity": "BRANCH_UNIT", "read_by": "PROGRAM",
             "payload": {"which": "BRANCH_UNIT", "pattern": pattern, "examples": examples},
             "src": {"src_source_id": source_id, "src_commit": commit, "src_path": "(branches)", "src_line_from": None, "src_line_to": None}}]


def task_key_pattern(wbs_proposal: dict | None, pr_titles: list[str], source_id: int, commit: str) -> list[dict]:
    """A key shape from WBS keys, kept only if PR titles use it."""
    if not wbs_proposal:
        return []
    root_keys = wbs_proposal.get("_keys", [])
    if not root_keys:
        return []
    shapes = Counter(re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", k)) for k in root_keys)
    shape = shapes.most_common(1)[0][0]
    parts = []
    for m in re.finditer(r"9+|A+|.", shape):
        run = m.group(0)
        parts.append(r"\d{%d}" % len(run) if run[0] == "9" else ("[A-Za-z]{%d}" % len(run) if run[0] == "A" else re.escape(run)))
    pattern = r"\b" + "".join(parts) + r"\b"
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []
    hits = [t for t in pr_titles if compiled.search(t)]
    if pr_titles and not hits:
        return []
    return [{"kind": "PATTERN", "identity": "TASK_KEY", "read_by": "PROGRAM",
             "payload": {"which": "TASK_KEY", "pattern": pattern, "examples": (hits or root_keys)[:5]},
             "src": {"src_source_id": source_id, "src_commit": commit, "src_path": wbs_proposal["payload"]["path"], "src_line_from": 1, "src_line_to": 1}}]


def people(authors: list[tuple[str, str]], pr_people: list[str], wbs_owners: list[str], source_id: int, commit: str) -> list[dict]:
    out: dict[str, dict] = {}
    for name, email in authors[:40]:
        key = name.lower()
        entry = out.setdefault(key, {"display_name": name, "aliases": []})
        if email and "noreply" not in email:
            entry["aliases"].append({"kind": "GIT", "value": email})
    first_names: dict[str, list[str]] = {}
    for key, entry in out.items():
        first_names.setdefault(entry["display_name"].split()[0].lower(), []).append(key)
    for name in wbs_owners:
        if name.lower() in out:
            out[name.lower()]["aliases"].append({"kind": "WBS", "value": name})
            continue
        same = first_names.get(name.lower(), [])
        if len(same) == 1 and " " not in name.strip():
            # "Rani" in the WBS is the one git author "Rani Putri": one person, two aliases
            out[same[0]]["aliases"].append({"kind": "WBS", "value": name})
            continue
        out[name.lower()] = {"display_name": name, "aliases": [{"kind": "WBS", "value": name}]}
    for login in pr_people:
        if login and not login.endswith("[bot]") and login.lower() not in out:
            out[login.lower()] = {"display_name": login, "aliases": [{"kind": "GITHUB", "value": login}]}
    return [{"kind": "PERSON", "identity": k, "read_by": "PROGRAM", "payload": v,
             "src": {"src_source_id": source_id, "src_commit": commit, "src_path": "(commit authors, PRs and WBS owners)",
                     "src_line_from": None, "src_line_to": None}} for k, v in out.items()]


def milestone_tables(root: Path, source_id: int, commit: str) -> list[dict]:
    """Dated rows of Markdown tables whose header names a milestone and a date column."""
    out = []
    for path in text_files(root, (".md", ".markdown")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = path.relative_to(root).as_posix()
        i = 0
        while i < len(lines) - 2:
            head = lines[i]
            if head.strip().startswith("|") and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
                cols = [c.strip().lower() for c in head.strip().strip("|").split("|")]
                k_col = next((n for n, c in enumerate(cols) if c in KEY_WORDS), None)
                m_col = next((n for n, c in enumerate(cols) if n != k_col and any(w in c for w in NAME_WORDS)), None)
                if m_col is None and k_col is not None and cols[k_col] == "gate":
                    m_col, k_col = k_col, None  # a lone "Gate" column names the gate
                d_col = next((n for n, c in enumerate(cols) if any(w in c for w in DATE_WORDS) and n not in (m_col, k_col)), None)
                j = i + 2
                if m_col is not None and d_col is not None:
                    while j < len(lines) and lines[j].strip().startswith("|"):
                        cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                        if len(cells) > max(m_col, d_col):
                            when = parse_any_date(cells[d_col])
                            name = re.sub(r"[*_`]", "", cells[m_col]).strip()
                            if when and name:
                                key = re.sub(r"[*_`]", "", cells[k_col]).strip() if k_col is not None and len(cells) > k_col else ""
                                if not key:
                                    first = name.split()[0]
                                    key = first if re.fullmatch(r"[A-Za-z]{0,4}\d{1,3}", first) else ""
                                out.append({"kind": "GATE", "identity": "", "read_by": "PROGRAM", "date_origin": "PLAN_TABLE",
                                            "payload": {"key": key, "name": name[:120], "date": when.isoformat(), "description": ""},
                                            "quote": lines[j].strip()[:600], "src": file_ref(source_id, commit, rel, j + 1)})
                        j += 1
                i = j
                continue
            i += 1
    # Name keys that are still empty: M1, M2... in date order.
    n = 1
    for g in sorted(out, key=lambda g: g["payload"]["date"]):
        if not g["payload"]["key"]:
            g["payload"]["key"] = f"M{n}"
            n += 1
        g["identity"] = g["payload"]["key"].lower()
    return out


def project_from_readme(root: Path, source_id: int, commit: str) -> list[dict]:
    for name in ("README.md", "readme.md", "README.markdown", "README"):
        path = root / name
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            title, title_line, para = "", None, []
            for n, line in enumerate(lines, start=1):
                h = HEADING.match(line)
                if h and not title:
                    title, title_line = h.group(2).strip(), n
                    continue
                if title and line.strip() and not line.startswith(("#", "|", "!", "<", "[", "```")):
                    para.append(line.strip())
                elif title and para:
                    break
            if title:
                return [{"kind": "PROJECT", "identity": "project", "read_by": "PROGRAM",
                         "payload": {"name": title[:120], "description": "", "scope_note": " ".join(para)[:1000]},
                         "src": file_ref(source_id, commit, name, title_line)}]
    return []
