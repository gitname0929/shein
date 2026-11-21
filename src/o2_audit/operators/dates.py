"""Gregorian calendar operators: strict parsing, closed intervals, day addition."""
from __future__ import annotations

import re
from datetime import date, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DateLike = date


class DateParseError(ValueError):
    pass


def parse_date(value: object) -> date:
    """Strict ``YYYY-MM-DD`` real Gregorian date.

    Rejects non-strings, wrong widths (``2026-6-15``), impossible dates
    (``2026-02-30``) and bool/None.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise DateParseError(f"date must be a YYYY-MM-DD string, got {type(value).__name__}")
    if not _DATE_RE.fullmatch(value):
        raise DateParseError(f"date must be YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise DateParseError(f"not a real Gregorian date: {value!r}") from e


def date_in_closed_interval(target: date, start: date, end: date) -> bool:
    """Closed interval membership: both endpoints are included."""
    return start <= target <= end


def interval_is_ordered(start: date, end: date) -> bool:
    """start <= end (validity period must not run backwards)."""
    return start <= end


def add_days(d: date, days: int) -> date:
    if isinstance(days, bool) or not isinstance(days, int):
        raise DateParseError("day offset must be an integer")
    return d + timedelta(days=days)
