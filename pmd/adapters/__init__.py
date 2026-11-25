"""Adapters turn exported files and saved API answers into rows (Part 1 §4-3, Part 2 §8).

Each adapter is a pure function of its input. It does not write the database and
does not call the network. What it cannot read goes to ``problems``, never an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Problem:
    adapter: str
    path: str
    reason: str
    line: int | None = None


@dataclass
class Result:
    rows: dict[str, list[dict]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def add(self, table: str, row: dict) -> None:
        self.rows.setdefault(table, []).append(row)

    def problem(self, adapter: str, path: str, reason: str, line: int | None = None) -> None:
        self.problems.append(Problem(adapter, path, reason, line))

    def merge(self, other: "Result") -> None:
        for table, rows in other.rows.items():
            self.rows.setdefault(table, []).extend(rows)
        self.problems.extend(other.problems)


def file_ref(source_id: int, commit: str, path: str, line_from: int | None = None, line_to: int | None = None) -> dict:
    return {"src_source_id": source_id, "src_commit": commit, "src_path": path,
            "src_line_from": line_from, "src_line_to": line_to if line_to is not None else line_from}


def object_ref(source_id: int, kind: str, number: int, url: str) -> dict:
    return {"src_source_id": source_id, "src_object": kind, "src_number": number, "src_url": url}
