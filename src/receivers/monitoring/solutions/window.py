"""The check window: the last N *completed* UTC days, by calendar date."""

from __future__ import annotations

import datetime as dt

#: number of completed UTC days re-checked on every run
WINDOW_DAYS = 45


def utc_today(now: dt.datetime | None = None) -> dt.date:
    """The UTC calendar date of ``now`` (default: the current time).

    A naive ``now`` is refused: guessing its zone is how a check run near
    midnight lands on the wrong day.
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(dt.UTC).date()


def check_window(today: dt.date, days: int = WINDOW_DAYS) -> list[dt.date]:
    """``[today - days, today - 1]`` in ascending order; ``today`` is excluded.

    The window is defined by dates, never by row counts, so a sparse or
    unsorted file cannot shift it.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    return [today - dt.timedelta(days=k) for k in range(days, 0, -1)]
