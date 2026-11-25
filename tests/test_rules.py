"""Gate and task rules (Design §5, §6-9 R-V1; Part 2 §9-5)."""

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from pmd.rules.gates import need_call_count, split_next_gate, task_days_late


def gate(key, d, outcome="", closed=None):
    return {"key": key, "date": d, "outcome": outcome, "closed_at": closed}


def test_next_gate_is_earliest_open_even_if_past():
    gates = [gate("G2", "2026-12-18"), gate("G0", "2026-11-20"), gate("G1", "2026-12-04")]
    nxt, later = split_next_gate(gates)
    assert nxt["key"] == "G0"
    assert [g["key"] for g in later] == ["G1", "G2"]


def test_next_gate_skips_gates_with_outcome_or_closed():
    gates = [gate("G0", "2026-11-20", outcome="PASS"), gate("X", "2026-11-21", closed="t"), gate("G1", "2026-12-04")]
    nxt, later = split_next_gate(gates)
    assert nxt["key"] == "G1" and later == []
    assert split_next_gate([]) == (None, [])


def test_task_days_late():
    today = date(2026, 11, 24)
    assert task_days_late({"finish": "2026-11-23", "status": "IN_PROGRESS"}, today) == 1
    assert task_days_late({"finish": "2026-11-20", "status": "NOT_STARTED"}, today) == 2  # Mon, Tue
    assert task_days_late({"finish": "2026-11-23", "status": "DONE"}, today) == 0
    assert task_days_late({"finish": "2026-11-23", "status": "DEFERRED"}, today) == 0
    assert task_days_late({"finish": "", "status": "IN_PROGRESS"}, today) == 0
    assert task_days_late({"finish": "2026-11-24", "status": "IN_PROGRESS"}, today) == 0


values = st.sampled_from(["ON_TRACK", "AT_RISK", "LATE"])


@given(st.dictionaries(st.integers(1, 30), values), st.dictionaries(st.integers(1, 30), values))
def test_need_call_count_matches_definition(proposals, confirmations):
    gate_ids = set(range(1, 31))
    expected = len([g for g in gate_ids if g in proposals and proposals[g] != confirmations.get(g)])
    assert need_call_count(gate_ids, proposals, confirmations) == expected


def test_no_proposals_means_nothing_to_decide():
    assert need_call_count({1, 2, 3}, {}, {1: "ON_TRACK"}) == 0
