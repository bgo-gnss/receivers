"""EPOS MARKER NUMBER fallback tracks the RINEX version when no DOMES.

DOMES present → MARKER NUMBER is the DOMES. DOMES missing → the
version-appropriate ID (9-char for R3, 4-char for R2), matching MARKER NAME —
a station like ELEY (no IERS DOMES) otherwise got a bare 4-char MARKER NUMBER
even in RINEX 3.
"""

from pathlib import Path

from receivers.dissemination.convert import epos_marker_name, finalize_epos_header

_MINIMAL_HEADER = (
    "     3.04           OBSERVATION DATA    M                   "
    "RINEX VERSION / TYPE\n"
    "                                                            "
    "END OF HEADER\n"
)


def _record(path: Path, label: str) -> str:
    for line in path.read_text(encoding="latin-1").splitlines():
        if line[60:80].strip() == label:
            return line[:60].strip()
    return ""


def _finalize(tmp_path, sid, version, domes=""):
    p = tmp_path / f"{sid.lower()}.rnx"
    p.write_text(_MINIMAL_HEADER, encoding="latin-1")
    finalize_epos_header(
        p, sid, version, country_code="ISL", monument_number="00", domes=domes
    )
    return p


def test_marker_number_r3_no_domes_is_nine_char(tmp_path):
    p = _finalize(tmp_path, "ELEY", 3)
    assert _record(p, "MARKER NUMBER") == "ELEY00ISL"
    # MARKER NAME is 9-char too — the fallback now matches its width.
    assert _record(p, "MARKER NAME") == "ELEY00ISL"


def test_marker_number_r2_no_domes_is_four_char(tmp_path):
    p = _finalize(tmp_path, "ELEY", 2)
    assert _record(p, "MARKER NUMBER") == "ELEY"
    assert _record(p, "MARKER NAME") == "ELEY"


def test_marker_number_uses_domes_when_present(tmp_path):
    p = _finalize(tmp_path, "RHOF", 3, domes="10230M001")
    assert _record(p, "MARKER NUMBER") == "10230M001"
    # MARKER NAME is still the 9-char ID, independent of DOMES.
    assert _record(p, "MARKER NAME") == "RHOF00ISL"


def test_epos_marker_name_width_by_version():
    assert epos_marker_name("ELEY", 3, "ISL", "00") == "ELEY00ISL"
    assert epos_marker_name("ELEY", 2, "ISL", "00") == "ELEY"
