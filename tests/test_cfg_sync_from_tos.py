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
