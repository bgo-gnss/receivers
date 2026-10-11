"""Tests for `receivers cfg sync-from-tos` (TOS → stations.cfg device sync).

The command itself needs live TOS + cfg, so these cover the pure decision units
and the safety guard, which is where the correctness/risk lives.
"""

import argparse
from pathlib import Path

from receivers.cli.cfg import (
    _is_literal_ip,
    _plan_router_ip,
    _plan_station_status,
    _station_status_in_scope,
    _sync_overwrite_specs,
    cmd_cfg_sync_from_tos,
)


class TestOverwriteSpecs:
    def test_overwrites_device_identity_only(self):
        keys = {s.cfg_key for s in _sync_overwrite_specs()}
        # TOS-canonical-for-cfg fields are overwritten: device identity + DOMES
        assert keys == {
            "receiver_type",
            "receiver_serial",
            "receiver_firmware_version",
            "antenna_type",
            "antenna_serial",
            "antenna_radome",
            "station_name",
            "rinex_marker_number",
            # Operational scope — who runs the station (TOS-canonical; the
            # signal that supersedes the health_check=passive proxy). Added
            # 2026-10-11, once TOS coverage was complete (0 stations missing),
            # which is the precondition for enabling its sync at all.
            "operational_status",
        }

    def test_never_surveyed_position(self):
        keys = {s.cfg_key for s in _sync_overwrite_specs()}
        # stations.cfg / survey is ground truth — these must stay flag-only
        for k in ("latitude", "longitude", "height", "antenna_height"):
            assert k not in keys

    def test_domes_is_canonical_for_cfg_but_not_pushable_to_tos(self):
        """rinex_marker_number ← TOS iers_domes_number, but NEVER cfg→TOS.

        DOMES is IGS/TOS-canonical: sync-from-tos writes it into cfg, but it
        carries no tos_attribute_code so reconcile --push-tos can't send cfg's
        (often wrong 4-char) value up to TOS. This split is the fix for the
        NYLA cross-contamination.
        """
        from receivers.cfg import tos_adapter
        from receivers.cfg.field_manifest import fields_by_key

        spec = fields_by_key()["rinex_marker_number"]
        assert spec.sync_from_tos is True
        assert spec.tos_writable is False  # must not push cfg→TOS
        assert tos_adapter.iers_domes_number({"iers_domes_number": "10230M001"}) == (
            "10230M001"
        )
        # Missing/blank DOMES → None, so a silent TOS gap never clobbers cfg.
        assert tos_adapter.iers_domes_number({}) is None
        assert tos_adapter.iers_domes_number({"iers_domes_number": ""}) is None


class TestSyntheticSerialStrip:
    """TOS synthetic device serials must never leak into stations.cfg.

    Surfaced building sync-from-tos: RVIT's TOS antenna serial was the malformed
    no-dash synthetic `antenna-RVIT20150625`, which the old antenna-only,
    dash-required regex missed — so it would have been written to cfg.
    """

    def test_synthetic_variants_strip_to_none(self):
        from receivers.cfg.field_manifest import _strip_placeholder

        for v in (
            "antenna-RVIT20150625",  # malformed: no date dash (the bug)
            "antenna-AFST-20210527",  # canonical antenna synthetic
            "receiver-OLKE-20001017",  # receiver subtype synthetic
            "antenna-SEY9-20210325",
        ):
            assert _strip_placeholder(v) is None, v

    def test_real_serials_pass_through(self):
        from receivers.cfg.field_manifest import _strip_placeholder

        for v in ("5423R48810", "4435237690", "3070341", "0220368057", "26094"):
            assert _strip_placeholder(v) == v


class TestIsLiteralIp:
    def test_ips(self):
        assert _is_literal_ip("10.6.1.229")
        assert _is_literal_ip(" 130.208.224.220 ")

    def test_hostnames_and_junk(self):
        assert not _is_literal_ip("OLKE.gps.vedur.is")
        assert not _is_literal_ip("")
        assert not _is_literal_ip(None)
        assert not _is_literal_ip("10.6.1.229 # comment")


class TestPlanRouterIp:
    def test_no_sim_ip_is_noop(self):
        assert _plan_router_ip("10.0.0.1", None) is None

    def test_missing_cfg_fills(self):
        assert _plan_router_ip(None, "10.6.1.229") == ("set", "10.6.1.229")

    def test_literal_drift_overwrites(self):
        assert _plan_router_ip("10.4.1.250", "10.6.1.229") == ("set", "10.6.1.229")

    def test_literal_match_is_noop(self):
        assert _plan_router_ip("10.6.1.229", "10.6.1.229") is None

    def test_hostname_mismatch_flags_only(self):
        # a stale hostname is a DNS fix, NOT an auto cfg-clobber
        assert _plan_router_ip("OLKE.gps.vedur.is", "10.6.1.229") == (
            "flag",
            ("OLKE.gps.vedur.is", "10.6.1.229"),
        )


class TestStationStatusInScope:
    """health_check=passive / station_role=passive stations never get a
    lifecycle plan — they may legitimately have no TOS gnss_receiver at all
    (external/reference stations we don't operate hardware for)."""

    def test_operated_station_in_scope(self):
        assert _station_status_in_scope({"health_check": None, "station_role": None})

    def test_health_check_passive_out_of_scope(self):
        assert not _station_status_in_scope({"health_check": "passive"})

    def test_health_check_passive_case_insensitive(self):
        assert not _station_status_in_scope({"health_check": "PASSIVE"})

    def test_station_role_passive_out_of_scope(self):
        assert not _station_status_in_scope({"station_role": "passive"})


class TestPlanStationStatus:
    """Exhaustive (cfg_status x lifecycle x contradiction x scope) table.

    The directional-asymmetry rule is the one thing that must never regress:
    only ("clear", ...) — activation — is produced by the 'in sync' active
    cases; every deactivation path returns ("set", ...), which callers must
    treat as confirm-only regardless of --yes (enforced at the CLI layer,
    tested separately in TestStationStatusSync).
    """

    def test_out_of_scope_always_none(self):
        # Even a wild mismatch produces nothing when out of scope.
        assert _plan_station_status("inactive", "active", False, in_scope=False) is None
        assert _plan_station_status(None, "discontinued", False, in_scope=False) is None

    def test_contradiction_always_flags_regardless_of_cfg_value(self):
        # Contradictory TOS evidence is never auto-resolved, whatever cfg says.
        for cfg_status in (None, "active", "inactive", "discontinued"):
            plan = _plan_station_status(cfg_status, "discontinued", True, in_scope=True)
            assert plan is not None
            assert plan[0] == "flag"
            assert "hygiene" in plan[1]

    def test_suppressed_cfg_value_never_touched(self):
        # Scheduler-owned DB concept; not this planner's to overwrite.
        assert (
            _plan_station_status("suppressed", "active", False, in_scope=True) is None
        )
        assert (
            _plan_station_status("suppressed", "inactive", False, in_scope=True) is None
        )

    def test_discontinued_lifecycle_already_discontinued_is_noop(self):
        assert (
            _plan_station_status("discontinued", "discontinued", False, in_scope=True)
            is None
        )

    def test_discontinued_lifecycle_sets_from_any_other_cfg_value(self):
        for cfg_status in (None, "active", "inactive"):
            assert _plan_station_status(
                cfg_status, "discontinued", False, in_scope=True
            ) == ("set", "discontinued")

    def test_active_lifecycle_clears_stale_inactive(self):
        # THE fix: activation is the only auto-apply-safe direction.
        assert _plan_station_status("inactive", "active", False, in_scope=True) == (
            "clear",
            None,
        )

    def test_active_lifecycle_already_active_is_noop(self):
        assert _plan_station_status(None, "active", False, in_scope=True) is None
        assert _plan_station_status("active", "active", False, in_scope=True) is None

    def test_active_lifecycle_against_cfg_discontinued_flags_as_resurrection(self):
        # Never silently un-discontinue a station — needs a human decision.
        plan = _plan_station_status("discontinued", "active", False, in_scope=True)
        assert plan is not None
        assert plan[0] == "flag"
        assert "resurrection" in plan[1]

    def test_inactive_lifecycle_sets_from_active_or_none(self):
        for cfg_status in (None, "active"):
            assert _plan_station_status(
                cfg_status, "inactive", False, in_scope=True
            ) == ("set", "inactive")

    def test_inactive_lifecycle_already_inactive_or_discontinued_is_noop(self):
        # Already at least as restrictive — nothing to do.
        assert (
            _plan_station_status("inactive", "inactive", False, in_scope=True) is None
        )
        assert (
            _plan_station_status("discontinued", "inactive", False, in_scope=True)
            is None
        )

    def test_cfg_status_case_and_whitespace_insensitive(self):
        assert _plan_station_status(" Inactive ", "active", False, in_scope=True) == (
            "clear",
            None,
        )


def _args(**over):
    base = dict(
        station=[],
        all=False,
        yes=False,
        no_dry_run=False,
        no_sim=False,
        only=None,
        global_cfg=False,
        push=False,
        dry_run=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestGuard:
    def test_all_with_yes_is_refused(self, capsys):
        # The load-bearing guardrail: never blind-bulk-apply (TOS can be stale).
        rc = cmd_cfg_sync_from_tos(_args(all=True, yes=True))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Refusing --all with --yes" in out

    def test_no_station_no_all_errors(self, capsys):
        rc = cmd_cfg_sync_from_tos(_args())
        assert rc == 1
        assert "Specify" in capsys.readouterr().out


class TestOnlyFilter:
    def test_unknown_only_field_rejected(self, capsys):
        # --only validates before any TOS call, so this needs no network.
        rc = cmd_cfg_sync_from_tos(_args(station=["ELDC"], only=["bogus_field"]))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Unknown --only field(s): bogus_field" in out
        # the error lists the valid fields, including the DOMES key
        assert "rinex_marker_number" in out

    def test_domes_and_router_ip_are_valid_only_fields(self):
        # rinex_marker_number is a spec field; router_ip is valid too (from SIM).
        valid = {s.cfg_key for s in _sync_overwrite_specs()} | {"router_ip"}
        assert "rinex_marker_number" in valid
        assert "router_ip" in valid


class TestMarkerNumberStrip:
    """`sync-from-tos --only rinex_marker_number` removes a cfg value that is not
    a valid IERS DOMES (invalid as a MARKER NUMBER — MARKER NAME carries the 4-char
    id), but never a real DOMES that TOS momentarily lacks."""

    def _patch(self, monkeypatch, *, cfg_value, removed):
        import receivers.cfg.reconciler as rec
        import receivers.cli.arguments as cliargs
        import receivers.cli.cfg as cfgmod
        import receivers.config.receivers_config as rc
        import receivers.config_utils as cu

        monkeypatch.setattr(
            cliargs, "normalize_station_tokens", lambda toks: list(toks)
        )
        monkeypatch.setattr(
            cu,
            "get_station_config",
            lambda sid, silent=True: {"rinex_marker_number": cfg_value},
        )
        # Truthy TOS record, but no DOMES → compare_station yields no planned diff.
        monkeypatch.setattr(cfgmod, "_query_tos", lambda sid: {"id_entity": 1})
        monkeypatch.setattr(rec, "compare_station", lambda *a, **k: [])
        monkeypatch.setattr(cfgmod, "_resolve_global_target", lambda args: None)
        monkeypatch.setattr(
            cfgmod, "_local_stations_cfg_path", lambda: Path("/tmp/x.cfg")
        )
        monkeypatch.setattr(cfgmod, "_maybe_commit_global", lambda *a, **k: None)

        def fake_remove(path, sid, key):
            removed.append((sid, key))
            return True

        monkeypatch.setattr(rc, "_remove_cfg_field", fake_remove)

    def test_non_domes_value_is_stripped(self, monkeypatch, capsys):
        removed: list = []
        self._patch(monkeypatch, cfg_value="AFST", removed=removed)
        rc = cmd_cfg_sync_from_tos(
            _args(
                station=["AFST"],
                only=["rinex_marker_number"],
                no_dry_run=True,
                yes=True,
            )
        )
        assert rc == 0
        assert removed == [("AFST", "rinex_marker_number")]
        assert "removed" in capsys.readouterr().out

    def test_real_domes_is_not_clobbered(self, monkeypatch, capsys):
        # cfg already holds a valid DOMES; TOS has none right now → leave it.
        removed: list = []
        self._patch(monkeypatch, cfg_value="10230M001", removed=removed)
        rc = cmd_cfg_sync_from_tos(
            _args(
                station=["REYK"],
                only=["rinex_marker_number"],
                no_dry_run=True,
                yes=True,
            )
        )
        assert rc == 0
        assert removed == []

    def test_dry_run_shows_but_does_not_strip(self, monkeypatch, capsys):
        removed: list = []
        self._patch(monkeypatch, cfg_value="AFST", removed=removed)
        rc = cmd_cfg_sync_from_tos(
            _args(station=["AFST"], only=["rinex_marker_number"])  # dry by default
        )
        assert rc == 0
        assert removed == []  # nothing written in dry-run
        assert "✂" in capsys.readouterr().out  # but the plan shows the strip


class TestStationStatusSync:
    """End-to-end wiring of the station_status lifecycle planner into
    `cfg sync-from-tos`. The directional-asymmetry rule is the thing these
    tests exist to prove: deactivation must NEVER apply without its own
    explicit confirm, --yes or not.
    """

    def _open_receiver_session(self):
        return {
            "time_from": "2020-01-01T00:00:00",
            "time_to": None,
            "gnss_receiver": {"model": "SEPT POLARX5", "serial_number": "3093129"},
        }

    def _patch(
        self,
        monkeypatch,
        *,
        cfg_station_status,
        tos_data,
        removed=None,
        updated=None,
    ):
        import receivers.cfg.reconciler as rec
        import receivers.cli.arguments as cliargs
        import receivers.cli.cfg as cfgmod
        import receivers.config.receivers_config as rc
        import receivers.config_utils as cu

        removed = removed if removed is not None else []
        updated = updated if updated is not None else []

        monkeypatch.setattr(
            cliargs, "normalize_station_tokens", lambda toks: list(toks)
        )
        monkeypatch.setattr(
            cu,
            "get_station_config",
            lambda sid, silent=True: {
                "station_status": cfg_station_status,
                "health_check": None,
                "station_role": None,
            },
        )
        monkeypatch.setattr(cfgmod, "_query_tos", lambda sid: tos_data)
        # No other field diffs in play — isolate station_status.
        monkeypatch.setattr(rec, "compare_station", lambda *a, **k: [])
        monkeypatch.setattr(cfgmod, "_resolve_global_target", lambda args: None)
        monkeypatch.setattr(
            cfgmod, "_local_stations_cfg_path", lambda: Path("/tmp/x.cfg")
        )
        monkeypatch.setattr(cfgmod, "_maybe_commit_global", lambda *a, **k: None)

        def fake_remove(path, sid, key):
            removed.append((sid, key))
            return True

        def fake_update(path, sid, key, value):
            updated.append((sid, key, value))
            return True

        monkeypatch.setattr(rc, "_remove_cfg_field", fake_remove)
        monkeypatch.setattr(rc, "_update_cfg_field", fake_update)
        return removed, updated

    def test_activation_clears_with_normal_yes_no_extra_prompt(self, monkeypatch):
        """cfg says inactive, TOS shows an open receiver — --yes alone is
        enough, same as every other safe-to-auto-apply field here."""
        removed, _ = self._patch(
            monkeypatch,
            cfg_station_status="inactive",
            tos_data={"device_history": [self._open_receiver_session()]},
        )
        rc = cmd_cfg_sync_from_tos(
            _args(station=["HAMR"], no_sim=True, no_dry_run=True, yes=True)
        )
        assert rc == 0
        assert removed == [("HAMR", "station_status")]

    def test_deactivation_requires_its_own_confirm_even_with_yes(self, monkeypatch):
        """THE safety property: --yes must NOT silently deactivate a station.
        A separate, explicit prompt is required — answering 'n' leaves cfg
        untouched."""
        _, updated = self._patch(
            monkeypatch,
            cfg_station_status=None,  # cfg thinks it's active
            tos_data={"device_history": []},  # TOS: no open receiver
        )
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        rc = cmd_cfg_sync_from_tos(
            _args(station=["HAMR"], no_sim=True, no_dry_run=True, yes=True)
        )
        assert rc == 0
        assert updated == []  # refused at the confirm — nothing written

    def test_deactivation_applies_only_after_explicit_yes_at_its_own_prompt(
        self, monkeypatch
    ):
        _, updated = self._patch(
            monkeypatch,
            cfg_station_status=None,
            tos_data={"device_history": []},
        )
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        rc = cmd_cfg_sync_from_tos(
            _args(station=["HAMR"], no_sim=True, no_dry_run=True, yes=True)
        )
        assert rc == 0
        assert updated == [("HAMR", "station_status", "inactive")]

    def test_dry_run_shows_deactivation_but_writes_nothing(self, monkeypatch, capsys):
        _, updated = self._patch(
            monkeypatch,
            cfg_station_status=None,
            tos_data={"device_history": []},
        )
        rc = cmd_cfg_sync_from_tos(
            _args(station=["HAMR"], no_sim=True)
        )  # dry by default
        assert rc == 0
        assert updated == []
        assert "DEACTIVATION" in capsys.readouterr().out

    def test_contradiction_flags_and_writes_nothing(self, monkeypatch, capsys):
        """date_end set AND an open receiver join — never auto-resolved."""
        _, updated = self._patch(
            monkeypatch,
            cfg_station_status=None,
            tos_data={
                "date_end": "2024-06-01",
                "device_history": [self._open_receiver_session()],
            },
        )
        rc = cmd_cfg_sync_from_tos(
            _args(station=["HAMR"], no_sim=True, no_dry_run=True, yes=True)
        )
        assert rc == 0
        assert updated == []
        assert "hygiene" in capsys.readouterr().out

    def test_out_of_scope_station_gets_no_plan_at_all(self, monkeypatch, capsys):
        """External/passive station: no gnss_receiver in TOS is NORMAL, must
        never produce a deactivation plan."""
        import receivers.cli.cfg as cfgmod
        import receivers.config_utils as cu

        self._patch(
            monkeypatch,
            cfg_station_status=None,
            tos_data={"device_history": []},
        )
        monkeypatch.setattr(
            cu,
            "get_station_config",
            lambda sid, silent=True: {
                "station_status": None,
                "health_check": "passive",
                "station_role": None,
            },
        )
        rc = cmd_cfg_sync_from_tos(
            _args(station=["KRAC"], no_sim=True, no_dry_run=True, yes=True)
        )
        assert rc == 0
        assert "already in sync" in capsys.readouterr().out

    def test_only_without_station_status_skips_lifecycle_entirely(
        self, monkeypatch, capsys
    ):
        """--only rinex_marker_number (etc.) must not touch station_status at
        all — no TOS lifecycle call, no plan, no prompt."""
        self._patch(
            monkeypatch,
            cfg_station_status=None,
            tos_data={"device_history": []},  # would deactivate if considered
        )
        rc = cmd_cfg_sync_from_tos(
            _args(
                station=["HAMR"],
                no_sim=True,
                only=["rinex_marker_number"],
                no_dry_run=True,
                yes=True,
            )
        )
        assert rc == 0
        assert "station_status" not in capsys.readouterr().out
