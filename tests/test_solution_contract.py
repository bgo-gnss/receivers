"""Drift guard, receivers half: the strict reader accepts the producer's format.

``tests/data/solution_contract/AFST.imo.24hr_itrf14.pos`` is a byte-for-byte
copy of the file ``geo_dataread.pos.write_pos`` writes, pinned by
``geo_dataread/tests/test_pos_monitor_contract.py``. When that test fails the
producer's format changed; copy its new fixture here and this test says whether
the monitor still reads it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from receivers.monitoring.solutions.pos_reader import ProductSpec, inspect_pos

FIXTURE = (
    Path(__file__).parent / "data" / "solution_contract" / "AFST.imo.24hr_itrf14.pos"
)


def test_strict_reader_accepts_the_producer_contract_fixture():
    insp = inspect_pos(FIXTURE, "AFST", ProductSpec())
    assert (insp.file_fact, insp.file_code) == ("ok", None)
    assert sorted(insp.days) == [dt.date(2026, 11, d) for d in (18, 19, 20)]
    assert {(f.fact, f.code) for f in insp.days.values()} == {("valid", None)}


def test_fixture_is_identical_to_the_geo_dataread_copy_when_both_are_checked_out():
    sibling = (
        Path(__file__).resolve().parents[2]
        / "geo_dataread/tests/data/pos_contract/AFST.imo.24hr_itrf14.pos"
    )
    if not sibling.exists():  # CI checks out receivers alone
        import pytest

        pytest.skip("geo_dataread is not checked out beside receivers")
    assert FIXTURE.read_bytes() == sibling.read_bytes()
