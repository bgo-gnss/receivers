"""Backfill RINEX must convert the raw file IN THE ARCHIVE, not the path the
download reported.

Regression for the rek-d01 failure (2026-11-16, ~35/day since at least
2026-11-08): the drivers report the temporary download path
(``/tmp/gps_receivers/SVIN/15s_24hr/SVIN3180.26_.gz``). Archiving has already
moved that file, so every backfilled day failed with "Input file not found" and
got no RINEX. Both the round-robin backfill and the long-term backfill go
through ``_backfill_station_day_generic``.

The test drives the REAL call chain — ``_backfill_station_day_generic`` →
``_run_backfill_rinex`` → the reconciler's per-station routine — and fakes
only the edges: the download (returns the vanished tmp path), the database, and
the converter (records what it was asked to convert).
"""

from __future__ import annotations

import gzip
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

DAY = date(2026, 11, 14)


@contextmanager
def _fake_db():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    yield conn


def _run_backfill(tmp_path: Path, session: str, raw_names: list[str]):
    """Run one backfill day; return the paths the converter was asked for."""
    from receivers.scheduling.backfill import _backfill_station_day_generic

    raw_dir = tmp_path / "2026" / "nov" / "SVIN" / session / "raw"
    raw_dir.mkdir(parents=True)
    for name in raw_names:
        with gzip.open(raw_dir / name, "wb") as fh:
            fh.write(b"SBF payload")

    # What the driver really returns: tmp download paths that no longer exist.
    vanished = [str(tmp_path / "tmp" / "gps_receivers" / "SVIN3180.26_.gz")]
    assert not Path(vanished[0]).exists()

    converted: list[Path] = []

    class FakeConverter:
        def __init__(self, station_id, session_type):
            pass

        def convert_file(self, raw_path, output_dir):
            assert Path(raw_path).exists(), f"asked to convert missing {raw_path}"
            converted.append(Path(raw_path))
            return SimpleNamespace(success=True, rinex_file=None, message="ok")

    with (
        patch(
            "receivers.scheduling.backfill._download_day_generic",
            return_value={
                "status": "completed",
                "files_downloaded": len(vanished),
                "downloaded_files": vanished,
            },
        ),
        patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            _fake_db,
        ),
        patch(
            "receivers.cli.main.get_station_config",
            return_value={"receiver_type": "PolaRX5"},
        ),
        patch(
            "receivers.health.file_tracker.ArchiveFileChecker.__init__",
            return_value=None,
        ),
        patch(
            "receivers.health.file_tracker.ArchiveFileChecker.get_archive_directory",
            return_value=str(raw_dir),
        ),
        patch(
            "receivers.scheduling.archive_reconciler._get_format_resolver",
            return_value=None,
        ),
        patch(
            "receivers.scheduling.archive_reconciler._load_tracked_rinex",
            return_value=set(),
        ),
        patch(
            "receivers.config.receiver_registry.get_converter_class",
            return_value=FakeConverter,
        ),
    ):
        _backfill_station_day_generic(
            "SVIN",
            DAY,
            DAY,
            session,
            immediate_archive=True,
            run_rinex=True,
        )
    return raw_dir, converted


@pytest.mark.unit
def test_daily_backfill_converts_the_archived_raw(tmp_path):
    raw_dir, converted = _run_backfill(
        tmp_path, "15s_24hr", ["SVIN202611140000a.sbf.gz"]
    )
    assert converted == [raw_dir / "SVIN202611140000a.sbf.gz"]


@pytest.mark.unit
def test_hourly_backfill_converts_every_archived_hour(tmp_path):
    names = [f"SVIN20261114{h:02d}00b.sbf.gz" for h in (2, 3, 23)]
    raw_dir, converted = _run_backfill(tmp_path, "1Hz_1hr", names)
    assert sorted(converted) == sorted(raw_dir / n for n in names)


@pytest.mark.unit
def test_hour_with_rinex_already_is_not_reconverted(tmp_path):
    names = ["SVIN202611140200b.sbf.gz", "SVIN202611140300b.sbf.gz"]
    rinex_dir = tmp_path / "2026" / "nov" / "SVIN" / "1Hz_1hr" / "rinex"
    rinex_dir.mkdir(parents=True)
    with (
        patch(
            "receivers.scheduling.archive_reconciler._find_rinex_file",
            side_effect=lambda raw: (
                rinex_dir / "SVIN318c.26D.Z" if "0200b" in raw.name else None
            ),
        ),
        patch("receivers.scheduling.archive_reconciler._ensure_rinex_tracked"),
    ):
        raw_dir, converted = _run_backfill(tmp_path, "1Hz_1hr", names)
    assert converted == [raw_dir / "SVIN202611140300b.sbf.gz"]
