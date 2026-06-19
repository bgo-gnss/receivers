"""Tests for `receivers cfg sync-from-tos` (TOS → stations.cfg device sync).

The command itself needs live TOS + cfg, so these cover the pure decision units
and the safety guard, which is where the correctness/risk lives.
"""

import argparse

from receivers.cli.cfg import (
    _is_literal_ip,
    _plan_router_ip,
    _sync_overwrite_specs,
    cmd_cfg_sync_from_tos,
)


class TestOverwriteSpecs:
    def test_overwrites_device_identity_only(self):
        keys = {s.cfg_key for s in _sync_overwrite_specs()}
        # TOS-authoritative device fields are overwritten
        assert keys == {
            "receiver_type",
            "receiver_serial",
            "receiver_firmware_version",
            "antenna_type",
            "antenna_serial",
            "antenna_radome",
            "station_name",
        }

    def test_never_surveyed_position(self):
        keys = {s.cfg_key for s in _sync_overwrite_specs()}
        # stations.cfg / survey is ground truth — these must stay flag-only
        for k in ("latitude", "longitude", "height", "antenna_height"):
            assert k not in keys


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


def _args(**over):
    base = dict(
        station=[],
        all=False,
        yes=False,
        no_dry_run=False,
        no_sim=False,
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
