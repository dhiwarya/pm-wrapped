"""Working days (Part 2 §6-2) and property test PR-9 (Part 3 §13-3)."""

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from pmd.rules.workdays import is_working_day, working_days_left, working_days_past

days = st.dates(min_value=date(2020, 1, 1), max_value=date(2032, 12, 31))
holiday_sets = st.sets(days, max_size=20)


def test_examples():
    tue = date(2026, 11, 24)
    assert working_days_left(tue, date(2026, 11, 27)) == 3  # Wed, Thu, Fri
    assert working_days_left(tue, date(2026, 12, 4)) == 8
    assert working_days_left(tue, date(2026, 11, 29)) == 3  # weekend adds nothing
    assert working_days_left(tue, date(2026, 11, 27), {date(2026, 11, 26)}) == 2
    assert working_days_past(date(2026, 11, 30), date(2026, 11, 27)) == 1  # Monday only
    assert not is_working_day(date(2026, 11, 28))


@given(days, days, holiday_sets)
def test_never_negative(today, gate, holidays):
    assert working_days_left(today, gate, holidays) >= 0
    assert working_days_past(today, gate, holidays) >= 0


@given(days, holiday_sets)
def test_zero_on_gate_day(day, holidays):
    assert working_days_left(day, day, holidays) == 0


@given(days, days, holiday_sets, days)
def test_adding_a_holiday_never_raises_the_count(today, gate, holidays, extra):
    assert working_days_left(today, gate, holidays | {extra}) <= working_days_left(today, gate, holidays)


@given(days, st.integers(min_value=0, max_value=400), holiday_sets)
def test_count_equals_number_of_working_days_in_range(today, span, holidays):
    gate = today + timedelta(days=span)
    expected = sum(1 for i in range(1, span + 1) if is_working_day(today + timedelta(days=i), holidays))
    assert working_days_left(today, gate, holidays) == expected
