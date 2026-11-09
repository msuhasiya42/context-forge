"""Context resolver (spec 9.1): the period a request names and its sources' readiness, in code.

The planner reads an explicit window instead of interpreting "September" or "this week" itself:
a period the model guessed is a silent error the eval cannot see, and a model left to it asks
the user (live: "Please specify the period"). A handful of patterns, no date library. Months
are whole calendar months; days and weeks count from today (a week starts on Monday). Text
naming none resolves to no window.
"""

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from contextforge.core.entities.artifact import Artifact

# A calendar month as (year, month); tuples order chronologically.
YearMonth = tuple[int, int]
# A window of whole days, first and last included.
Window = tuple[date, date]

# Spelled out: calendar.month_name follows the process locale, and requests are in English.
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_NUMBERS = {
    **{name: number for number, name in enumerate(_MONTHS, start=1)},
    **{name[:3]: number for number, name in enumerate(_MONTHS, start=1)},
    "sept": 9,
}
_MONTH_NAME = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + r")\b(?:\s+(\d{4})\b)?",
    re.IGNORECASE,
)
_LAST_MONTH = re.compile(r"\blast month\b", re.IGNORECASE)
_THIS_MONTH = re.compile(r"\bthis month\b", re.IGNORECASE)
_LAST_N_MONTHS = re.compile(r"\blast (\d+) months?\b", re.IGNORECASE)
_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_THIS_WEEK = re.compile(r"\bthis week\b", re.IGNORECASE)
_LAST_WEEK = re.compile(r"\b(?:last|previous) week\b", re.IGNORECASE)
_PAST_WEEK = re.compile(r"\bpast week\b", re.IGNORECASE)
_PAST_N_DAYS = re.compile(r"\b(?:last|past) (\d+) days?\b", re.IGNORECASE)


@dataclass(frozen=True)
class SourceStatus:
    artifact_id: str
    index_status: str


@dataclass(frozen=True)
class ResolvedContext:
    """Dates are ISO ``YYYY-MM-DD`` in ``timezone``; the period is None when none was named."""

    period_start: str | None
    period_end: str | None
    today: str
    timezone: str
    sources: list[SourceStatus]

    @property
    def period(self) -> str | None:
        """The window as ``"<start> to <end>"``, the form it takes in the planner's context."""
        if self.period_start is None:
            return None
        return f"{self.period_start} to {self.period_end}"


def resolve_context(
    request_text: str, artifacts: list[Artifact], *, today: date, timezone: str
) -> ResolvedContext:
    """Resolve the windows ``request_text`` names relative to ``today``, the user's local date.

    Several windows give one, from the earliest first day to the latest last day.
    """
    windows = [
        *(_month_window(m) for pattern in _MONTH_PATTERNS for m in pattern(request_text, today)),
        *(window for pattern in _DAY_PATTERNS for window in pattern(request_text, today)),
    ]
    first = min((start for start, _ in windows), default=None)
    last = max((end for _, end in windows), default=None)
    return ResolvedContext(
        period_start=first.isoformat() if first else None,
        period_end=last.isoformat() if last else None,
        today=today.isoformat(),
        timezone=timezone,
        sources=[SourceStatus(a.artifact_id, a.index_status.value) for a in artifacts],
    )


def _named_months(text: str, today: date) -> list[YearMonth]:
    """ "September", "Sep" or "Sep 2025"; without a year, the latest one not after ``today``."""
    months: list[YearMonth] = []
    for match in _MONTH_NAME.finditer(text):
        name, year = match.group(1), match.group(2)
        # Lowercase "may" is the verb ("which may be duplicates"); only "May" names the month.
        if name.lower() == "may" and name != "May":
            continue
        month = _MONTH_NUMBERS[name.lower()]
        months.append((int(year), month) if year else _latest(month, today))
    return months


def _last_month(text: str, today: date) -> list[YearMonth]:
    return [_shift(today, -1)] if _LAST_MONTH.search(text) else []


def _this_month(text: str, today: date) -> list[YearMonth]:
    return [_shift(today, 0)] if _THIS_MONTH.search(text) else []


def _last_n_months(text: str, today: date) -> list[YearMonth]:
    """ "last 3 months": the three whole months before the current one."""
    return [
        _shift(today, -offset)
        for match in _LAST_N_MONTHS.finditer(text)
        for offset in range(1, int(match.group(1)) + 1)
    ]


_MONTH_PATTERNS: tuple[Callable[[str, date], list[YearMonth]], ...] = (
    _named_months,
    _last_month,
    _this_month,
    _last_n_months,
)


def _days(text: str, today: date) -> list[Window]:
    """ "today", "yesterday" and "past 3 days" (today and the two days before it)."""
    windows = [(today, today)] if _TODAY.search(text) else []
    if _YESTERDAY.search(text):
        windows.append((today - timedelta(days=1), today - timedelta(days=1)))
    windows += [
        (today - timedelta(days=int(match.group(1)) - 1), today)
        for match in _PAST_N_DAYS.finditer(text)
        if int(match.group(1)) >= 1
    ]
    return windows


def _weeks(text: str, today: date) -> list[Window]:
    """ "this week": Monday to today; "last week": the Monday-to-Sunday week before it;
    "past week": the seven days ending today."""
    monday = today - timedelta(days=today.weekday())
    windows = [(monday, today)] if _THIS_WEEK.search(text) else []
    if _LAST_WEEK.search(text):
        windows.append((monday - timedelta(days=7), monday - timedelta(days=1)))
    if _PAST_WEEK.search(text):
        windows.append((today - timedelta(days=6), today))
    return windows


_DAY_PATTERNS: tuple[Callable[[str, date], list[Window]], ...] = (_days, _weeks)


def _month_window(month: YearMonth) -> Window:
    return date(*month, 1), _last_day(month)


def _latest(month: int, today: date) -> YearMonth:
    """The most recent ``month`` that starts on or before ``today``."""
    return (today.year, month) if month <= today.month else (today.year - 1, month)


def _shift(today: date, months: int) -> YearMonth:
    """The calendar month ``months`` away from the one ``today`` is in."""
    year, zero_based = divmod(today.year * 12 + today.month - 1 + months, 12)
    return year, zero_based + 1


def _last_day(month: YearMonth) -> date:
    year, number = month
    return date(year, number, calendar.monthrange(year, number)[1])
