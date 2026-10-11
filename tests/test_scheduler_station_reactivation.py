"""Regression tests for the 2026-10-10 'HAMR reactivation needed a restart' bug.

Per-station health/download jobs (health_<SID>, <session>_<SID>) are normally
only built once, in schedule_all_sessions()/_schedule_health_monitoring(), at
scheduler startup. A station that was inactive/discontinued/passive at the
last startup got no jobs at all (correctly), but flipping it active via a
stations.cfg edit previously updated self.stations and the DB (via
_sync_station_status_to_db) without ever registering the now-eligible
station's jobs — it silently sat unpolled until the whole process restarted.

_resync_station_jobs() + its wiring into _check_config_changes() fix this
without replaying the unsafe parts of schedule_all_sessions() (specifically
_schedule_daily_catchup, which would otherwise fire an immediate one-shot
download for the ENTIRE eligible fleet of every daily session on every
unrelated cfg edit).
"""

import logging
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

# `bs` is bound up front to keep it DEFINITELY bound for type checkers. The
# try/except only exists so an environment without APScheduler skips this file
# (via the pytestmark below) instead of erroring at collection — but the bare
# `from ... import ... as bs` in a try block leaves `bs` 'possibly unbound' to
# a checker, which then flags every use site. Typed Any: when the import fails
# every test here is skipped, so the `bs.<attr>` uses are never evaluated.
bs: Any = None
try:
    from receivers.scheduling import bulk_scheduler as _bs  # type: ignore[import-untyped]

    bs = _bs
    SCHEDULER_AVAILABLE = bs.HAS_APSCHEDULER
except ImportError:  # pragma: no cover — apscheduler is a hard dep of receivers
    SCHEDULER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SCHEDULER_AVAILABLE, reason="APScheduler not installed"
)


def _station(**overrides):
    base = {
        "enabled": True,
        "receiver_type": "polarx5",
        "station_status": None,
        "health_check": None,
    }
    base.update(overrides)
    return base


def _schedule_configs():
    return {
        "15s_24hr": bs.ScheduleConfig(
            session_type="15s_24hr",
            distribution_window=10,
            schedule="00:10",
        ),
        "1Hz_1hr": bs.ScheduleConfig(
            session_type="1Hz_1hr",
            distribution_window=5,
            schedule=":15",
            midnight_offset=10,
        ),
    }


def _mock_scheduler(sched) -> MagicMock:
    """The MagicMock standing in for ``sched.scheduler`` (installed by _stub).

    Cast is needed because ``BulkDownloadScheduler.scheduler`` is annotated
    with the real APScheduler type: pyright therefore reads ``.add_job`` /
    ``.get_jobs`` / ``.remove_job`` as BOUND METHODS of that class rather than
    as mock attributes, so ``.call_args_list`` is 'unknown on MethodType'.
    Casting to MagicMock once, here, keeps every call site clean and states
    the intent in one place.
    """
    return cast(MagicMock, sched.scheduler)


def _stub(stations_cfg):
    sched = bs.BulkDownloadScheduler.__new__(bs.BulkDownloadScheduler)
    sched.logger = logging.getLogger("receivers.test")
    sched._disabled_jobs = []
    sched.scheduler = MagicMock()
    sched.scheduler_types = {"health": True, "15s_24hr": True, "1Hz_1hr": True}
    sched.yaml_config = {
        "status_monitoring": {
            "enabled": True,
            "schedule": "5m",
            "distribution_window": 3,
        }
    }
    sched.stations = stations_cfg
    sched.station_filter = None
    sched.max_stations_per_session = None
    sched.receiver_sessions = {}
    sched.production_mode = False
    sched.schedule_configs = _schedule_configs()
    return sched


@pytest.mark.unit
@pytest.mark.scheduler
class TestResyncStationJobs:
    """_resync_station_jobs registers/drops jobs for stations passed to it."""

    def test_newly_active_station_gets_health_and_download_jobs(self):
        sched = _stub(
            {
                "HAMR": _station(station_status=None),  # just flipped active
                "AKUR": _station(),  # unaffected, already active
            }
        )

        sched._resync_station_jobs({"HAMR"})

        added = {c.kwargs.get("id") for c in _mock_scheduler(sched).add_job.call_args_list}
        assert "health_HAMR" in added
        assert "15s_24hr_HAMR" in added
        assert "1Hz_1hr_HAMR" in added
        assert "1Hz_1hr_midnight_HAMR" in added  # midnight_offset > 0

    def test_unaffected_station_not_touched(self):
        """Download jobs are scoped to the station ids passed in — health
        monitoring legitimately re-touches the whole fleet (see
        test_does_not_trigger_fleet_wide_catchup_or_bootstrap for why that's
        safe; unlike downloads it has no fleet-wide side effect to avoid)."""
        sched = _stub(
            {
                "HAMR": _station(),
                "AKUR": _station(),
            }
        )

        sched._resync_station_jobs({"HAMR"})

        added_ids = {
            c.kwargs.get("id") for c in _mock_scheduler(sched).add_job.call_args_list
        }
        assert not any(
            job_id.startswith(("15s_24hr_AKUR", "1Hz_1hr_AKUR")) for job_id in added_ids
        )

    def test_newly_inactive_station_queued_for_removal(self):
        """A station transitioning to inactive is not re-added, and its
        persisted jobs are queued (via _disabled_jobs) for removal."""
        sched = _stub(
            {
                "HAMR": _station(station_status="inactive"),
            }
        )

        sched._resync_station_jobs({"HAMR"})

        added_ids = {
            c.kwargs.get("id") for c in _mock_scheduler(sched).add_job.call_args_list
        }
        assert "health_HAMR" not in added_ids
        assert "15s_24hr_HAMR" not in added_ids
        assert "health_HAMR" in sched._disabled_jobs
        assert "15s_24hr_HAMR" in sched._disabled_jobs

    def test_does_not_trigger_fleet_wide_catchup_or_bootstrap(self):
        """The whole reason this isn't just 'call schedule_all_sessions()
        again': that method also fires _schedule_daily_catchup (an immediate
        one-shot download for every eligible station) and _schedule_bootstrap.
        Neither must run from a targeted resync.
        """
        sched = _stub({"HAMR": _station()})
        sched._schedule_daily_catchup = MagicMock(
            side_effect=AssertionError("must not run from _resync_station_jobs")
        )
        sched._schedule_bootstrap = MagicMock(
            side_effect=AssertionError("must not run from _resync_station_jobs")
        )

        sched._resync_station_jobs({"HAMR"})  # must not raise

    def test_removal_actually_flushed_not_just_queued(self):
        """_resync_station_jobs must call _remove_disabled_jobs() itself —
        nothing else drains the queue mid-run."""
        sched = _stub({"HAMR": _station(station_status="discontinued")})
        _mock_scheduler(sched).get_jobs.return_value = [
            MagicMock(id="health_HAMR"),
            MagicMock(id="15s_24hr_HAMR"),
        ]

        sched._resync_station_jobs({"HAMR"})

        removed = {
            c.args[0] for c in _mock_scheduler(sched).remove_job.call_args_list
        }
        assert "health_HAMR" in removed
        assert "15s_24hr_HAMR" in removed


@pytest.mark.unit
@pytest.mark.scheduler
class TestCheckConfigChangesWiring:
    """_check_config_changes calls _resync_station_jobs for affected stations
    only, and never lets a resync failure kill the watcher."""

    def _stub_with_cfg_watcher_deps(self, old_stations, new_stations, tmp_path):
        sched = _stub(new_stations)
        sched._config_mtime = 100.0
        sched._load_station_configs = MagicMock(return_value=new_stations)
        sched._sync_station_status_to_db = MagicMock()
        sched._refresh_stream_capture_on_config_change = MagicMock()
        sched.yaml_config["stream_capture"] = {"enabled": False}
        sched.stations = old_stations

        cfg_file = tmp_path / "stations.cfg"
        cfg_file.write_text("[HAMR]\n")
        import os

        os.utime(cfg_file, (200.0, 200.0))
        sched._get_stations_cfg_path = MagicMock(return_value=cfg_file)
        return sched

    def test_status_flip_triggers_resync_for_that_station_only(self, tmp_path):
        old = {"HAMR": _station(station_status="inactive"), "AKUR": _station()}
        new = {"HAMR": _station(station_status=None), "AKUR": _station()}
        sched = self._stub_with_cfg_watcher_deps(old, new, tmp_path)
        sched._resync_station_jobs = MagicMock()

        sched._check_config_changes()

        sched._resync_station_jobs.assert_called_once_with({"HAMR"})

    def test_new_station_section_triggers_resync(self, tmp_path):
        old = {"AKUR": _station()}
        new = {"AKUR": _station(), "NEWC": _station()}
        sched = self._stub_with_cfg_watcher_deps(old, new, tmp_path)
        sched._resync_station_jobs = MagicMock()

        sched._check_config_changes()

        sched._resync_station_jobs.assert_called_once_with({"NEWC"})

    def test_no_eligibility_change_does_not_resync(self, tmp_path):
        """A cfg edit unrelated to lifecycle (e.g. antenna_height) must not
        trigger a resync — only status_status/health_check/enabled flips do."""
        old = {"AKUR": _station(enabled=True)}
        new = {"AKUR": _station(enabled=True)}  # identical lifecycle fields
        sched = self._stub_with_cfg_watcher_deps(old, new, tmp_path)
        sched._resync_station_jobs = MagicMock()

        sched._check_config_changes()

        sched._resync_station_jobs.assert_not_called()

    def test_resync_failure_does_not_crash_the_watcher(self, tmp_path, caplog):
        old = {"HAMR": _station(station_status="inactive")}
        new = {"HAMR": _station(station_status=None)}
        sched = self._stub_with_cfg_watcher_deps(old, new, tmp_path)
        sched._resync_station_jobs = MagicMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR, logger="receivers.test"):
            sched._check_config_changes()  # must not raise

        assert any("Failed to resync jobs" in r.message for r in caplog.records)
