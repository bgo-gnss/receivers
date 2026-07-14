"""EPOS MARKER NUMBER carries the IERS DOMES only, else the line is stripped.

DOMES present → MARKER NUMBER is the DOMES. DOMES missing → no MARKER NUMBER
line at all (never the station id, which MARKER NAME already carries). A station
like ELEY (no IERS DOMES) gets no MARKER NUMBER record in any RINEX version.
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


def _has_record(path: Path, label: str) -> bool:
    return any(
        line[60:80].strip() == label
        for line in path.read_text(encoding="latin-1").splitlines()
    )


def test_marker_number_r3_no_domes_is_absent(tmp_path):
    p = _finalize(tmp_path, "ELEY", 3)
    assert not _has_record(p, "MARKER NUMBER")
    # MARKER NAME is still written (the 9-char ID).
    assert _record(p, "MARKER NAME") == "ELEY00ISL"


def test_marker_number_r2_no_domes_is_absent(tmp_path):
    p = _finalize(tmp_path, "ELEY", 2)
    assert not _has_record(p, "MARKER NUMBER")
    assert _record(p, "MARKER NAME") == "ELEY"


def test_marker_number_non_domes_value_is_stripped(tmp_path):
    # A 4-char id passed as domes is not a real DOMES → no MARKER NUMBER line.
    p = _finalize(tmp_path, "ELEY", 3, domes="ELEY")
    assert not _has_record(p, "MARKER NUMBER")


def test_existing_marker_number_line_stripped_when_no_domes(tmp_path):
    # A pre-existing MARKER NUMBER line (e.g. from the decoder) is removed when
    # the station has no DOMES — "actively strip", not "leave whatever's there".
    p = tmp_path / "eley.rnx"
    p.write_text(
        "     3.04           OBSERVATION DATA    M                   "
        "RINEX VERSION / TYPE\n"
        "ELEY                                                        "
        "MARKER NUMBER\n"
        "                                                            "
        "END OF HEADER\n",
        encoding="latin-1",
    )
    finalize_epos_header(
        p, "ELEY", 3, country_code="ISL", monument_number="00", domes=""
    )
    assert not _has_record(p, "MARKER NUMBER")


def test_marker_number_uses_domes_when_present(tmp_path):
    p = _finalize(tmp_path, "RHOF", 3, domes="10230M001")
    assert _record(p, "MARKER NUMBER") == "10230M001"
    # MARKER NAME is still the 9-char ID, independent of DOMES.
    assert _record(p, "MARKER NAME") == "RHOF00ISL"


def test_epos_marker_name_width_by_version():
    assert epos_marker_name("ELEY", 3, "ISL", "00") == "ELEY00ISL"
    assert epos_marker_name("ELEY", 2, "ISL", "00") == "ELEY"
