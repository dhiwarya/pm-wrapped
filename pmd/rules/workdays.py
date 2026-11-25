"""Working days (Detailed Design Part 2 §6-2).

A working day is Monday to Friday, less the holidays of the project. "Working days
left" counts the days after today up to and including the gate date.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, timedelta


def is_working_day(day: date, holidays: Collection[date] = ()) -> bool:
    return day.weekday() < 5 and day not in holidays


def working_days_between(after: date, up_to: date, holidays: Collection[date] = ()) -> int:
    """Working days ``d`` with ``after < d <= up_to``. Zero when ``up_to <= after``."""
    if up_to <= after:
        return 0
    count = 0
    day = after + timedelta(days=1)
    while day <= up_to:
        if is_working_day(day, holidays):
            count += 1
        day += timedelta(days=1)
    return count


def working_days_left(today: date, gate_date: date, holidays: Collection[date] = ()) -> int:
    return working_days_between(today, gate_date, holidays)


def working_days_past(today: date, gate_date: date, holidays: Collection[date] = ()) -> int:
    return working_days_between(gate_date, today, holidays)
