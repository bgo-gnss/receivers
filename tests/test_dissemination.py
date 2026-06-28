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
    DisseminationTarget,
    load_dissemination_config,
)
from receivers.dissemination.convert import (
    _crinex_to_obs_name,
    _is_hatanaka,
    _strip_compression,
    cache_key,
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
        country_code="ISL",
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
      rinex_version: 3
      naming: long
      country_code: ISL
"""
        )
        targets = load_dissemination_config(cfg)
        assert [t.name for t in targets] == ["epos"]  # archive tier filtered out
        t = targets[0]
        assert t.active is True
        assert t.country_code == "ISL"
        assert t.rinex_version == 3
        assert t.naming == "long"
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
    path: Path, marker: str, *, xyz: str = "", receiver: str = ""
) -> Path:
    """Write a minimal RINEX-3 header file (enough for extract_header_info)."""
    lines = [
        "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE",
        f"{marker:<60}MARKER NAME",
    ]
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


@pytest.mark.skipif(
    not (_tools_available() and _SAMPLE.is_file()),
    reason="needs gps-tools binaries and a real archived RINEX sample",
)
class TestEndToEnd:
    def test_convert_produces_valid_rinex3_long_name(self, tmp_path):
        from receivers.dissemination.convert import convert_to_rinex3_long

        res = convert_to_rinex3_long(
            _SAMPLE,
            "FIM2",
            datetime(2026, 5, 8),
            country_code="ISL",
            cache_dir=tmp_path / "cache",
        )
        assert res.long_name == "FIM200ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.output_path.is_file()
        assert res.cached is False
        # RINEX 3.04 header line.
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_second_convert_is_cache_hit(self, tmp_path):
        from receivers.dissemination.convert import convert_to_rinex3_long

        kw = dict(country_code="ISL", cache_dir=tmp_path / "cache")
        first = convert_to_rinex3_long(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        second = convert_to_rinex3_long(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        assert first.cached is False
        assert second.cached is True

    def test_run_one_pushes_to_local_dest(self, tmp_path):
        (tmp_path / "archive" / "2026" / "may" / "FIM2" / "15s_24hr" / "rinex").mkdir(
            parents=True
        )
        # symlink the real sample into the synthetic archive layout
        link = tmp_path / "archive/2026/may/FIM2/15s_24hr/rinex/FIM21280.26d.gz"
        os.symlink(_SAMPLE, link)
        eng = EposDisseminate(_target(tmp_path))
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == "FIM200ISL_R_20261280000_01D_15S_MO.rnx"
        staged = tmp_path / "stage" / r.long_name
        assert staged.is_file()

    def _archive_with_sample(self, tmp_path):
        rinex_dir = tmp_path / "archive/2026/may/FIM2/15s_24hr/rinex"
        rinex_dir.mkdir(parents=True)
        os.symlink(_SAMPLE, rinex_dir / "FIM21280.26d.gz")

    def test_qc_gate_passes_then_pushes(self, tmp_path):
        self._archive_with_sample(tmp_path)
        # The converted header carries MARKER NAME FIM2 (from the source file).
        provider = lambda station, dt: {"marker": "FIM2"}  # noqa: E731
        eng = EposDisseminate(
            _target(tmp_path), session_provider=provider, set_header=False
        )
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.qc_passed is True
        assert r.ok is True
        assert (tmp_path / "stage" / r.long_name).is_file()

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
        # Nothing pushed when the gate fails.
        assert not (tmp_path / "stage").exists() or not list(
            (tmp_path / "stage").iterdir()
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
    def test_raw_t02_converts_to_rinex3_long(self, tmp_path):
        from receivers.dissemination.convert import convert_raw_to_rinex3_long

        res = convert_raw_to_rinex3_long(
            _RAW_SAMPLE, "RHOF", datetime(2026, 5, 8), cache_dir=tmp_path / "cache"
        )
        assert res.long_name == "RHOF00ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.output_path.is_file()
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_engine_prefers_rinex_else_raw(self, tmp_path):
        # Only raw present → engine falls back to the raw path.
        raw_dir = tmp_path / "archive/2026/may/RHOF/15s_24hr/raw"
        raw_dir.mkdir(parents=True)
        os.symlink(_RAW_SAMPLE, raw_dir / _RAW_SAMPLE.name)
        eng = EposDisseminate(_target(tmp_path))  # no QC provider
        r = eng.run_one("RHOF", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == "RHOF00ISL_R_20261280000_01D_15S_MO.rnx"
        assert (tmp_path / "stage" / r.long_name).is_file()
