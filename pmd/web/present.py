"""Turns hand data into what the templates show (Design §6, §6-9).

Pure functions over rows: no writes. Phase 1 has no run, so there are no
proposals, no run states of evidence items and no facts yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..data import hand
from ..rules.gates import need_call_count, split_next_gate, task_days_late
from ..rules.workdays import working_days_left, working_days_past

# Design §6-9 R-V2: icon + word for every status, and the tile colour of the value.
STATUS = {
    "ON_TRACK": {"cls": "good", "icon": "✓", "word": "On track", "tile": "t-mint"},
    "AT_RISK": {"cls": "warn", "icon": "▲", "word": "At risk", "tile": "t-butter"},
    "LATE": {"cls": "crit", "icon": "✕", "word": "Late", "tile": "t-red"},
    "UNKNOWN": {"cls": "unk", "icon": "?", "word": "Unknown", "tile": "t-night"},
}
OUTCOME = {
    "PASS": {"cls": "good", "icon": "✓", "word": "Pass"},
    "CONDITIONAL_PASS": {"cls": "warn", "icon": "◐", "word": "Conditional pass"},
    "REWORK": {"cls": "crit", "icon": "↺", "word": "Rework required"},
}
# R-V3: symbol + word for evidence states. WAIVED is a hand state outside the bar.
ITEM_STATE = {
    "MET": {"icon": "●", "word": "MET", "o": "o1"},
    "CANDIDATE": {"icon": "◕", "word": "CANDIDATE", "o": "o2"},
    "IN_PROGRESS": {"icon": "◑", "word": "IN PROGRESS", "o": "o3"},
    "NOT_STARTED": {"icon": "○", "word": "NOT STARTED", "o": "o4"},
    "WAIVED": {"icon": "⊘", "word": "WAIVED", "o": ""},
}
TASK_STATUS_WORD = {
    "NOT_STARTED": "Not started",
    "IN_PROGRESS": "In progress",
    "DONE": "Done",
    "BLOCKED": "Blocked",
    "DEFERRED": "Deferred",
}
COLOUR_TILE = {c: f"t-{c.lower()}" for c in hand.COLOURS}
COLOUR_WORD = {c: c.capitalize() for c in hand.COLOURS}


def zone(conn: sqlite3.Connection):
    name = hand.get_setting(conn, "time_zone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None  # host local time


def today(conn: sqlite3.Connection, now: datetime) -> date:
    tz = zone(conn)
    return (now.astimezone(tz) if tz else now.astimezone()).date()


def local_stamp(stamp: str, tz) -> datetime:
    """A stored UTC time (``2026-11-25T01:02:03Z``) in the user's time zone."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(tz) if tz else moment.astimezone()


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def long_date(iso: str | date) -> str:
    d = iso if isinstance(iso, date) else date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B}"


def full_date(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B} {d.year}"


def plural(n: int, word: str) -> str:
    return f"{word}" if n == 1 else f"{word}s"


def item_state(row: sqlite3.Row) -> str:
    """Phase 1: no run, so no run state. An item without a hand state is NOT_STARTED (R-E2)."""
    return row["hand_state"] or "NOT_STARTED"


@dataclass
class EvidenceSummary:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    @property
    def segments(self) -> list[dict]:
        return [
            {"state": s, "n": self.counts[s], **ITEM_STATE[s]}
            for s in ("MET", "CANDIDATE", "IN_PROGRESS", "NOT_STARTED")
            if self.counts.get(s)
        ]

    @property
    def line(self) -> str:
        if not self.total:
            return ""
        parts = [f"{self.counts.get('MET', 0)} of {self.total} met"]
        if self.counts.get("WAIVED"):
            parts.append(f"{self.counts['WAIVED']} waived")
        return " · ".join(parts)


def summarise(items: list[sqlite3.Row]) -> EvidenceSummary:
    summary = EvidenceSummary()
    for row in items:
        state = item_state(row)
        summary.counts[state] = summary.counts.get(state, 0) + 1
        summary.total += 1
    return summary


def gate_view(gate: sqlite3.Row, today_: date, holidays: set[date], confirmation: sqlite3.Row | None, tz=None) -> dict:
    gdate = date.fromisoformat(gate["date"])
    is_past = gdate < today_
    health = confirmation["value"] if confirmation else "UNKNOWN"
    return {
        "row": gate,
        "id": gate["id"],
        "key": gate["key"],
        "name": gate["name"],
        "date": gate["date"],
        "short": short_date(gate["date"]),
        "long": long_date(gate["date"]),
        "is_past": is_past,
        "left": working_days_left(today_, gdate, holidays),
        "past": working_days_past(today_, gdate, holidays),
        "health": health,
        "status": STATUS[health],
        "confirmation": confirmation,
        "confirmed_on": short_date(local_stamp(confirmation["created_at"], tz).date().isoformat()) if confirmation else "",
        "outcome": OUTCOME.get(gate["outcome"]),
    }


def build_today(conn: sqlite3.Connection, now: datetime) -> dict:
    """V-1 Today (Design §6-1), Phase 1 form."""
    day = today(conn, now)
    projects = hand.list_projects(conn)
    blocks = []
    open_ids: list[int] = []
    for p in projects:
        holidays = hand.holiday_dates(conn, p["id"])
        gates = hand.list_gates(conn, p["id"])
        nxt, later = split_next_gate(gates)
        open_ids += [g["id"] for g in gates if not g["outcome"]]
        block = {
            "project": p,
            "tile": COLOUR_TILE[p["colour"]],
            "colour": p["colour"],
            "next": None,
            "later": [{"key": g["key"], "short": short_date(g["date"])} for g in later],
            "evidence": None,
        }
        if nxt is not None:
            conf = hand.latest_confirmations(conn, [nxt["id"]]).get(nxt["id"])
            block["next"] = gate_view(nxt, day, holidays, conf, zone(conn))
            block["evidence"] = summarise(hand.list_items(conn, [nxt["id"]])[nxt["id"]])
        blocks.append(block)
    blocks.sort(key=lambda b: (b["next"] is None, b["next"]["date"] if b["next"] else "", b["project"]["name"].lower()))
    # R-V1. No proposals exist before the run (Phase 2), so the count is 0.
    need = need_call_count(open_ids, proposals={}, confirmations={})
    return {"day": day, "day_text": full_date(day), "blocks": blocks, "need": need, "count": len(blocks)}


def build_project(conn: sqlite3.Connection, project: sqlite3.Row, now: datetime) -> dict:
    """V-5 Project (Design §6-5), Phase 1 form."""
    day = today(conn, now)
    tz = zone(conn)
    holidays = hand.holiday_dates(conn, project["id"])
    gates = hand.list_gates(conn, project["id"])
    closed = [g for g in hand.list_gates(conn, project["id"], include_closed=True) if g["closed_at"]]
    confs = hand.latest_confirmations(conn, [g["id"] for g in gates])
    items = hand.list_items(conn, [g["id"] for g in gates])
    gate_views = []
    for g in gates:
        view = gate_view(g, day, holidays, confs.get(g["id"]), tz)
        view["items"] = [
            {"row": i, "state": item_state(i), "look": ITEM_STATE[item_state(i)]} for i in items[g["id"]]
        ]
        view["summary"] = summarise(items[g["id"]])
        view["history"] = [
            {"row": c, "when": f"{local_stamp(c['created_at'], tz):%a %d %b %Y, %H:%M}"}
            for c in hand.confirmation_history(conn, g["id"])
        ]
        gate_views.append(view)
    tasks = []
    for t in hand.list_tasks(conn, project["id"]):
        tasks.append({"row": t, "late": task_days_late(t, day, holidays), "status_word": TASK_STATUS_WORD[t["status"]]})
    return {
        "project": project,
        "tile": COLOUR_TILE[project["colour"]],
        "day": day,
        "gates": gate_views,
        "closed": closed,
        "tasks": tasks,
        "late_count": sum(1 for t in tasks if t["late"]),
        "holidays": hand.list_holidays(conn, project["id"]),
        "global_holidays": hand.list_holidays(conn),
        "people": hand.list_people(conn),
    }
