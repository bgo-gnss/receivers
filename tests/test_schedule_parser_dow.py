"""`cron:` schedules use STANDARD cron weekday numbers (0 or 7 = Sunday).

APScheduler numbers weekdays 0 = Monday. The parser passed the field through
unchanged, so `cron: 40 6 * * 1` (meant Monday) fired on Tuesday — found on the
truncation_scan job, 2026-11-16. These tests ask APScheduler itself which days
fire, so they check the real behaviour, not a string.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.triggers.cron import CronTrigger

from receivers.scheduling.schedule_parser import parse_schedule

START = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)  # a Sunday


def fire_days(schedule: str, n: int = 14) -> list[str]:
    t = parse_schedule(schedule)
    trig = CronTrigger(timezone=timezone.utc, **t.trigger_kwargs)
    out, prev, now = [], None, START
    for _ in range(n):
        nxt = trig.get_next_fire_time(prev, now)
        out.append(nxt.strftime("%a"))
        prev, now = nxt, nxt + timedelta(seconds=1)
    return out


@pytest.mark.unit
@pytest.mark.parametrize(
    "dow, expected",
    [
        ("1", {"Mon"}),
        ("0", {"Sun"}),
        ("7", {"Sun"}),
        ("1-5", {"Mon", "Tue", "Wed", "Thu", "Fri"}),
        ("0-2", {"Sun", "Mon", "Tue"}),
        ("5-7", {"Fri", "Sat", "Sun"}),
        ("1,3,5", {"Mon", "Wed", "Fri"}),
        ("mon-fri", {"Mon", "Tue", "Wed", "Thu", "Fri"}),
        ("sun", {"Sun"}),
    ],
)
def test_weekday_numbers_follow_standard_cron(dow, expected):
    assert set(fire_days(f"cron: 40 6 * * {dow}")) == expected


@pytest.mark.unit
def test_the_truncation_scan_default_fires_on_monday():
    assert set(fire_days("cron: 40 6 * * 1", n=4)) == {"Mon"}


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["*/2", "1-5/2", "8", "5-1"])
def test_untranslatable_weekday_fields_are_refused(bad):
    with pytest.raises(ValueError):
        parse_schedule(f"cron: 0 6 * * {bad}")


@pytest.mark.unit
def test_fields_without_a_weekday_are_unchanged():
    t = parse_schedule("cron: */15 * * * *")
    assert t.trigger_kwargs == {"minute": "*/15"}
