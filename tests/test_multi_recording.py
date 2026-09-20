"""`multi_recording` — days with more than one raw recording.

The regression these lock down: a session-date carrying two DISTINCT recordings
used to convert one and silently count the other as an idempotent "already
staged" resume, losing data while reporting success (measured on RJUC
2015-09-15: 1h 7m of the station's first day). The same code must still treat
`X.T00` + `X.T00.gz` as ONE recording — JOKU alone has ~365 such days, and
merging a file with itself would double every epoch.
"""

from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path

import pytest

# Repo-wide condition, not this file's: the `receivers` package ships no
# `py.typed` and pyproject sets no `mypy_path`, so mypy skips it as untyped.
# All ~193 test modules importing `receivers.*` raise the same
# [import-untyped], including untouched ones (tests/test_archive_audit.py).
# The class-wide fix is a deliberate decision — `mypy_path = "src"` clears it
# but surfaces 582 pre-existing errors across the package — so the ignore stays
# scoped here. Remove it if the package is ever marked PEP 561-typed.
from receivers.rinex.multi_recording import (  # type: ignore[import-untyped]
    collapse_compression_twins,
    group_recordings,
    read_time_span,
    recording_key,
    spans_overlap,
)


def _hdr(value: str, label: str) -> str:
    """A RINEX header line: value in cols 1-60, label in 61-80."""
    return f"{value:<60}{label}\n"


def _write_rinex(path: Path, first: str, last: str | None = None) -> Path:
    lines = [
        _hdr("     3.04           OBSERVATION DATA    M (MIXED)", "RINEX VERSION / TYPE"),
        _hdr("RJUC", "MARKER NAME"),
    ]
    if first:
        lines.append(_hdr(first, "TIME OF FIRST OBS"))
    if last:
        lines.append(_hdr(last, "TIME OF LAST OBS"))
    lines.append(_hdr("", "END OF HEADER"))
    path.write_text("".join(lines), encoding="latin-1")
    return path


# --- compression twins vs distinct recordings ------------------------------


def test_recording_key_ignores_compression() -> None:
    assert recording_key(Path("/a/X.T00")) == recording_key(Path("/a/X.T00.gz"))


def test_collapse_keeps_the_gz_copy_and_the_order() -> None:
    paths = [
        Path("/a/JOKU201901010000a.T00"),
        Path("/a/JOKU201901010000a.T00.gz"),
        Path("/a/JOKU201901020000a.T00"),
        Path("/a/JOKU201901020000a.T00.gz"),
    ]
    assert [p.name for p in collapse_compression_twins(paths)] == [
        "JOKU201901010000a.T00.gz",
        "JOKU201901020000a.T00.gz",
    ]


def test_collapse_never_fuses_two_distinct_recordings() -> None:
    """The RJUC case: different stems on the same date are TWO recordings."""
    paths = [
        Path("/a/RJUC201509150000a.T02"),
        Path("/a/RJUC201509152049a.T02"),
    ]
    assert len(collapse_compression_twins(paths)) == 2


def test_collapse_is_a_noop_on_the_common_single_recording() -> None:
    paths = [Path("/a/RJUC201509150000a.T02")]
    assert collapse_compression_twins(paths) == paths


def test_group_recordings_separates_distinct_windows() -> None:
    paths = [
        Path("/a/RJUC201509150000a.T02"),
        Path("/a/RJUC201509152049a.T02"),
        Path("/a/RJUC201509152049a.T02.gz"),
    ]
    groups = group_recordings(paths)
    assert set(groups) == {"RJUC201509150000a.T02", "RJUC201509152049a.T02"}
    assert len(groups["RJUC201509152049a.T02"]) == 2


# --- the disjointness gate -------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        # the real RJUC pair: 20:49-21:56 and 21:57-23:59
        (
            (datetime(2015, 9, 15, 21, 57, 15), datetime(2015, 9, 15, 23, 59, 45)),
            (datetime(2015, 9, 15, 20, 49, 15), datetime(2015, 9, 15, 21, 56, 45)),
            False,
        ),
        # one second of shared epochs is still overlap
        (
            (datetime(2015, 9, 15, 21, 57, 15), datetime(2015, 9, 15, 23, 59, 45)),
            (datetime(2015, 9, 15, 20, 49, 15), datetime(2015, 9, 15, 21, 57, 15)),
            True,
        ),
        # a truncated re-fetch of the same window
        (
            (datetime(2015, 9, 15, 20, 49, 15), datetime(2015, 9, 15, 23, 59, 45)),
            (datetime(2015, 9, 15, 20, 49, 15), datetime(2015, 9, 15, 21, 0, 0)),
            True,
        ),
        # touching but not overlapping is safe to join
        (
            (datetime(2015, 9, 15, 22, 0, 0), datetime(2015, 9, 15, 23, 0, 0)),
            (datetime(2015, 9, 15, 21, 0, 0), datetime(2015, 9, 15, 22, 0, 0)),
            True,
        ),
    ],
)
def test_spans_overlap(a, b, expected) -> None:
    assert spans_overlap(a, b) is expected


def test_spans_overlap_is_unknown_when_a_span_is_unknown() -> None:
    span = (datetime(2015, 9, 15, 21, 0), datetime(2015, 9, 15, 22, 0))
    assert spans_overlap(span, None) is None
    assert spans_overlap(None, span) is None
    assert spans_overlap(None, None) is None


# --- header span reading --------------------------------------------------


def test_read_time_span_from_a_plain_header(tmp_path: Path) -> None:
    p = _write_rinex(
        tmp_path / "a.15O",
        "  2015     9    15    20    49   15.0000000     GPS",
        "  2015     9    15    21    56   45.0000000     GPS",
    )
    assert read_time_span(p) == (
        datetime(2015, 9, 15, 20, 49, 15),
        datetime(2015, 9, 15, 21, 56, 45),
    )


def test_read_time_span_from_a_gzip_header(tmp_path: Path) -> None:
    plain = _write_rinex(
        tmp_path / "b.15O",
        "  2015     9    15    21    57   15.0000000     GPS",
        "  2015     9    15    23    59   45.0000000     GPS",
    )
    gz = tmp_path / "b.15O.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    assert read_time_span(gz) == (
        datetime(2015, 9, 15, 21, 57, 15),
        datetime(2015, 9, 15, 23, 59, 45),
    )


def test_read_time_span_is_none_without_time_of_last_obs(tmp_path: Path) -> None:
    """RINEX 2 headers carry no TIME OF LAST OBS — the caller must not merge."""
    p = _write_rinex(tmp_path / "c.15O", "  2015     9    15    20    49   15.0000000     GPS")
    assert read_time_span(p) is None


def test_read_time_span_returns_none_on_a_missing_file(tmp_path: Path) -> None:
    assert read_time_span(tmp_path / "nope.15O") is None
