"""Tests for EPOS dissemination (receivers.dissemination) — T1 tracer bullet.

Pure-logic tests run everywhere. The end-to-end convert+push test is guarded:
it needs the gps-tools binaries (CRX2RNX/gfzrnx) and a real archived RINEX sample,
so it skips cleanly in environments that lack either.
"""

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from receivers.dissemination.config import (
    DisseminationFormat,
    DisseminationTarget,
    VersionPolicy,
    load_dissemination_config,
)
from receivers.dissemination.convert import (
    _crinex_to_obs_name,
    _is_hatanaka,
    _obs_to_crinex_name,
    _strip_compression,
    cache_key,
    published_name,
    resolve_tool,
)
from receivers.dissemination.engine import EposDisseminate
from receivers.dissemination.qc_gate import qc_check, select_session
from receivers.dissemination.tos_access import (
    epos_markers,
    get_attribute_value,
    is_epos_flagged,
    make_session_provider,
    missing_required_attributes,
)


def _target(tmp_root, **over):
    base = dict(
        name="epos",
        active=False,
        host="",
        user="epos",
        dest=str(tmp_root / "stage"),
        source_root=str(tmp_root / "archive"),
        sessions=("15s_24hr",),
        exclude_stations=frozenset({"DYNA", "HRNC", "HAUR"}),
        convert_cache_dir=str(tmp_root / "cache"),
    )
    base.update(over)
    return DisseminationTarget(**base)


# --------------------------------------------------------------------------- config
class TestConfig:
    def test_loads_only_dissemination_tier(self, tmp_path):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            """
targets:
  - name: imo_archive
    tier: archive
    host: rawdata
    user: gpsops
    dest: ~/gpsdata
    source_root: /mnt/data/gpsdata
    cutover: "2026-06-22T00:00:00"
  - name: epos
    tier: dissemination
    active: true
    host: ""
    user: epos
    dest: /tmp/epos_stage
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr]
    exclude_stations: [DYNA]
    convert_cache_dir: ~/.cache/epos
    format:
      preserve_source_version: true
      country_code: ISL
      rinex2:
        naming: short
        hatanaka: true
        compression: Z
      rinex3:
        naming: long
        hatanaka: true
        compression: gz
      dir_template: "%Y/#b/{station}/15s_24hr/rinex/"
"""
        )
        targets = load_dissemination_config(cfg)
        assert [t.name for t in targets] == ["epos"]  # archive tier filtered out
        t = targets[0]
        assert t.active is True
        assert t.format.country_code == "ISL"
        assert t.format.preserve_source_version is True
        assert t.format.policy_for(3).naming == "long"
        assert t.format.policy_for(3).compression == "gz"
        assert t.format.policy_for(2).naming == "short"
        assert t.format.policy_for(2).compression == "Z"
        assert t.format.dir_template.startswith("%Y")
        assert "DYNA" in t.exclude_stations

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_dissemination_config(tmp_path / "nope.yaml") == []

    def test_remote_spec_local_vs_remote(self, tmp_path):
        local = _target(tmp_path)
        assert local.remote == local.dest  # empty host => bare local path
        remote = _target(tmp_path, host="epos-portal.vedur.is")
        assert remote.remote == f"epos@epos-portal.vedur.is:{remote.dest}"


# --------------------------------------------------------------------------- convert helpers
class TestConvertHelpers:
    @pytest.mark.parametrize(
        "name,expected_stem,expected_flag",
        [
            ("FIM21280.26d.gz", "FIM21280.26d", True),
            ("FIM21280.26D.Z", "FIM21280.26D", True),
            ("FIM21280.26o", "FIM21280.26o", False),
            ("foo.crx.gz", "foo.crx", True),
        ],
    )
    def test_strip_compression(self, name, expected_stem, expected_flag):
        assert _strip_compression(name) == (expected_stem, expected_flag)

    @pytest.mark.parametrize(
        "crinex,obs",
        [
            ("FIM21280.26d", "FIM21280.26o"),
            ("FIM21280.26D", "FIM21280.26O"),
            (
                "STAT00ISL_R_20261280000_01D_15S_MO.crx",
                "STAT00ISL_R_20261280000_01D_15S_MO.rnx",
            ),
        ],
    )
    def test_crinex_to_obs_name(self, crinex, obs):
        assert _crinex_to_obs_name(crinex) == obs

    @pytest.mark.parametrize(
        "name,is_hat",
        [
            ("FIM21280.26d", True),
            ("FIM21280.26D", True),
            ("foo.crx", True),
            ("FIM21280.26o", False),
            ("STAT_..._MO.rnx", False),
            ("", False),
        ],
    )
    def test_is_hatanaka(self, name, is_hat):
        assert _is_hatanaka(name) is is_hat

    def test_cache_key_depends_on_fingerprint(self, tmp_path):
        # A plain (uncompressed) file is hashed as-is by content_sha256.
        f = tmp_path / "sample.rnx"
        f.write_bytes(b"RINEX CONTENT")
        k_empty = cache_key(f, "")
        k_fp = cache_key(f, "tos-fingerprint-v1")
        assert k_empty != k_fp  # header correction (new fingerprint) ⇒ new cache slot
        assert cache_key(f, "tos-fingerprint-v1") == k_fp  # deterministic

    @pytest.mark.parametrize(
        "obs,crinex",
        [
            ("STAT00ISL_R_..._MO.rnx", "STAT00ISL_R_..._MO.crx"),
            ("FIM21280.26o", "FIM21280.26d"),
            ("FIM21280.26O", "FIM21280.26D"),
        ],
    )
    def test_obs_to_crinex_name(self, obs, crinex):
        assert _obs_to_crinex_name(obs) == crinex

    def test_published_name_per_policy(self):
        # R3 long, Hatanaka + gz
        r3 = VersionPolicy(naming="long", hatanaka=True, compression="gz")
        assert (
            published_name("RHOF00ISL_R_20261280000_01D_15S_MO.rnx", r3)
            == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"
        )
        # R2 short, Hatanaka + .Z (legacy)
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        assert published_name("FIM21280.26o", r2) == "FIM21280.26d.Z"
        # plain obs, no hatanaka, gz
        plain = VersionPolicy(naming="long", hatanaka=False, compression="gz")
        assert published_name("X.rnx", plain) == "X.rnx.gz"
        # no compression
        none = VersionPolicy(naming="long", hatanaka=False, compression="none")
        assert published_name("X.rnx", none) == "X.rnx"

    def test_resolve_tool_missing(self):
        with pytest.raises(Exception):
            resolve_tool("definitely-not-a-real-tool-xyz")


# --------------------------------------------------------------------------- engine (no tools)
class TestEngineSourceResolution:
    def test_excluded_station_is_noop(self, tmp_path):
        eng = EposDisseminate(_target(tmp_path))
        r = eng.run_one("DYNA", date(2026, 5, 8))
        assert r.ok is True
        assert "excluded" in r.message

    def test_missing_source_reports_cleanly(self, tmp_path):
        eng = EposDisseminate(_target(tmp_path))
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.ok is False
        assert r.source_path is None
        assert "no archived RINEX" in r.message

    def test_find_source_globs_archive_layout(self, tmp_path):
        # Build the archive layout: <root>/2026/may/FIM2/15s_24hr/rinex/<file>
        d = date(2026, 5, 8)  # doy 128
        rinex_dir = (
            tmp_path / "archive" / "2026" / "may" / "FIM2" / "15s_24hr" / "rinex"
        )
        rinex_dir.mkdir(parents=True)
        sample = rinex_dir / "FIM21280.26d.gz"
        sample.write_bytes(b"x")
        eng = EposDisseminate(_target(tmp_path))
        found = eng.find_source("FIM2", d)
        assert found == sample


# --------------------------------------------------------------------------- QC gate (pure)
def _write_min_header(
    path: Path, marker: str, *, xyz: str = "", receiver: str = "", number: str = ""
) -> Path:
    """Write a minimal RINEX-3 header file (enough for extract_header_info)."""
    lines = [
        "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE",
        f"{marker:<60}MARKER NAME",
    ]
    if number:
        lines.append(f"{number:<60}MARKER NUMBER")
    if receiver:
        lines.append(f"{receiver:<60}REC # / TYPE / VERS")
    if xyz:
        lines.append(f"{xyz:<60}APPROX POSITION XYZ")
    lines.append(f"{'':<60}END OF HEADER")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestQCGate:
    def test_select_session_by_date(self):
        history = [
            {
                "time_from": datetime(2020, 1, 1),
                "time_to": datetime(2021, 1, 1),
                "marker": "OLD",
            },
            {"time_from": datetime(2021, 1, 1), "time_to": None, "marker": "CUR"},
        ]
        assert select_session(history, datetime(2020, 6, 1))["marker"] == "OLD"
        assert select_session(history, datetime(2026, 5, 8))["marker"] == "CUR"
        assert select_session(history, datetime(2019, 1, 1)) is None

    def test_no_session_fails(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, None)
        assert v.passed is False
        assert "no TOS session" in v.message

    def test_marker_match_passes(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, {"marker": "FIM2"})
        assert v.passed is True

    def test_marker_mismatch_is_blocking(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, {"marker": "XXXX"})
        assert v.passed is False
        assert "marker" in v.blocking

    def test_receiver_diff_is_not_blocking(self, tmp_path):
        # gnss_receiver is recorded as a (non-blocking) discrepancy; marker matches.
        f = _write_min_header(
            tmp_path / "h.rnx", "FIM2", receiver="3070340 SEPT POLARX5 5.4.0"
        )
        session = {
            "marker": "FIM2",
            "gnss_receiver": {
                "serial_number": "1",
                "model": "X",
                "firmware_version": "1",
            },
        }
        v = qc_check(f, session)
        assert v.passed is True  # receiver discrepancy does not block
        assert "receiver" in v.discrepancies

    def test_unreadable_header_fails(self, tmp_path):
        bad = tmp_path / "bad.rnx"
        bad.write_text("not a rinex file\n")
        v = qc_check(bad, {"marker": "FIM2"})
        assert v.passed is False

    def test_epos_9char_marker_accepted(self, tmp_path):
        # EPOS 4.1.7: 9-char MARKER NAME whose 4-char prefix is the TOS marker
        # must NOT be flagged as a mismatch.
        f = _write_min_header(tmp_path / "h.rnx", "FIM200ISL")
        v = qc_check(f, {"marker": "FIM2"})
        assert v.passed is True
        assert "marker" not in v.blocking
        assert v.matches.get("marker") == "FIM200ISL"

    def test_domes_match_passes_mismatch_blocks(self, tmp_path):
        # DOMES present in TOS → MARKER NUMBER must equal it.
        ok = _write_min_header(tmp_path / "ok.rnx", "FIM200ISL", number="10222M001")
        v = qc_check(ok, {"marker": "FIM2", "domes": "10222M001"})
        assert v.passed is True
        assert v.matches.get("domes") == "10222M001"

        bad = _write_min_header(tmp_path / "bad.rnx", "FIM200ISL", number="FIM2")
        v = qc_check(bad, {"marker": "FIM2", "domes": "10222M001"})
        assert v.passed is False
        assert "domes" in v.blocking


# --------------------------------------------------------------------------- EPOS filter (offline)
def _station(marker, *, epos="true", drop=()):
    """Build a TOS-style station dict with the full required attribute set."""
    attrs = {
        "marker": marker,
        "in_network_epos": epos,
        "lat": "64.1",
        "lon": "-21.9",
        "altitude": "10",
        "bedrock_condition": "good",
        "bedrock_type": "basalt",
        "geological_characteristic": "x",
        "name": f"{marker} station",
        "date_start": "2020-01-01",
    }
    for k in drop:
        attrs.pop(k, None)
    return {"attributes": [{"code": k, "value": v} for k, v in attrs.items()]}


class TestEposFilter:
    def test_get_attribute_value(self):
        st = _station("FIM2")
        assert get_attribute_value(st["attributes"], "marker") == "FIM2"
        assert get_attribute_value(st["attributes"], "nope") is None

    def test_is_epos_flagged(self):
        assert is_epos_flagged(_station("A", epos="true")) is True
        assert is_epos_flagged(_station("A", epos="TRUE")) is True
        assert is_epos_flagged(_station("A", epos="false")) is False

    def test_missing_required_attributes(self):
        assert missing_required_attributes(_station("A")) == []
        miss = missing_required_attributes(_station("A", drop=("bedrock_type", "name")))
        assert set(miss) == {"bedrock_type", "name"}

    def test_epos_markers_filters_flag_and_completeness(self):
        stations = [
            _station("FIM2", epos="true"),
            _station("GORE", epos="false"),  # not flagged
            _station("INCM", epos="true", drop=("lat",)),  # incomplete
            _station("REYK", epos="true"),
        ]
        assert epos_markers(stations) == ["FIM2", "REYK"]


class TestSessionFingerprint:
    def test_empty_session_is_empty_fingerprint(self):
        from receivers.dissemination.tos_access import session_fingerprint

        assert session_fingerprint(None) == ""
        assert session_fingerprint({}) == ""

    def test_changes_on_receiver_or_antenna(self):
        from receivers.dissemination.tos_access import session_fingerprint

        base = {
            "marker": "RHOF",
            "gnss_receiver": {
                "model": "NETR9",
                "serial_number": "1",
                "firmware_version": "4.6",
            },
            "antenna": {
                "model": "TRM57971.00",
                "serial_number": "9",
                "antenna_height": "1.0",
            },
            "radome": {"model": "NONE"},
        }
        fp = session_fingerprint(base)
        assert fp == session_fingerprint(dict(base))  # stable
        changed = {
            **base,
            "gnss_receiver": {**base["gnss_receiver"], "model": "MOSAIC"},
        }
        assert session_fingerprint(changed) != fp  # receiver change ⇒ new fingerprint

    def test_ignores_non_header_fields(self):
        from receivers.dissemination.tos_access import session_fingerprint

        base = {"marker": "RHOF", "gnss_receiver": {"model": "NETR9"}}
        noisy = {**base, "description": "irrelevant", "time_to": "2026-01-01"}
        assert session_fingerprint(base) == session_fingerprint(noisy)


class TestSessionProvider:
    class _FakeClient:
        def __init__(self, meta):
            self._meta = meta

        def get_complete_station_metadata(self, station):
            return self._meta

    def test_provider_selects_session_and_injects_marker(self):
        meta = {
            "marker": "fim2",
            "device_history": [
                {
                    "time_from": datetime(2021, 1, 1),
                    "time_to": None,
                    "gnss_receiver": {},
                },
            ],
        }
        provider = make_session_provider(self._FakeClient(meta))
        session = provider("FIM2", datetime(2026, 5, 8))
        assert session is not None
        assert session["marker"] == "FIM2"  # injected, upper-cased

    def test_provider_returns_none_when_no_coverage(self):
        meta = {"marker": "FIM2", "device_history": []}
        provider = make_session_provider(self._FakeClient(meta))
        assert provider("FIM2", datetime(2026, 5, 8)) is None

    def test_provider_failsafe_on_tos_error(self):
        class Boom:
            def get_complete_station_metadata(self, station):
                raise RuntimeError("TOS down")

        provider = make_session_provider(Boom())
        assert provider("FIM2", datetime(2026, 5, 8)) is None


# --------------------------------------------------------------------------- end-to-end (guarded)
def _tools_available() -> bool:
    try:
        resolve_tool("CRX2RNX")
        resolve_tool("gfzrnx")
        return True
    except Exception:
        return False


_SAMPLE = Path.home() / "tmp/gpsdata/2026/may/FIM2/15s_24hr/rinex/FIM21280.26d.gz"

# The local archive's "*.26d.gz" 15s files are RINEX 3.04 content (Hatanaka) with
# legacy short names — detect-from-content yields version 3 → long-name .crx.gz.
_PUBLISHED = "FIM200ISL_R_20261280000_01D_15S_MO.crx.gz"
_LAYOUT = "2026/may/FIM2/15s_24hr/rinex"


@pytest.mark.skipif(
    not (_tools_available() and _SAMPLE.is_file()),
    reason="needs gps-tools binaries and a real archived RINEX sample",
)
class TestEndToEnd:
    def test_convert_produces_canonical_obs(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        res = convert_for_dissemination(
            _SAMPLE,
            "FIM2",
            datetime(2026, 5, 8),
            fmt=DisseminationFormat(),
            cache_dir=tmp_path / "cache",
        )
        assert res.obs_name == "FIM200ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.rinex_version == 3
        assert res.output_path.is_file()
        assert res.cached is False
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_second_convert_is_cache_hit(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        kw = dict(fmt=DisseminationFormat(), cache_dir=tmp_path / "cache")
        first = convert_for_dissemination(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        second = convert_for_dissemination(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        assert first.cached is False
        assert second.cached is True

    def _archive_with_sample(self, tmp_path):
        rinex_dir = tmp_path / "archive/2026/may/FIM2/15s_24hr/rinex"
        rinex_dir.mkdir(parents=True)
        os.symlink(_SAMPLE, rinex_dir / "FIM21280.26d.gz")

    def test_run_one_publishes_crx_gz_in_layout(self, tmp_path):
        self._archive_with_sample(tmp_path)
        eng = EposDisseminate(_target(tmp_path))  # no provider → set-header off
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == _PUBLISHED
        assert r.rinex_version == 3
        assert r.relative_path == f"{_LAYOUT}/{_PUBLISHED}"
        assert (tmp_path / "stage" / _LAYOUT / _PUBLISHED).is_file()

    def test_qc_gate_passes_then_pushes(self, tmp_path):
        self._archive_with_sample(tmp_path)
        provider = lambda station, dt: {"marker": "FIM2"}  # noqa: E731
        eng = EposDisseminate(
            _target(tmp_path), session_provider=provider, set_header=False
        )
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.qc_passed is True
        assert r.ok is True
        assert (tmp_path / "stage" / _LAYOUT / r.long_name).is_file()

    def test_qc_gate_blocks_on_marker_mismatch(self, tmp_path):
        self._archive_with_sample(tmp_path)
        provider = lambda station, dt: {"marker": "WRNG"}  # noqa: E731
        eng = EposDisseminate(
            _target(tmp_path), session_provider=provider, set_header=False
        )
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.qc_passed is False
        assert r.ok is False
        assert "QC gate failed" in r.message
        assert not (tmp_path / "stage").exists() or not list(
            (tmp_path / "stage").rglob("*.gz")
        )


# --------------------------------------------------------------------------- ETL + indexer (pure)
class TestEtlPure:
    def test_llh_to_xyz_reyk(self):
        from receivers.dissemination.epos_etl import llh_to_xyz

        x, y, z = llh_to_xyz(64.1388, -21.9555, 93.0)  # ~REYK
        # ECEF magnitude ≈ Earth radius (~6.37e6 m).
        assert abs((x**2 + y**2 + z**2) ** 0.5 - 6.37e6) < 5e4
        assert z > 0  # northern hemisphere

    def test_file_type_for(self):
        from receivers.dissemination.rinex_index import _file_type_for

        assert _file_type_for(3, "15s_24hr") == {
            "format": "RINEX3",
            "sampling_window": "24hour",
            "sampling_frequency": "15s",
        }


def _local_epos_db_ov():
    import getpass

    return {
        "host": "localhost",
        "dbname": "gnss_europe_local",
        "user": getpass.getuser(),
        "schema": "public",
    }


def _local_epos_db_available() -> bool:
    try:
        from receivers.dissemination import epos_db

        conn = epos_db.connect(_local_epos_db_ov())
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _local_epos_db_available(),
    reason="needs the local gnss_europe_local harness DB",
)
class TestEposDbHelpers:
    def test_insert_get_update_roundtrip(self):
        from receivers.dissemination import epos_db

        conn = epos_db.connect(_local_epos_db_ov())
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT t")
                aid = epos_db.get_or_create(
                    cur, "agency", {"abbreviation": "ZZ"}, {"name": "Z"}
                )
                aid2 = epos_db.get_or_create(cur, "agency", {"abbreviation": "ZZ"})
                assert aid == aid2  # get-or-create returns the same row
                epos_db.update_row(cur, "agency", aid, {"name": "Z2"})
                cur.execute("SELECT name FROM agency WHERE id = %s", (aid,))
                assert cur.fetchone()[0] == "Z2"
                cur.execute("ROLLBACK TO SAVEPOINT t")  # leave the DB clean
        finally:
            conn.rollback()
            conn.close()


class TestVocabMapping:
    def test_receiver_subtype_is_gnss_receiver(self):
        from receivers.dissemination.epos_etl import _ATTR_MAP, WHITELISTED_ITEMS

        assert "gnss_receiver" in WHITELISTED_ITEMS
        assert "receiver" not in WHITELISTED_ITEMS  # TOS uses gnss_receiver
        assert _ATTR_MAP["gnss_receiver"]["model"] == "receiver_type"
        assert _ATTR_MAP["antenna"]["model"] == "antenna_type"
        assert _ATTR_MAP["radome"]["model"] == "radome_type"

    def test_type_resolve_covers_trigger_attributes(self):
        from receivers.dissemination.epos_etl import _TYPE_RESOLVE

        # The schema triggers fire on antenna_type/receiver_type/radome_type.
        assert set(_TYPE_RESOLVE) == {"antenna_type", "receiver_type", "radome_type"}


@pytest.mark.skipif(
    not _local_epos_db_available(),
    reason="needs the local gnss_europe_local harness DB (seeded vocab + type tables)",
)
class TestItemVocabETL:
    def test_populate_items_resolves_types_and_fires_triggers(self):
        from datetime import datetime as _dt

        from receivers.dissemination import epos_db
        from receivers.dissemination.epos_etl import _populate_items

        conn = epos_db.connect(_local_epos_db_ov())
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT t")
                sid = epos_db.insert_row(cur, "station", {"name": "T", "marker": "TST"})
                children = [
                    (
                        {"time_from": _dt(2021, 1, 1), "time_to": None},
                        {
                            "code_entity_subtype": "antenna",
                            "id_entity": 1,
                            "attributes": [
                                {"code": "model", "value": "ASH701945C_M"},
                                {"code": "serial_number", "value": "S1"},
                            ],
                        },
                    ),
                    (
                        {"time_from": _dt(2021, 1, 1), "time_to": None},
                        {
                            "code_entity_subtype": "gnss_receiver",
                            "id_entity": 2,
                            "attributes": [
                                {"code": "model", "value": "TRIMBLE NETR9"},
                                {"code": "firmware_version", "value": "4.6"},
                            ],
                        },
                    ),
                ]
                n = _populate_items(cur, sid, children)
                assert n == 2
                # Scope all checks to THIS station's items (the harness DB may hold
                # committed fleet data from other ETL runs).
                scoped = (
                    "SELECT ia.value_varchar, ia.value_numeric FROM item_attribute ia "
                    "JOIN station_item si ON si.id_item = ia.id_item "
                    "WHERE si.id_station = %s AND ia.id_attribute = %s"
                )
                # antenna_type resolved to value_numeric (the trigger requires it)
                cur.execute(scoped, (sid, 1))
                assert cur.fetchone() == ("ASH701945C_M", 52)
                # the trigger wrote a filter_antenna row for this item_attribute
                cur.execute(
                    "SELECT count(*) FROM filter_antenna fa "
                    "JOIN item_attribute ia ON ia.id = fa.id_item_attribute "
                    "JOIN station_item si ON si.id_item = ia.id_item "
                    "WHERE si.id_station = %s",
                    (sid,),
                )
                assert cur.fetchone()[0] == 1
                # serial_number (id 5) carries no value_numeric
                cur.execute(scoped, (sid, 5))
                assert cur.fetchone()[1] is None
                cur.execute("ROLLBACK TO SAVEPOINT t")
        finally:
            conn.rollback()
            conn.close()


class TestRinexMd5s:
    def test_plain_rinex_md5s_equal(self, tmp_path):
        from receivers.dissemination.rinex_index import rinex_md5s

        f = tmp_path / "STAT00ISL_R_20261280000_01D_15S_MO.rnx"
        f.write_bytes(b"     3.04           OBSERVATION DATA\n")
        chk, unc = rinex_md5s(f)
        assert chk == unc  # plain (uncompressed, non-Hatanaka) ⇒ identical

    def test_gz_md5checksum_differs_uncompressed_matches_plain(self, tmp_path):
        import gzip as _gz

        from receivers.dissemination.rinex_index import _md5_bytes, rinex_md5s

        body = b"     3.04           OBSERVATION DATA\nplain rinex body\n"
        gzf = tmp_path / "STAT00ISL_R_20261280000_01D_15S_MO.rnx.gz"
        with _gz.open(gzf, "wb") as fh:
            fh.write(body)
        chk, unc = rinex_md5s(gzf)
        assert chk != unc  # compressed bytes vs content
        assert unc == _md5_bytes(body)  # uncompressed md5 == the obs content md5


# --------------------------------------------------------------------------- raw→rinex (guarded, Docker)
_RAW_SAMPLE = (
    Path.home() / "tmp/gpsdata/2026/may/RHOF/15s_24hr/raw/RHOF202605080000a.T02.gz"
)


def _native_trimble_available() -> bool:
    try:
        from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

        return bool(TrimbleNativeConverter.is_available())
    except Exception:
        return False


@pytest.mark.skipif(
    not (_native_trimble_available() and _RAW_SAMPLE.is_file()),
    reason="needs the trm2rinex Docker image and a real archived Trimble .T02 sample",
)
class TestRawFallback:
    def test_raw_t02_converts_to_r3_obs(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        res = convert_for_dissemination(
            _RAW_SAMPLE,
            "RHOF",
            datetime(2026, 5, 8),
            fmt=DisseminationFormat(),
            cache_dir=tmp_path / "cache",
        )
        assert res.obs_name == "RHOF00ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.rinex_version == 3
        assert res.output_path.is_file()
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_engine_prefers_rinex_else_raw(self, tmp_path):
        # Only raw present → engine falls back to the raw path, publishes .crx.gz.
        raw_dir = tmp_path / "archive/2026/may/RHOF/15s_24hr/raw"
        raw_dir.mkdir(parents=True)
        os.symlink(_RAW_SAMPLE, raw_dir / _RAW_SAMPLE.name)
        eng = EposDisseminate(_target(tmp_path))  # no QC provider
        r = eng.run_one("RHOF", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"
        layout = "2026/may/RHOF/15s_24hr/rinex"
        assert (tmp_path / "stage" / layout / r.long_name).is_file()


# --------------------------------------------------------------------------- EPOS header finalization (C1/C2)
class TestEposHeaderFinalize:
    """C1/C2: 9-char MARKER NAME (R3), DOMES in MARKER NUMBER, generic OBSERVER."""

    def _header(self, tmp_path, *, marker="RHOF", number=None, observer="BGO/HMF"):
        lines = [
            "     3.04           OBSERVATION DATA    M (MIXED)"
            "           RINEX VERSION / TYPE\n",
            f"{marker:<60}MARKER NAME\n",
        ]
        if number is not None:
            lines.append(f"{number:<60}MARKER NUMBER\n")
        lines.append(f"{observer:<20}{'Old Agency':<40}OBSERVER / AGENCY\n")
        lines.append(" " * 60 + "END OF HEADER\n")
        p = tmp_path / "x.rnx"
        p.write_text("".join(lines))
        return p

    def _records(self, path):
        recs = {}
        for line in path.read_text().splitlines():
            label = line[60:80].strip()
            if label:
                recs[label] = line[:60]
        return recs

    def test_marker_name_9char_for_r3(self):
        from receivers.dissemination.convert import epos_marker_name

        assert epos_marker_name("RHOF", 3, "ISL") == "RHOF00ISL"
        assert epos_marker_name("rhof", 3, "isl") == "RHOF00ISL"
        assert epos_marker_name("RHOF", 2, "ISL") == "RHOF"  # 4-char for R2

    def test_finalize_r3_updates_and_inserts(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        # MARKER NUMBER absent + personal-initials OBSERVER (the FIHO/RHOF case)
        p = self._header(tmp_path, marker="RHOF", number=None, observer="BGO/HMF")
        finalize_epos_header(
            p,
            "RHOF",
            3,
            country_code="ISL",
            domes="10216M001",
            observer="GNSSatIMO",
            agency="Vedurstofa Islands",
        )
        recs = self._records(p)
        assert recs["MARKER NAME"].strip() == "RHOF00ISL"
        assert recs["MARKER NUMBER"].strip() == "10216M001"  # inserted
        assert recs["OBSERVER / AGENCY"][:20].strip() == "GNSSatIMO"
        assert recs["OBSERVER / AGENCY"][20:].strip() == "Vedurstofa Islands"

    def test_finalize_no_domes_falls_back_to_4char_number(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        p = self._header(tmp_path, marker="ABCD", number="ABCD")
        finalize_epos_header(p, "ABCD", 3, country_code="ISL", domes="")
        assert self._records(p)["MARKER NUMBER"].strip() == "ABCD"

    def test_finalize_is_idempotent(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        p = self._header(tmp_path, number="10216M001")
        kw = dict(
            country_code="ISL",
            domes="10216M001",
            observer="GNSSatIMO",
            agency="Vedurstofa Islands",
        )
        finalize_epos_header(p, "RHOF", 3, **kw)
        first = p.read_text()
        finalize_epos_header(p, "RHOF", 3, **kw)
        assert p.read_text() == first


# --------------------------------------------------------------------------- sampling / config-driven naming
class TestSamplingConfig:
    """C4/C5: sampling + naming knobs come from the format policy, and the
    frequency token is derived from the file's INTERVAL, not a hardcoded default."""

    def _write_header(self, tmp_path, interval=None):
        lines = [
            "     3.04           OBSERVATION DATA    M (MIXED)"
            "           RINEX VERSION / TYPE\n"
        ]
        if interval is not None:
            lines.append(f"{interval:10.3f}".ljust(60) + "INTERVAL\n")
        lines.append(" " * 60 + "END OF HEADER\n")
        p = tmp_path / "x.rnx"
        p.write_text("".join(lines))
        return p

    def test_detect_interval(self, tmp_path):
        from receivers.dissemination.convert import detect_interval

        assert detect_interval(self._write_header(tmp_path, 15.0)) == 15.0
        assert detect_interval(self._write_header(tmp_path, 30.0)) == 30.0
        assert detect_interval(self._write_header(tmp_path, None)) is None

    def test_frequency_token(self):
        from receivers.dissemination.convert import _frequency_token

        assert _frequency_token(15) == "15S"
        assert _frequency_token(30) == "30S"
        assert _frequency_token(1) == "01S"
        assert _frequency_token(5) == "05S"

    def test_resolve_data_frequency_prefers_config_then_interval(self, tmp_path):
        from receivers.dissemination.convert import _resolve_data_frequency

        f15 = self._write_header(tmp_path, 15.0)
        assert _resolve_data_frequency(f15, sample=30) == "30S"  # config override wins
        assert _resolve_data_frequency(f15, sample=None) == "15S"  # else INTERVAL
        f_none = self._write_header(tmp_path, None)
        assert _resolve_data_frequency(f_none, sample=None) == "15S"  # logged fallback

    def test_format_policy_reads_sample_and_period(self):
        fmt = DisseminationFormat.from_dict(
            {"sample": 30, "file_period": "01D", "country_code": "ISL"}
        )
        assert fmt.sample == 30
        assert fmt.file_period == "01D"
        # default: no decimation, source rate preserved
        assert DisseminationFormat.from_dict({}).sample is None


# --------------------------------------------------------------------------- raw-source date matching
class TestFindRawSource:
    """Regression: the raw dir holds a whole month, so find_raw_source MUST
    match the requested day, not just return the directory's earliest file."""

    def _make_raw(self, tmp_path, *names):
        raw_dir = tmp_path / "archive/2026/jun/AKUR/15s_24hr/raw"
        raw_dir.mkdir(parents=True)
        for n in names:
            (raw_dir / n).write_bytes(b"dummy")
        return raw_dir

    def test_picks_date_matching_file_in_multi_day_dir(self, tmp_path):
        self._make_raw(
            tmp_path,
            "AKUR202606010000a.T02.gz",  # earliest — the old bug returned this
            "AKUR202606270000a.T02.gz",
            "AKUR202606280000a.T02.gz",  # the one we want
        )
        eng = EposDisseminate(_target(tmp_path))
        got = eng.find_raw_source("AKUR", date(2026, 6, 28))
        assert got is not None and got.name == "AKUR202606280000a.T02.gz"

    def test_matches_underscore_padded_netr5_name(self, tmp_path):
        self._make_raw(
            tmp_path,
            "AKUR______202606010000a.T02",
            "AKUR______202606280000a.T02",
        )
        eng = EposDisseminate(_target(tmp_path))
        got = eng.find_raw_source("AKUR", date(2026, 6, 28))
        assert got is not None and got.name == "AKUR______202606280000a.T02"

    def test_returns_none_when_day_absent(self, tmp_path):
        self._make_raw(tmp_path, "AKUR202606010000a.T02.gz")
        eng = EposDisseminate(_target(tmp_path))
        assert eng.find_raw_source("AKUR", date(2026, 6, 28)) is None


# --------------------------------------------------------------------------- packaging (.Z, guarded)
def _compress_available() -> bool:
    import shutil as _sh

    return _sh.which("compress") is not None


@pytest.mark.skipif(
    not _compress_available(), reason="needs the `compress` tool for .Z"
)
class TestPackagingZ:
    def test_package_unix_compress(self, tmp_path):
        # Exercise the legacy .Z compression path (no Hatanaka, so no valid-RINEX
        # requirement — this isolates the `compress` step).
        from receivers.dissemination.convert import package

        policy = VersionPolicy(naming="short", hatanaka=False, compression="Z")
        obs = tmp_path / "FIM21280.26o"
        obs.write_bytes(b"     2.11           OBSERVATION DATA\nbody\n")
        pub = package(obs, policy, tmp_path / "out")
        assert pub.name == "FIM21280.26o.Z"
        assert pub.is_file()
