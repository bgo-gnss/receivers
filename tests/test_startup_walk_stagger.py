"""The three heavy startup walks must not overlap each other.

gap_detection, archive_reconciler and integrity_checker each take 22-27 min on
rek-d01 over the same ~180 stations on the same 40-thread executor. Their
steady-state schedules were moved to cron hours 1 h apart (gps-config-data
2026-09-13) so they can never coincide -- but their "run once shortly after
start" jobs were 60/120/180 s apart, which made them ~95 % concurrent and
reproduced the whole pile-up on every restart, while the scheduler was also
re-registering 848 jobs.

These tests pin the ORDERING and the MINIMUM SEPARATION, not the exact numbers,
so retuning the delays stays cheap while re-collapsing them fails loudly.
"""

import re
from pathlib import Path

import pytest

SRC = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "receivers"
    / "scheduling"
    / "bulk_scheduler.py"
)

# Each walk's measured median runtime on rek-d01 2026-09-06..13 (seconds).
LONGEST_WALK_S = 1624

STARTUP_JOBS = (
    "gap_detection_startup",
    "archive_reconciler_startup",
    "integrity_checker_startup",
)


def _startup_delays():
    """Map startup job id -> its run_date delay in seconds, read from source."""
    text = SRC.read_text()
    out = {}
    for job_id in STARTUP_JOBS:
        # The add_job call puts run_date before id=; take the nearest preceding one.
        idx = text.index(f'id="{job_id}"')
        window = text[max(0, idx - 1200) : idx]
        matches = re.findall(r"timedelta\(seconds=(\d+)\)", window)
        assert matches, f"no run_date delay found for {job_id}"
        out[job_id] = int(matches[-1])
    return out


@pytest.fixture(scope="module")
def delays():
    return _startup_delays()


def test_all_three_startup_jobs_are_present(delays):
    assert set(delays) == set(STARTUP_JOBS)


def test_gap_detection_runs_first_and_soon(delays):
    """It is the only thing that re-activates a completed backfill_progress row,
    and with a cron trigger the next scheduled run may be 6 h away."""
    assert delays["gap_detection_startup"] <= 300
    assert delays["gap_detection_startup"] == min(delays.values())


@pytest.mark.parametrize(
    "earlier,later",
    [
        ("gap_detection_startup", "archive_reconciler_startup"),
        ("archive_reconciler_startup", "integrity_checker_startup"),
    ],
)
def test_consecutive_walks_do_not_overlap(delays, earlier, later):
    """Separation must exceed the longest measured walk, or they run together."""
    gap = delays[later] - delays[earlier]
    assert gap >= LONGEST_WALK_S, (
        f"{earlier} -> {later} separated by only {gap}s; the longest measured "
        f"walk is {LONGEST_WALK_S}s, so they would overlap on every restart"
    )


def test_ordering_is_strictly_increasing(delays):
    ordered = [delays[j] for j in STARTUP_JOBS]
    assert ordered == sorted(ordered), f"startup delays out of order: {delays}"
