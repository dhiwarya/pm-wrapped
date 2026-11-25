"""Rules about gates and tasks that need no run (Design §5, §6-9)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date
from typing import Any

from .workdays import working_days_between

FINISHED_TASK = ("DONE", "DEFERRED")


def is_open_gate(gate: Mapping[str, Any]) -> bool:
    return not gate["outcome"] and gate["closed_at"] is None


def split_next_gate(gates: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, list]:
    """The next gate is the earliest open gate, even when its date has passed (R-J1)."""
    open_gates = sorted((g for g in gates if is_open_gate(g)), key=lambda g: (g["date"], g["key"]))
    if not open_gates:
        return None, []
    return open_gates[0], open_gates[1:]


def task_days_late(task: Mapping[str, Any], today: date, holidays: Collection[date] = ()) -> int:
    """Working days a task is late (Part 2 §9-5, TASK_LATE). Zero when not late."""
    if not task["finish"] or task["status"] in FINISHED_TASK:
        return 0
    finish = date.fromisoformat(task["finish"])
    if finish >= today:
        return 0
    return working_days_between(finish, today, holidays)


def need_call_count(
    open_gate_ids: Collection[int],
    proposals: Mapping[int, str],
    confirmations: Mapping[int, str],
) -> int:
    """R-V1: open gates whose proposal differs from the confirmed value, including
    gates with no confirmed value yet. A gate with no proposal is not counted."""
    return sum(1 for gid in open_gate_ids if gid in proposals and proposals[gid] != confirmations.get(gid))
