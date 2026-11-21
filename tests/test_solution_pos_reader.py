"""Strict POS reader: every way a file can be wrong gets its own code."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from receivers.monitoring.solutions.pos_reader import (
    ProductSpec,
    inspect_pos,
    row_fingerprint,
)
from tests.solution_pos_helpers import row, write_pos

SPEC = ProductSpec()
D1 = dt.date(2026, 11, 18)
D2 = dt.date(2026, 11, 19)
D3 = dt.date(2026, 11, 20)


def _inspect(path: Path, station: str = "AFST"):
    return inspect_pos(path, station, SPEC)


def test_valid_file_reports_each_day_valid_with_fingerprint(tmp_path):
    p = write_pos(tmp_path / "AFST.imo.24hr_itrf14.pos", [D1, D2, D3])
    insp = _inspect(p)
    assert insp.file_fact == "ok" and insp.file_code is None
    assert set(insp.days) == {D1, D2, D3}
    assert all(f.fact == "valid" for f in insp.days.values())
    assert insp.days[D1].fingerprint == row_fingerprint(row(D1))
    assert insp.sha256 is not None and insp.size == p.stat().st_size


def test_repadding_a_row_does_not_change_its_fingerprint():
    assert row_fingerprint(row(D1)) == row_fingerprint(
        "   " + row(D1).replace(" ", "  ")
    )


def test_a_changed_value_changes_the_fingerprint():
    assert row_fingerprint(row(D1)) != row_fingerprint(row(D1, x="2598588.29094"))


def test_sparse_and_unsorted_rows_are_fine(tmp_path):
    p = write_pos(tmp_path / "f.pos", [], rows=[row(D3), "", row(D1)])
    insp = _inspect(p)
    assert set(insp.days) == {D1, D3}  # D2 simply absent, not an error
    assert all(f.fact == "valid" for f in insp.days.values())


@pytest.mark.parametrize("dupe", ["identical", "different"])
def test_duplicate_dates_are_invalid_even_when_identical(tmp_path, dupe):
    second = row(D1) if dupe == "identical" else row(D1, x="2598588.30000")
    p = write_pos(tmp_path / "f.pos", [], rows=[row(D1), second, row(D2)])
    insp = _inspect(p)
    assert insp.days[D1].fact == "invalid"
    assert insp.days[D1].code == "duplicate_date"
    assert insp.days[D1].fingerprint is None
    assert insp.days[D2].fact == "valid"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"x": "**************"}, "nonfinite_xyz"),
        ({"z": "nan"}, "nonfinite_xyz"),
        ({"y": "inf"}, "nonfinite_xyz"),
        ({"sigma_x": "inf"}, "nonfinite_value"),
        ({"sigma_x": "abc"}, "bad_number"),
        ({"mjd": "**********"}, "bad_number"),
        ({"soln": "08hrs"}, "session_mismatch"),
        ({"soln": "final"}, "session_mismatch"),
        ({"time": "120000"}, "unexpected_epoch_time"),
        ({"time": "11:59"}, "bad_epoch_time"),
        ({"mjd": "61360.4993"}, "mjd_mismatch"),
        ({"extra": " surplus"}, "bad_column_count"),
    ],
)
def test_row_level_problems_invalidate_only_that_day(tmp_path, kwargs, code):
    p = write_pos(tmp_path / "f.pos", [], rows=[row(D1, **kwargs), row(D2)])
    insp = _inspect(p)
    assert insp.file_fact == "ok"
    assert (insp.days[D1].fact, insp.days[D1].code) == ("invalid", code)
    assert insp.days[D2].fact == "valid"


def test_fortran_overflow_outside_xyz_is_tolerated(tmp_path):
    p = write_pos(tmp_path / "f.pos", [], rows=[row(D1, dn="*********")])
    assert _inspect(p).days[D1].fact == "valid"


def test_a_short_row_is_invalid(tmp_path):
    short = " ".join(row(D1).split()[:20])
    p = write_pos(tmp_path / "f.pos", [], rows=[short])
    assert _inspect(p).days[D1].code == "bad_column_count"


@pytest.mark.parametrize("bad", ["2026111", "20261131", "XXXX1120", "*comment"])
def test_an_undatable_row_makes_the_whole_file_unverifiable(tmp_path, bad):
    p = write_pos(tmp_path / "f.pos", [], rows=[row(D1), f" {bad} 115900 1 2 3"])
    insp = _inspect(p)
    assert (insp.file_fact, insp.file_code) == ("unverifiable", "undatable_row")
    assert insp.days == {}


@pytest.mark.parametrize(
    ("header_kw", "station", "code"),
    [
        ({"frame": "ITRF2014"}, "AFST", "frame_mismatch"),
        ({"frame": "IGb20"}, "AFST", "frame_mismatch"),
        ({"sid": ""}, "AFST", "missing_station_id"),
        ({"sid": "THOB"}, "AFST", "station_mismatch"),
    ],
)
def test_header_problems_invalidate_the_whole_file(tmp_path, header_kw, station, code):
    p = write_pos(tmp_path / "f.pos", [D1], **header_kw)
    insp = _inspect(p, station)
    assert (insp.file_fact, insp.file_code) == ("invalid", code)
    assert insp.sha256 is not None


def test_station_identity_is_never_taken_from_the_filename(tmp_path):
    # geo_dataread's reader falls back to path.name[:4]; this one must not.
    p = write_pos(tmp_path / "AFST.imo.24hr_itrf14.pos", [D1], sid="")
    assert _inspect(p).file_code == "missing_station_id"


def test_not_a_pbo_file(tmp_path):
    p = tmp_path / "f.pos"
    p.write_text("hello\n")
    assert _inspect(p).file_code == "not_pbo"


def test_empty_file_is_not_pbo(tmp_path):
    p = tmp_path / "f.pos"
    p.write_text("")
    assert _inspect(p).file_code == "not_pbo"


def test_missing_column_header(tmp_path):
    p = tmp_path / "f.pos"
    p.write_text(
        "PBO Station Position Time Series. Reference Frame : IGb14\n"
        "4-character ID: AFST\n" + row(D1) + "\n"
    )
    assert _inspect(p).file_code == "no_column_header"


def test_non_ascii_content_is_invalid(tmp_path):
    p = write_pos(tmp_path / "f.pos", [D1])
    p.write_bytes(p.read_bytes() + "Þ\n".encode())
    assert _inspect(p).file_code == "not_ascii"


def test_unreadable_file_is_unverifiable_not_invalid(tmp_path):
    p = tmp_path / "dir.pos"
    p.mkdir()  # reading a directory raises IsADirectoryError (an OSError)
    insp = _inspect(p)
    assert (insp.file_fact, insp.file_code) == ("unverifiable", "unreadable")
    assert insp.sha256 is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_permission_denied_is_unverifiable(tmp_path):
    p = write_pos(tmp_path / "f.pos", [D1])
    p.chmod(0)
    try:
        assert _inspect(p).file_code == "unreadable"
    finally:
        p.chmod(0o644)


def test_header_only_file_has_no_days(tmp_path):
    p = write_pos(tmp_path / "f.pos", [], rows=[])
    insp = _inspect(p)
    assert insp.file_fact == "ok" and insp.days == {}


def test_frame_label_is_taken_from_the_spec_not_hardcoded(tmp_path):
    p = write_pos(tmp_path / "f.pos", [D1], frame="ITRF2020")
    spec = ProductSpec(stream="imo.24hr_itrf20", frame_label="ITRF2020")
    assert inspect_pos(p, "AFST", spec).days[D1].fact == "valid"
