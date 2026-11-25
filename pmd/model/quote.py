"""The quote check (Design R-F2, R-O2; Part 3 §10-5).

A quote is accepted only if it is found in the source text. Before comparing,
runs of spaces and line breaks become one space on both sides; letters are
compared exactly. The line numbers come from where the program found the quote,
never from the model.
"""

from __future__ import annotations

import re
from datetime import date

MIN_LEN = 12
MAX_LEN = 600


def _normalise(text: str) -> tuple[str, list[int]]:
    """Collapsed text and, for each of its characters, the index in the original."""
    out: list[str] = []
    where: list[int] = []
    space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not space and out:
                out.append(" ")
                where.append(i)
            space = True
            continue
        space = False
        out.append(ch)
        where.append(i)
    if out and out[-1] == " ":
        out.pop()
        where.pop()
    return "".join(out), where


def find(quote: str, text: str) -> tuple[int, int] | None:
    """(first line, last line) of the quote in the text, 1-based, or None."""
    q, _ = _normalise(quote)
    if not (MIN_LEN <= len(q) <= MAX_LEN):
        return None
    t, where = _normalise(text)
    pos = t.find(q)
    if pos < 0:
        return None
    start, end = where[pos], where[pos + len(q) - 1]
    return text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 1


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def mentions_date(quote: str, day: date) -> bool:
    """Does the quote state this date in a common written form? (Part 3 §10-9, OB-4)."""
    text = " ".join(quote.lower().split())
    month, short = MONTHS[day.month - 1], MONTHS[day.month - 1][:3]
    d = day.day
    forms = [
        day.isoformat(), f"{day:%Y/%m/%d}", f"{d:02d}/{day.month:02d}/{day.year}", f"{day.month:02d}/{d:02d}/{day.year}",
        f"{d}/{day.month}/{day.year}", f"{day.month}/{d}/{day.year}", f"{d:02d}.{day.month:02d}.{day.year}",
    ]
    if any(f in text for f in forms):
        return True
    day_word = rf"0?{d}(st|nd|rd|th)?"
    month_word = rf"({month}|{short}\.?)"
    patterns = [rf"\b{day_word}\s+{month_word}\b", rf"\b{month_word}\s+{day_word}\b"]
    return any(re.search(p, text) for p in patterns)
