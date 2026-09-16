"""A station DELETED from stations.cfg must stop firing its persisted jobs.

`d0a53d3` retires the jobs of a station that is still IN stations.cfg but
lifecycle-excluded. It cannot reach a station whose section was removed
outright: the registration loop walks `self.stations`, and a deleted station
is not there to be skipped.

MEASURED on rek-d01, 2026-09-16. `SFEH` has no section in stations.cfg and
still holds three persisted jobs — `15s_24hr_SFEH`, `status_1hr_SFEH` and
`health_SFEH`. The health job alone fires every 15 minutes against a station
that no longer exists.

What these tests pin, in order of how badly each would hurt:

* **No sections read, nothing swept.** An unreadable stations.cfg yields an
  empty section set; reading that as "every station was deleted" would retire
  the entire fleet's jobs in one start. This is the guard that matters most,
  because `_load_station_configs` genuinely falls back to `stations = {}` on
  any config exception — which is exactly why `self.stations` is NOT the
  oracle here.
* **A station still in stations.cfg is never touched**, whatever its lifecycle
  state. Deactivation is `d0a53d3`'s job and goes through a different path;
  this sweep must not double as a lifecycle sweep.
* **Only recognised per-station job families are swept.** `15s_24hr_batch_summary`
  and `archive_sync` must survive a sweep that is looking for `{session}_{SID}`.
* **The deleted station's jobs actually go** — the fix itself.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.id = job_id


class _FakeScheduler:
    def __init__(self, job_ids):
        self._jobs = list(job_ids)
        self.removed: list[str] = []

    def get_jobs(self):
        return [_FakeJob(j) for j in self._jobs]

    def remove_job(self, job_id):
        from apscheduler.jobstores.base import JobLookupError

        if job_id not in self._jobs:
            raise JobLookupError(job_id)
        self._jobs.remove(job_id)
        self.removed.append(job_id)


@pytest.fixture(autouse=True)
def _apscheduler_stub(monkeypatch):
    """`JobLookupError` only — importing APScheduler proper is not the point."""
    if "apscheduler.jobstores.base" in sys.modules:
        return
    base = types.ModuleType("apscheduler.jobstores.base")

    class JobLookupError(Exception):
        pass

    base.JobLookupError = JobLookupError
    monkeypatch.setitem(sys.modules, "apscheduler", types.ModuleType("apscheduler"))
    monkeypatch.setitem(
        sys.modules, "apscheduler.jobstores", types.ModuleType("apscheduler.jobstores")
    )
    monkeypatch.setitem(sys.modules, "apscheduler.jobstores.base", base)


def _sched(job_ids, cfg_sections, sessions=("15s_24hr", "1Hz_1hr", "status_1hr")):
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    obj = BulkDownloadScheduler.__new__(BulkDownloadScheduler)
    obj.logger = logging.getLogger("test.deleted_station_jobs")
    obj.scheduler = _FakeScheduler(job_ids)
    obj._cfg_all_sections = set(cfg_sections)
    obj.schedule_configs = {s: object() for s in sessions}
    return obj


# The production job store on 2026-09-16, reduced to one of each shape.
FLEET_JOBS = [
    "15s_24hr_THOB",
    "1Hz_1hr_THOB",
    "1Hz_1hr_midnight_THOB",
    "status_1hr_THOB",
    "health_THOB",
    "catchup_15s_24hr_THOB",
]
SFEH_JOBS = ["15s_24hr_SFEH", "status_1hr_SFEH", "health_SFEH"]
NON_STATION_JOBS = [
    "15s_24hr_batch_summary",
    "15s_24hr_second_chance",
    "archive_sync",
    "backfill_1Hz_1hr",
    "morning_recovery_0",
    "long_term_backfill",
]


class TestTheDeletedStationIsSwept:
    def test_all_three_sfeh_jobs_are_removed(self):
        sched = _sched(FLEET_JOBS + SFEH_JOBS, cfg_sections={"THOB"})
        sched._retire_deleted_station_jobs()
        assert sorted(sched.scheduler.removed) == sorted(SFEH_JOBS)

    def test_midnight_and_catchup_variants_are_reached(self):
        jobs = ["1Hz_1hr_midnight_SFEH", "catchup_15s_24hr_SFEH"]
        sched = _sched(jobs, cfg_sections={"THOB"})
        sched._retire_deleted_station_jobs()
        assert sorted(sched.scheduler.removed) == sorted(jobs)

    def test_it_reports_what_it_removed(self, caplog):
        sched = _sched(SFEH_JOBS, cfg_sections={"THOB"})
        with caplog.at_level(logging.INFO, logger="test.deleted_station_jobs"):
            sched._retire_deleted_station_jobs()
        assert "SFEH" in caplog.text
        assert "no such station in stations.cfg" in caplog.text


class TestAbsentEvidenceIsNotDeletion:
    """The guard that stops one bad config read from retiring the fleet."""

    def test_empty_section_set_sweeps_nothing(self):
        sched = _sched(FLEET_JOBS + SFEH_JOBS, cfg_sections=set())
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []

    def test_missing_attribute_sweeps_nothing(self):
        sched = _sched(FLEET_JOBS + SFEH_JOBS, cfg_sections={"THOB"})
        del sched._cfg_all_sections
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []


class TestAStationStillInCfgIsNeverTouched:
    def test_active_station_survives(self):
        sched = _sched(FLEET_JOBS, cfg_sections={"THOB"})
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []

    def test_lifecycle_excluded_station_survives_this_sweep(self):
        """Deactivation is d0a53d3's path; presence in cfg is all this one reads.

        If this sweep also retired inactive stations it would fire on every
        start for every inactive station, duplicating (and racing) the
        registration-time sweep that carries the lifecycle reasoning.
        """
        sched = _sched(["15s_24hr_HLFJ", "health_HLFJ"], cfg_sections={"HLFJ"})
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []


class TestOnlyStationJobFamiliesAreSwept:
    def test_non_station_jobs_survive(self):
        sched = _sched(NON_STATION_JOBS, cfg_sections={"THOB"})
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []

    def test_unknown_session_prefix_is_not_swept(self):
        """A job whose session this run does not know about is left alone.

        Better a visible orphan than a regex loose enough to eat a job that
        merely ends in four capitals.
        """
        sched = _sched(["30s_1hr_SFEH"], cfg_sections={"THOB"})
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == []

    def test_health_family_works_without_any_session_configs(self):
        sched = _sched(["health_SFEH"], cfg_sections={"THOB"}, sessions=())
        sched._retire_deleted_station_jobs()
        assert sched.scheduler.removed == ["health_SFEH"]


class TestRaces:
    def test_a_job_that_vanishes_mid_sweep_is_not_fatal(self):
        sched = _sched(SFEH_JOBS, cfg_sections={"THOB"})
        real_remove = sched.scheduler.remove_job

        def flaky(job_id):
            if job_id == "status_1hr_SFEH":
                from apscheduler.jobstores.base import JobLookupError

                raise JobLookupError(job_id)
            return real_remove(job_id)

        sched.scheduler.remove_job = flaky
        sched._retire_deleted_station_jobs()
        assert sorted(sched.scheduler.removed) == ["15s_24hr_SFEH", "health_SFEH"]
