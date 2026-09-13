"""Retiring persisted jobs whose ids changed while the feature stayed enabled.

Production bug, 2026-09-13: ``morning_recovery``'s job id is
``morning_recovery`` for a single-fire schedule and ``morning_recovery_{idx}``
once ``schedule`` becomes a list. When the deployed config grew to three fires
the unsuffixed job was never removed, so TWO "Morning recovery starting" passes
ran at 01:30 every night — one of them carrying the stale args from whenever
the schedule was last a single string (days_back=3 vs the current 7) — and both
downloaded the same stations a minute apart.

``_mark_disabled`` only covers "feature switched off". This covers "feature
still on, ids moved", which is the same jobstore-is-authoritative trap as the
2026-08 "archive_sync disabled but still pushing" bug.
"""

import logging

import pytest


class _Job:
    def __init__(self, job_id):
        self.id = job_id


class _FakeScheduler:
    """Minimal stand-in: records removals, raises for unknown ids."""

    def __init__(self, job_ids):
        self._jobs = {j: _Job(j) for j in job_ids}
        self.removed = []

    def get_jobs(self):
        return list(self._jobs.values())

    def remove_job(self, job_id):
        from apscheduler.jobstores.base import JobLookupError

        if job_id not in self._jobs:
            raise JobLookupError(job_id)
        del self._jobs[job_id]
        self.removed.append(job_id)


@pytest.fixture
def sched():
    """A BulkDownloadScheduler shell with only the bits under test wired."""
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    obj = BulkDownloadScheduler.__new__(BulkDownloadScheduler)
    obj.logger = logging.getLogger("test.retire")
    obj._disabled_jobs = []
    obj._job_family_keep = {}
    return obj


def test_stale_family_member_is_removed(sched):
    """The exact production shape: _0/_1/_2 registered, bare id left over."""
    sched.scheduler = _FakeScheduler(
        [
            "morning_recovery",  # stale
            "morning_recovery_0",
            "morning_recovery_1",
            "morning_recovery_2",
            "gap_detection",
        ]
    )
    sched._retire_stale_family(
        "morning_recovery",
        ["morning_recovery_0", "morning_recovery_1", "morning_recovery_2"],
    )
    sched._remove_disabled_jobs()

    assert sched.scheduler.removed == ["morning_recovery"]
    remaining = {j.id for j in sched.scheduler.get_jobs()}
    assert remaining == {
        "morning_recovery_0",
        "morning_recovery_1",
        "morning_recovery_2",
        "gap_detection",
    }


def test_registered_jobs_are_never_removed(sched):
    """Guard against the inverse failure: retiring the live jobs."""
    sched.scheduler = _FakeScheduler(["morning_recovery_0", "morning_recovery_1"])
    sched._retire_stale_family(
        "morning_recovery", ["morning_recovery_0", "morning_recovery_1"]
    )
    sched._remove_disabled_jobs()
    assert sched.scheduler.removed == []


def test_reverting_to_single_fire_retires_the_indexed_jobs(sched):
    """The bug is symmetric — list -> single string must clean up too."""
    sched.scheduler = _FakeScheduler(
        ["morning_recovery", "morning_recovery_0", "morning_recovery_1"]
    )
    sched._retire_stale_family("morning_recovery", ["morning_recovery"])
    sched._remove_disabled_jobs()
    assert sorted(sched.scheduler.removed) == [
        "morning_recovery_0",
        "morning_recovery_1",
    ]


def test_other_families_are_untouched(sched):
    """A prefix must not reach jobs outside it."""
    sched.scheduler = _FakeScheduler(
        [
            "morning_recovery_0",
            "morning_recovery_extra",
            "morning_glory",
            "backfill_15s",
        ]
    )
    sched._retire_stale_family("morning_recovery", ["morning_recovery_0"])
    sched._remove_disabled_jobs()
    assert sched.scheduler.removed == ["morning_recovery_extra"]
    assert "morning_glory" in {j.id for j in sched.scheduler.get_jobs()}
    assert "backfill_15s" in {j.id for j in sched.scheduler.get_jobs()}


def test_no_keep_set_declared_is_a_no_op(sched):
    """A registrar that never calls _retire_stale_family changes nothing."""
    sched.scheduler = _FakeScheduler(["morning_recovery", "morning_recovery_0"])
    sched._remove_disabled_jobs()
    assert sched.scheduler.removed == []


def test_disabled_removal_still_works_alongside(sched):
    """_mark_disabled and the new sweep must coexist in one pass."""
    sched.scheduler = _FakeScheduler(
        ["morning_recovery", "morning_recovery_0", "archive_sync"]
    )
    sched._retire_stale_family("morning_recovery", ["morning_recovery_0"])
    sched._mark_disabled("Archive sync", "archive_sync")
    sched._remove_disabled_jobs()
    assert sorted(sched.scheduler.removed) == ["archive_sync", "morning_recovery"]


def test_vanished_job_does_not_raise(sched):
    """A one-shot completing between listing and removal must be survived."""

    class _Racy(_FakeScheduler):
        def get_jobs(self):
            return [_Job("morning_recovery"), _Job("morning_recovery_0")]

    sched.scheduler = _Racy([])  # nothing actually present to remove
    sched._retire_stale_family("morning_recovery", ["morning_recovery_0"])
    sched._remove_disabled_jobs()  # must not raise
    assert sched.scheduler.removed == []
