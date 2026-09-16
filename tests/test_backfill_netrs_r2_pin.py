"""A NetRS must land as RINEX 2 whichever path converts it.

The live download path has pinned NetRS to RINEX 2 since `3d6a3f7` — its
codeless L2 codes as C2D in RINEX 3 and GAMIT deletes it ("no P2 range").
The scheduler's BACKFILL job never did: `bulk_scheduler.py` constructs
`RINEXTask(..., rinex_version=3)` receiver-blind, and `RINEXTask._get_converter`
passed that straight through.

MEASURED on rek-d01 2026-09-16 by reading the header of every archived daily
RINEX (736,636 files) plus every hourly file of the 25 NetRS stations:

    34,382 hourly  + 1,472 daily  archived NetRS files are RINEX 3 with C2D

and the newest of them was written that morning at 06:46 by
`receivers.scheduler.backfill` — so this was still accruing, not historical.
`be6fd7c` unified which converter CLASS the two paths pick and left the VERSION
decision duplicated; `resolve_trimble_rinex_version` is the missing half.

What these tests pin, in order of how badly each would hurt:

* **A NetRS is forced to RINEX 2 even when the caller asks for 3** — the fix.
* **A NetR5 is NOT.** A NetR5 is multi-GNSS and codes L2 as C2W/C2X; pinning it
  to RINEX 2 would DISCARD its GLONASS. Verified against the archive: 40/40
  sampled R3 NetR5 files carry an `R` block. This boundary is the one that
  would silently destroy data if someone "simplified" the rule to all Trimbles.
* **Everything else passes through untouched** — a PolaRX5 or NetR9 asking for
  3 gets 3.
* **The config knob still governs both paths**, so there is one place to change
  the policy rather than two that can drift again.
"""

from __future__ import annotations

import pytest

from receivers.rinex.converter_select import resolve_trimble_rinex_version


class TestTheCodelessL2Pin:
    @pytest.mark.parametrize(
        "receiver_type",
        ["netrs", "NetRS", "NETRS", "trimble netrs", "TRIMBLE NETRS"],
    )
    def test_netrs_is_forced_to_rinex_2(self, receiver_type):
        assert resolve_trimble_rinex_version(3, receiver_type=receiver_type) == 2

    def test_a_netrs_request_for_2_stays_2(self):
        assert resolve_trimble_rinex_version(2, receiver_type="netrs") == 2


class TestNetR5IsNotPinned:
    """The boundary that would destroy GLONASS if the rule were widened."""

    @pytest.mark.parametrize("receiver_type", ["netr5", "NetR5", "TRIMBLE NETR5"])
    def test_netr5_keeps_the_requested_version(self, receiver_type):
        assert resolve_trimble_rinex_version(3, receiver_type=receiver_type) == 3


class TestEverythingElsePassesThrough:
    @pytest.mark.parametrize(
        "receiver_type", ["netr9", "NetR9", "polarx5", "mosaic-x5", "g10", "leica"]
    )
    def test_other_receivers_are_untouched(self, receiver_type):
        assert resolve_trimble_rinex_version(3, receiver_type=receiver_type) == 3

    @pytest.mark.parametrize("receiver_type", [None, "", "unknown"])
    def test_absent_or_unknown_type_is_untouched(self, receiver_type):
        assert resolve_trimble_rinex_version(3, receiver_type=receiver_type) == 3


class TestTheConfigKnobGovernsBothPaths:
    def test_netrs_rinex_version_is_honoured(self):
        assert (
            resolve_trimble_rinex_version(
                3, receiver_type="netrs", rinex_config={"netrs_rinex_version": 3}
            )
            == 3
        )

    def test_a_garbage_config_value_falls_back_to_2(self):
        """Never let a bad config value silently re-open the C2D path."""
        for bad in ("", "three", None, [], {}):
            assert (
                resolve_trimble_rinex_version(
                    3, receiver_type="netrs", rinex_config={"netrs_rinex_version": bad}
                )
                == 2
            )


class TestTheLivePathStillResolvesTheSameWay:
    """Both callers must go through the resolver — that is the whole point."""

    @pytest.mark.parametrize("session_type", ["15s_24hr", "1Hz_1hr", "status_1hr"])
    def test_live_selector_pins_netrs_for_every_session(self, session_type):
        import logging

        from receivers.rinex.async_converter import _create_converter
        from receivers.rinex.converter_base import NamingConvention, RinexVersion

        converter, _ext = _create_converter(
            "SNAE", "netrs", {}, logging.getLogger("test.pin"), session_type
        )
        assert converter.rinex_version is RinexVersion.RINEX_2
        assert converter.naming_convention is NamingConvention.SHORT

    def test_live_selector_leaves_netr9_at_3(self):
        import logging

        from receivers.rinex.async_converter import _create_converter
        from receivers.rinex.converter_base import RinexVersion

        converter, _ext = _create_converter(
            "MANA", "netr9", {}, logging.getLogger("test.pin"), "15s_24hr"
        )
        assert converter.rinex_version is RinexVersion.RINEX_3


class TestTheBackfillTaskUsesTheResolver:
    def _task(self, rinex_version=3):
        from receivers.scheduling.task_interface import (
            TaskConfig,
            TaskFrequency,
            TaskType,
        )
        from receivers.scheduling.tasks.rinex_task import RINEXTask

        cfg = TaskConfig(
            task_type=TaskType.RINEX,
            session_type="1Hz_1hr",
            schedule_minute=0,
            distribution_window=0,
            frequency=TaskFrequency.HOURLY,
        )
        return RINEXTask(
            station_id="SNAE",
            config=cfg,
            input_files=[],
            output_dir="/tmp",
            rinex_version=rinex_version,
        )

    def test_backfill_converts_a_netrs_at_rinex_2(self, monkeypatch):
        from receivers.rinex.converter_base import NamingConvention, RinexVersion

        task = self._task(rinex_version=3)
        monkeypatch.setattr(task, "_rinex_config", lambda: {})
        conv = task._get_converter({"receiver_type": "NetRS"})
        assert conv is not None
        assert conv.rinex_version is RinexVersion.RINEX_2
        assert conv.naming_convention is NamingConvention.SHORT

    def test_short_naming_survives_a_long_default_in_config(self, monkeypatch):
        """The explicit SHORT is load-bearing, not decoration.

        `ConverterBase` resolves naming as explicit > CONFIG > version-based.
        So with `default_naming: long` in config and no explicit convention, a
        RINEX 2 NetRS would be written under a LONG name — the exact inverse of
        the bug that started this (R3 content wearing an R2 name), and just as
        invisible. The live path forces SHORT for this reason; so must backfill.

        Reaching this needed a config stub: without one the mutation that drops
        the explicit SHORT was NOT DETECTED, because the version-based fallback
        happens to give SHORT too.
        """
        from receivers.rinex.converter_base import NamingConvention, RinexVersion

        task = self._task(rinex_version=3)
        monkeypatch.setattr(task, "_rinex_config", lambda: {})

        class _Cfg:
            def get_rinex_config(self):
                return {"default_naming": "long"}

            def __getattr__(self, name):  # anything else is not this test's business
                raise AttributeError(name)

        monkeypatch.setattr(
            "receivers.rinex.converter_base.get_receivers_config", lambda: _Cfg()
        )
        conv = task._get_converter({"receiver_type": "NetRS"})
        assert conv is not None
        assert conv.rinex_version is RinexVersion.RINEX_2
        assert conv.naming_convention is NamingConvention.SHORT

    def test_backfill_leaves_a_netr5_at_rinex_3(self, monkeypatch):
        from receivers.rinex.converter_base import RinexVersion

        task = self._task(rinex_version=3)
        monkeypatch.setattr(task, "_rinex_config", lambda: {})
        conv = task._get_converter({"receiver_type": "NetR5"})
        assert conv is not None
        assert conv.rinex_version is RinexVersion.RINEX_3

    def test_an_unreadable_config_does_not_raise(self, monkeypatch):
        """A config problem must not turn a backfill conversion into a crash."""
        from receivers.rinex.converter_base import RinexVersion

        task = self._task(rinex_version=3)

        def boom():
            raise RuntimeError("config gone")

        monkeypatch.setattr(
            "receivers.config.receivers_config.get_receivers_config", boom
        )
        conv = task._get_converter({"receiver_type": "NetRS"})
        assert conv is not None
        assert conv.rinex_version is RinexVersion.RINEX_2
