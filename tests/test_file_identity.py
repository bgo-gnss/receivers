"""Tests for the report-only archive identity probe (archive/file_identity).

Covers the two integrity checks that the FAGC episode motivated:
* stacked/multi-document detection (the NYLA late-2022 files), and
* stray/wrong-station detection (position decides identity).
"""

from pathlib import Path

import pytest

from receivers.archive import file_identity as fi

# A two-station fleet, far enough apart that a position at one is
# unambiguously NOT the other (real stations sit kilometres apart).
FLEET = {"AAAA": (64.00, -16.00), "BBBB": (65.50, -20.00)}


def _ecef(lat: float, lon: float, h: float = 100.0):
    pyproj = pytest.importorskip("pyproj")
    tr = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    x, y, z = tr.transform(lon, lat, h)
    return (x, y, z)


# ------------------------------------------------------------- count_documents


def test_count_documents_single_vs_stacked():
    assert fi.count_documents("... MARKER NAME\n... END OF HEADER\n") == 1
    two = "A MARKER NAME\nEND OF HEADER\nB MARKER NAME\nEND OF HEADER\n"
    assert fi.count_documents(two) == 2
    assert fi.count_documents("no header here") == 0


# ------------------------------------------------------- parse_first_approx_xyz


def test_parse_first_approx_xyz():
    line = (
        "  2588166.9242 -1084675.5099  5708490.2760"
        "                  APPROX POSITION XYZ\n"
    )
    assert fi.parse_first_approx_xyz(line) == (2588166.9242, -1084675.5099, 5708490.276)


def test_parse_first_approx_xyz_absent_or_malformed():
    assert fi.parse_first_approx_xyz("nothing here") is None
    assert fi.parse_first_approx_xyz("  1.0 2.0  APPROX POSITION XYZ") is None  # <3 vals


def test_parse_first_approx_xyz_takes_first():
    text = (
        "  1.0 2.0 3.0   APPROX POSITION XYZ\n"
        "  9.0 9.0 9.0   APPROX POSITION XYZ\n"
    )
    assert fi.parse_first_approx_xyz(text) == (1.0, 2.0, 3.0)


# --------------------------------------------------------- classify_position


def test_classify_confirmed_at_own_mark():
    xyz = _ecef(*FLEET["AAAA"])
    # Filed under AAAA, position is AAAA → confirmed (None).
    assert fi.classify_position(xyz, "AAAA", FLEET, gate_m=10.0) is None


def test_classify_stray_position_belongs_to_other_station():
    xyz = _ecef(*FLEET["AAAA"])
    # Filed under BBBB but the position is AAAA's → stray, nearest is AAAA.
    verdict = fi.classify_position(xyz, "BBBB", FLEET, gate_m=10.0)
    assert verdict is not None
    nearest, near_d, exp_d = verdict
    assert nearest == "AAAA"
    assert near_d < 100.0  # essentially at AAAA
    assert exp_d > 100_000.0  # BBBB is far away


def test_classify_no_position_is_none():
    assert fi.classify_position(None, "AAAA", FLEET, gate_m=10.0) is None
    assert fi.classify_position((0.0, 0.0, 0.0), "AAAA", FLEET, gate_m=10.0) is None


def test_classify_station_not_in_fleet_is_none():
    xyz = _ecef(*FLEET["AAAA"])
    assert fi.classify_position(xyz, "ZZZZ", FLEET, gate_m=10.0) is None


def test_classify_noisy_but_same_station_is_none():
    # 200 m north of AAAA's mark: beyond a 10 m gate, but AAAA is still the
    # nearest station (BBBB is >100 km away) → not a stray.
    xyz = _ecef(FLEET["AAAA"][0] + 0.0018, FLEET["AAAA"][1])
    assert fi.classify_position(xyz, "AAAA", FLEET, gate_m=10.0) is None


# ------------------------------------------------------------ probe_rinex_file


def _write_rinex(path: Path, *, docs: int, lat: float, lon: float) -> None:
    """Write a minimal plaintext RINEX with ``docs`` concatenated headers."""
    x, y, z = _ecef(lat, lon)
    block = (
        "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
        "TEST                                                        MARKER NAME\n"
        f"  {x:.4f}  {y:.4f}  {z:.4f}                  APPROX POSITION XYZ\n"
        "                                                            END OF HEADER\n"
    )
    path.write_text(block * docs)


def test_probe_flags_stacked_and_stray(tmp_path):
    # A file whose position is AAAA's, filed under BBBB, and stacked ×3.
    f = tmp_path / "BBBB0010.24o.rnx"
    _write_rinex(f, docs=3, lat=FLEET["AAAA"][0], lon=FLEET["AAAA"][1])
    kinds = {
        x.kind for x in fi.probe_rinex_file(f, "BBBB", fleet=FLEET, gate_m=10.0)
    }
    assert kinds == {"stacked", "stray"}


def test_probe_clean_file_no_findings(tmp_path):
    f = tmp_path / "AAAA0010.24o.rnx"
    _write_rinex(f, docs=1, lat=FLEET["AAAA"][0], lon=FLEET["AAAA"][1])
    assert fi.probe_rinex_file(f, "AAAA", fleet=FLEET, gate_m=10.0) == []


def test_probe_unreadable_file_is_silent(tmp_path):
    f = tmp_path / "AAAA0010.24o.rnx"
    f.write_bytes(b"\x00\x01\x02not a rinex")
    # No crash, no false findings on garbage.
    assert fi.probe_rinex_file(f, "AAAA", fleet=FLEET, gate_m=10.0) == []
