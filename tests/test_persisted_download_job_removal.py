"""A deactivated station must stop downloading — not just stop being registered.

The SQLite jobstore is authoritative across restarts, so skipping a station at
registration time does nothing about the `{session}_{SID}` job a previous run
already persisted: it reloads and keeps firing.

MEASURED on HLFJ. `station_status = inactive` since 2026-08-27, yet a
`1Hz_1hr_HLFJ` download ran at 23:02:06 on 2026-09-15 — half an hour before the
station was re-activated. Four weeks of a station that operations believed was
switched off still dialling out every hour.

The HEALTH path has guarded this since the PolaRX5 lockout incident
(`_schedule_status_monitoring` queues `health_<SID>` for skipped stations),
precisely because a stray TCP login against a pre-5.7 receiver feeds a
brute-force lockout. Downloads had no equivalent. Same bug class as the 2026-08
"archive_sync disabled but still pushing".

What these tests pin, in order of how badly each would hurt:

* **A lifecycle-excluded station's persisted download jobs are queued for
  removal** — the fix itself, covering all three variants (regular, midnight,
  catch-up).
* **A station skipped by a DEV FILTER or a cap is NOT swept.** `station_filter`
  is a laptop knob and `max_stations_per_session` is a throttle; both mean "not
  this run", not "not wanted". Sweeping them would delete jobs the next
  unfiltered start expects to still be there — a far worse bug than the one
  being fixed.
* **An active station is never touched.**
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def sched():
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    obj = BulkDownloadScheduler.__new__(BulkDownloadScheduler)
    obj.logger = logging.getLogger("test.persisted_dl")
    obj._disabled_jobs = []
    obj.station_filter = None
    obj.receiver_sessions = {}
    obj.max_stations_per_session = None
    return obj


def _cfg(**kw):
    base = {"enabled": True, "receiver_type": "polarx5"}
    base.update(kw)
    return base


ACTIVE = _cfg()


class TestLifecycleExclusionSweepsTheJobs:
    def test_inactive_station_jobs_are_queued(self, sched):
        """The HLFJ shape."""
        sched.stations = {"HLFJ": _cfg(station_status="inactive"), "THOB": ACTIVE}
        got = sched._get_stations_for_session("1Hz_1hr")
        assert got == ["THOB"]
        assert "1Hz_1hr_HLFJ" in sched._disabled_jobs

    def test_all_three_job_variants_are_covered(self, sched):
        """A station has a regular, a midnight and a catch-up job id."""
        sched.stations = {"HLFJ": _cfg(station_status="inactive")}
        sched._get_stations_for_session("1Hz_1hr")
        for jid in (
            "1Hz_1hr_HLFJ",
            "1Hz_1hr_midnight_HLFJ",
            "catchup_1Hz_1hr_HLFJ",
        ):
            assert jid in sched._disabled_jobs, jid

    def test_discontinued_is_swept(self, sched):
        sched.stations = {"OLD1": _cfg(station_status="discontinued")}
        sched._get_stations_for_session("15s_24hr")
        assert "15s_24hr_OLD1" in sched._disabled_jobs

    def test_passive_health_check_is_swept(self, sched):
        """Data arrives externally; this scheduler must not dial the receiver."""
        sched.stations = {"GRVM": _cfg(health_check="passive")}
        sched._get_stations_for_session("15s_24hr")
        assert "15s_24hr_GRVM" in sched._disabled_jobs

    def test_disabled_station_is_swept(self, sched):
        sched.stations = {"OFF1": _cfg(enabled=False)}
        sched._get_stations_for_session("15s_24hr")
        assert "15s_24hr_OFF1" in sched._disabled_jobs

    def test_the_session_is_scoped(self, sched):
        """Only the session being registered — not every session's jobs."""
        sched.stations = {"HLFJ": _cfg(station_status="inactive")}
        sched._get_stations_for_session("1Hz_1hr")
        assert not any("15s_24hr" in j for j in sched._disabled_jobs)


class TestNonLifecycleSkipsAreLeftAlone:
    """These mean "not this run", not "not wanted". Sweeping them would delete
    jobs the next unfiltered start expects to keep — worse than the bug."""

    def test_a_dev_station_filter_does_not_sweep(self, sched):
        sched.stations = {"THOB": ACTIVE, "ELDC": ACTIVE}
        sched.station_filter = ["THOB"]
        got = sched._get_stations_for_session("1Hz_1hr")
        assert got == ["THOB"]
        assert sched._disabled_jobs == [], "a laptop --stations run wiped production jobs"

    def test_an_unsupported_receiver_type_does_not_sweep(self, sched):
        sched.stations = {"BLEI": _cfg(receiver_type="netrs")}
        sched.receiver_sessions = {"netrs": ["15s_24hr"]}
        got = sched._get_stations_for_session("1Hz_1hr")
        assert got == []
        assert sched._disabled_jobs == []

    def test_the_max_stations_cap_does_not_sweep(self, sched):
        sched.stations = {s: ACTIVE for s in ("AAAA", "BBBB", "CCCC")}
        sched.max_stations_per_session = 1
        sched._get_stations_for_session("1Hz_1hr")
        assert sched._disabled_jobs == []


class TestActiveStationsAreUntouched:
    def test_nothing_is_queued_for_a_healthy_fleet(self, sched):
        sched.stations = {s: ACTIVE for s in ("THOB", "ELDC", "GONH")}
        got = sched._get_stations_for_session("1Hz_1hr")
        assert sorted(got) == ["ELDC", "GONH", "THOB"]
        assert sched._disabled_jobs == []

    def test_an_active_station_is_not_swept_alongside_an_inactive_one(self, sched):
        sched.stations = {"HLFJ": _cfg(station_status="inactive"), "THOB": ACTIVE}
        sched._get_stations_for_session("1Hz_1hr")
        assert not any("THOB" in j for j in sched._disabled_jobs)


class TestItSurvivesAPartialShell:
    """Registrars run on __new__-built shells in the suite, and _keep_job_family
    already documents this. A hard attribute reference here broke 10 tests."""

    def test_missing_disabled_jobs_attribute_is_tolerated(self, sched):
        del sched._disabled_jobs
        sched.stations = {"HLFJ": _cfg(station_status="inactive")}
        sched._get_stations_for_session("1Hz_1hr")
        assert "1Hz_1hr_HLFJ" in sched._disabled_jobs


class TestTheRemovalActuallyHappens:
    """End to end: queued ids must be dropped by _remove_disabled_jobs."""

    def test_queued_download_jobs_are_removed_post_start(self, sched):
        class _Job:
            def __init__(self, i):
                self.id = i

        class _FakeScheduler:
            def __init__(self, ids):
                self._jobs = {i: _Job(i) for i in ids}
                self.removed = []

            def get_jobs(self):
                return list(self._jobs.values())

            def remove_job(self, jid):
                from apscheduler.jobstores.base import JobLookupError

                if jid not in self._jobs:
                    raise JobLookupError(jid)
                del self._jobs[jid]
                self.removed.append(jid)

        sched.stations = {"HLFJ": _cfg(station_status="inactive"), "THOB": ACTIVE}
        sched._get_stations_for_session("1Hz_1hr")
        sched._job_family_keep = {}
        sched.scheduler = _FakeScheduler(["1Hz_1hr_HLFJ", "1Hz_1hr_THOB"])
        sched._remove_disabled_jobs()
        assert "1Hz_1hr_HLFJ" in sched.scheduler.removed
        assert "1Hz_1hr_THOB" not in sched.scheduler.removed
