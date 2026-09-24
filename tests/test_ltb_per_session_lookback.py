"""Per-session lookback for the long-term backfill.

One `lookback_days` cannot serve both sessions. MEASURED on rek-d01 2026-09-24
from `receiver_horizon`: for `15s_24hr` 140 of 149 stations still hold MORE than
90 days, so a 30-day window leaves recoverable data on the receiver; for
`1Hz_1hr` 105 of 149 hold 30 days or LESS (52 of them 7 or less), so the same 30
mostly buys requests for data that is already gone. The 04:00 pass that day:
217/600 slots for recovered=14, nothing_on_receiver=195.

The deployed key is `lookback_days`, not `days_back`, so `Lookback.from_config`
accepts it via `legacy_days_key` — the deployed config keeps working untouched
and gains the mapping form.
"""

import pytest

from receivers.scheduling.long_term_backfill import _describe_lookback, _lookback_for
from receivers.scheduling.lookback import Lookback, LookbackConfigError

SECTION = "long_term_backfill"


def _from(section):
    return Lookback.from_config(
        section, default_days=365, section_name=SECTION, legacy_days_key="lookback_days"
    )


class TestDeployedConfigKeepsWorking:
    """The whole point of legacy_days_key: change nothing, break nothing."""

    def test_plain_int_is_unchanged(self):
        lb = _from({"lookback_days": 30})
        assert lb.count == 30 and lb.unit == "days"
        # identical for every session — exactly the old flat behaviour
        assert lb.count_for("15s_24hr") == 30
        assert lb.count_for("1Hz_1hr") == 30

    def test_absent_key_falls_back_to_the_callers_default(self):
        lb = _from({})
        assert lb.count == 365 and lb.unit == "days"

    def test_legacy_key_is_calendar_days_not_files(self):
        """`lookback_days` means DAYS on an hourly session too — the LTB queues
        days for both sessions ('23/23 day(s) to recover' on 1Hz)."""
        assert _from({"lookback_days": 30}).unit == "days"


class TestPerSessionMapping:
    def test_the_measured_shape(self):
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        assert lb.count_for("15s_24hr") == 90, "daily: 140/149 stations hold >90d"
        assert lb.count_for("1Hz_1hr") == 30, "1Hz: 105/149 hold <=30d"
        assert lb.count_for("status_1hr") == 30, "unlisted sessions take the default"

    def test_mapping_without_default_is_refused(self):
        with pytest.raises(LookbackConfigError, match="no 'default'"):
            _from({"lookback_days": {"15s_24hr": 90}})

    def test_overrides_stay_hashable_for_the_jobstore(self):
        """These instances are pickled into the APScheduler jobstore."""
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        assert isinstance(lb.overrides, tuple)
        hash(lb)


class TestAmbiguityIsAHardError:
    """A silent precedence rule is the bug class this module exists to remove."""

    def test_legacy_plus_days_back(self):
        with pytest.raises(LookbackConfigError, match="not both"):
            _from({"lookback_days": 30, "days_back": 7})

    def test_legacy_plus_files_back(self):
        with pytest.raises(LookbackConfigError, match="not both"):
            _from({"lookback_days": 30, "files_back": 36})

    def test_legacy_key_is_not_a_global_alias(self):
        """Only the section that already ships the key opts in. Without
        legacy_days_key, `lookback_days` is just an unknown key."""
        lb = Lookback.from_config({"lookback_days": 30}, default_days=365)
        assert lb.count == 365, "must NOT be picked up as a lookback"


class TestResolution:
    def test_int_passes_through(self):
        assert _lookback_for(30, "15s_24hr") == 30
        assert _lookback_for(30, "1Hz_1hr") == 30

    def test_lookback_resolves_per_session(self):
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        assert _lookback_for(lb, "15s_24hr") == 90
        assert _lookback_for(lb, "1Hz_1hr") == 30

    def test_returns_an_int_not_a_lookback(self):
        """query_long_term_gaps does `end - timedelta(days=lookback_days - 1)`.
        A Lookback reaching it is a TypeError, so resolution must happen at the
        job seam and hand an int downwards."""
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        for session in ("15s_24hr", "1Hz_1hr"):
            got = _lookback_for(lb, session)
            assert isinstance(got, int)
            from datetime import timedelta

            timedelta(days=got - 1)  # the arithmetic the inner function does

    def test_banner_shows_the_effective_window_per_session(self):
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        desc = _describe_lookback(lb, ["15s_24hr", "1Hz_1hr"])
        assert "15s_24hr=90d" in desc and "1Hz_1hr=30d" in desc

    def test_banner_handles_a_plain_int(self):
        assert _describe_lookback(30, ["15s_24hr"]) == "15s_24hr=30d"


# ---------------------------------------------------------------------------
# Review 2026-09-24: the helper above was pinned, its USE was not. Mutating the
# daily job's `_one()` (M6), the reconnection trigger (M7) or the registration
# site in bulk_scheduler (M8) to pass the Lookback straight through left every
# test above green. These pin the production path, not the helper.
# ---------------------------------------------------------------------------

import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from receivers.scheduling.long_term_backfill import (
    _reset_attempt_history,
    _reset_rotation,
    _run_long_term_backfill_job,
    _run_reconnection_backfill_job,
)

LTB = "receivers.scheduling.long_term_backfill"


def _report(queued=(), skipped_offline=False):
    return SimpleNamespace(queued=list(queued), skipped_offline=skipped_offline)


class TestFilesUnitIsRefused:
    """`from_config` with legacy_days_key still accepts `files_back`, and the
    LTB counts calendar days end to end — so a files-unit Lookback reaching the
    job would be silently misread as DAYS (36 files -> 36 days on 1Hz, 24x)."""

    def test_files_back_is_refused_not_misread_as_days(self):
        lb = Lookback.from_config(
            {"files_back": 36},
            default_days=365,
            section_name=SECTION,
            legacy_days_key="lookback_days",
        )
        assert lb.unit == "files"  # from_config lets it through; the guard is here
        with pytest.raises(LookbackConfigError, match="calendar days"):
            _lookback_for(lb, "1Hz_1hr")
        with pytest.raises(LookbackConfigError):
            _describe_lookback(lb, ["1Hz_1hr"])  # the run banner fails first


class TestTheJobSeam:
    def setup_method(self):
        _reset_rotation()
        _reset_attempt_history()

    def test_daily_job_hands_each_session_its_own_int(self):
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        station = MagicMock(return_value=_report())
        with (
            patch(
                "receivers.cli.main.get_all_station_configs",
                return_value={"AAAA": {}},
            ),
            patch(f"{LTB}.run_long_term_backfill_station", station),
        ):
            _run_long_term_backfill_job(
                sessions=["15s_24hr", "1Hz_1hr"], lookback_days=lb, max_workers=1
            )
        got = {c.args[1]: c.kwargs["lookback_days"] for c in station.call_args_list}
        assert got == {"15s_24hr": 90, "1Hz_1hr": 30}
        assert all(type(v) is int for v in got.values()), got

    def test_reconnection_job_resolves_the_daily_session(self):
        lb = _from({"lookback_days": {"default": 30, "15s_24hr": 90}})
        cm = MagicMock()
        (
            cm.__enter__.return_value.cursor.return_value.__enter__.return_value
        ).fetchall.return_value = [("AAAA",)]
        old_gap = SimpleNamespace(file_date=date(2000, 1, 1))  # older than floor
        query = MagicMock(return_value=_report(queued=[old_gap]))
        station = MagicMock(return_value=_report())
        with (
            patch(
                "receivers.health.database_factory.DatabaseConnectionFactory.connection",
                return_value=cm,
            ),
            patch(f"{LTB}.query_long_term_gaps", query),
            patch(f"{LTB}.run_long_term_backfill_station", station),
        ):
            _run_reconnection_backfill_job(lookback_days=lb)
        assert query.call_args.kwargs["lookback_days"] == 90
        assert station.call_args.kwargs["lookback_days"] == 90
        assert type(station.call_args.kwargs["lookback_days"]) is int


class TestRegistration:
    """bulk_scheduler must REGISTER the jobs with the resolved Lookback, and a
    bad section must fail at scheduling time, before any job is added."""

    def _stub(self, section):
        bs = pytest.importorskip("receivers.scheduling.bulk_scheduler")
        if not bs.HAS_APSCHEDULER:
            pytest.skip("APScheduler not installed")
        sched = bs.BulkDownloadScheduler.__new__(bs.BulkDownloadScheduler)
        sched.logger = logging.getLogger("receivers.test")
        sched.yaml_config = {"long_term_backfill": {"enabled": True, **section}}
        sched.scheduler = MagicMock()
        sched._disabled_jobs = []
        return sched

    def _registered(self, sched):
        return {
            c.kwargs["id"]: c.kwargs["kwargs"]["lookback_days"]
            for c in sched.scheduler.add_job.call_args_list
        }

    def test_both_jobs_carry_the_per_session_lookback(self):
        sched = self._stub({"lookback_days": {"default": 30, "15s_24hr": 90}})
        sched._schedule_long_term_backfill()
        sched._schedule_reconnection_backfill()
        by_id = self._registered(sched)
        assert set(by_id) == {"long_term_backfill", "reconnection_backfill"}
        for lb in by_id.values():
            assert isinstance(lb, Lookback), lb
            assert lb.count_for("15s_24hr") == 90
            assert lb.count_for("1Hz_1hr") == 30

    def test_the_deployed_plain_int_registers_as_a_flat_lookback(self):
        sched = self._stub({"lookback_days": 30})
        sched._schedule_long_term_backfill()
        (lb,) = self._registered(sched).values()
        assert lb == Lookback(count=30, unit="days")

    def test_an_ambiguous_section_fails_before_any_job_is_added(self):
        sched = self._stub({"lookback_days": 30, "files_back": 36})
        with pytest.raises(LookbackConfigError, match="not both"):
            sched._schedule_long_term_backfill()
        sched.scheduler.add_job.assert_not_called()
