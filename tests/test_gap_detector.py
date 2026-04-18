"""Tests for GapDetector functionality.

Tests gap detection logic without requiring actual database or archive.
"""

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from receivers.health.file_tracker import (
    ArchiveFileChecker,
    FileTracker,
    GapDetector,
    GapInfo,
    SyncResult,
)


class TestGapInfo:
    """Test GapInfo dataclass."""

    def test_gap_info_creation(self):
        """Test creating GapInfo."""
        gap = GapInfo(
            station_id="TEST",
            session_type="15s_24hr",
            file_date=date(2026, 2, 1),
            file_hour=None,
            reason="not_in_archive",
            expected_path="/tmp/test/TEST2026020100a.sbf.gz",
        )

        assert gap.station_id == "TEST"
        assert gap.session_type == "15s_24hr"
        assert gap.file_date == date(2026, 2, 1)
        assert gap.file_hour is None
        assert gap.reason == "not_in_archive"

    def test_gap_info_hourly(self):
        """Test GapInfo for hourly file."""
        gap = GapInfo(
            station_id="ELDC",
            session_type="1Hz_1hr",
            file_date=date(2026, 2, 1),
            file_hour=15,
            reason="not_in_archive",
        )

        assert gap.file_hour == 15
        assert gap.expected_path is None


class TestSyncResult:
    """Test SyncResult dataclass."""

    def test_sync_result_creation(self):
        """Test creating SyncResult."""
        result = SyncResult(
            files_found=10,
            files_added=5,
            files_updated=3,
            files_removed=1,
            errors=0,
        )

        assert result.files_found == 10
        assert result.files_added == 5
        assert result.files_updated == 3
        assert result.files_removed == 1
        assert result.errors == 0


class TestGapDetectorInit:
    """Test GapDetector initialization."""

    def test_init_default(self):
        """Test default initialization."""
        detector = GapDetector()

        assert detector.archive_checker is not None
        assert detector.file_tracker is not None

    def test_init_with_paths(self):
        """Test initialization with custom paths."""
        detector = GapDetector(
            data_prepath="/tmp/gpsdata",
            connection_string="postgresql://localhost/test",
        )

        assert detector.archive_checker.data_prepath == "/tmp/gpsdata"
        assert detector.file_tracker.connection_string == "postgresql://localhost/test"


class TestExpectedFileGeneration:
    """Test expected file generation."""

    def test_daily_files(self):
        """Test generating expected daily files."""
        detector = GapDetector()

        start = date(2026, 2, 1)
        end = date(2026, 2, 3)

        expected = detector._generate_expected_files("TEST", "15s_24hr", start, end)

        assert len(expected) == 3
        assert expected[0] == (date(2026, 2, 1), None)
        assert expected[1] == (date(2026, 2, 2), None)
        assert expected[2] == (date(2026, 2, 3), None)

    def test_hourly_files(self):
        """Test generating expected hourly files."""
        detector = GapDetector()

        start = date(2026, 2, 1)
        end = date(2026, 2, 1)

        expected = detector._generate_expected_files("TEST", "1Hz_1hr", start, end)

        # 1 day * 24 hours = 24 files
        assert len(expected) == 24
        assert expected[0] == (date(2026, 2, 1), 0)
        assert expected[23] == (date(2026, 2, 1), 23)

    def test_multi_day_hourly(self):
        """Test generating hourly files for multiple days."""
        detector = GapDetector()

        start = date(2026, 2, 1)
        end = date(2026, 2, 3)

        expected = detector._generate_expected_files("TEST", "status_1hr", start, end)

        # 3 days * 24 hours = 72 files
        assert len(expected) == 72


class TestArchiveFileCheck:
    """Test archive file checking."""

    def test_file_exists(self):
        """Test detecting file that exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            archive_dir = Path(tmpdir) / "2026" / "feb" / "TEST" / "15s_24hr" / "raw"
            archive_dir.mkdir(parents=True)
            test_file = archive_dir / "TEST202602010000a.sbf.gz"
            test_file.write_bytes(b"test data")

            detector = GapDetector(data_prepath=tmpdir)

            # Mock the build_archive_path to return our test file
            with patch.object(
                detector.archive_checker,
                "build_archive_path",
                return_value=str(test_file),
            ):
                exists, path, size = detector._check_archive_for_file(
                    "TEST", "15s_24hr", date(2026, 2, 1), None
                )

            assert exists is True
            assert path == str(test_file)
            assert size == len(b"test data")

    def test_file_missing(self):
        """Test detecting missing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            detector = GapDetector(data_prepath=tmpdir)

            # Mock to return non-existent path
            with patch.object(
                detector.archive_checker,
                "build_archive_path",
                return_value="/nonexistent/path.sbf.gz",
            ):
                exists, path, size = detector._check_archive_for_file(
                    "TEST", "15s_24hr", date(2026, 2, 1), None
                )

            assert exists is False
            assert path == "/nonexistent/path.sbf.gz"
            assert size is None


class TestFindGaps:
    """Test find_gaps functionality."""

    def test_all_files_exist(self):
        """Test when all files exist - no gaps."""
        detector = GapDetector()

        # Mock archive check to always return True
        with patch.object(
            detector, "_check_archive_for_file", return_value=(True, "/path", 100)
        ), patch.object(
            detector.file_tracker, "connect", return_value=True
        ), patch.object(
            detector, "sync_archive_to_db", return_value=SyncResult(3, 0, 0, 0, 0)
        ):
            gaps = detector.find_gaps(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
                sync_first=True,
            )

        assert len(gaps) == 0

    def test_all_files_missing(self):
        """Test when all files are missing - all gaps."""
        detector = GapDetector()

        # Mock archive check to always return False
        with patch.object(
            detector, "_check_archive_for_file", return_value=(False, "/path", None)
        ), patch.object(
            detector.file_tracker, "connect", return_value=False
        ), patch.object(
            detector, "sync_archive_to_db", return_value=SyncResult(0, 0, 0, 0, 0)
        ):
            gaps = detector.find_gaps(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
                sync_first=True,
            )

        assert len(gaps) == 3
        assert all(g.reason == "not_in_archive" for g in gaps)

    def test_some_files_missing(self):
        """Test when some files are missing."""
        detector = GapDetector()

        # Mock archive check: first file exists, rest missing
        call_count = [0]

        def mock_check(*args):
            call_count[0] += 1
            if call_count[0] == 1:
                return (True, "/path", 100)
            return (False, "/path", None)

        with patch.object(
            detector, "_check_archive_for_file", side_effect=mock_check
        ), patch.object(
            detector.file_tracker, "connect", return_value=False
        ), patch.object(
            detector, "sync_archive_to_db", return_value=SyncResult(1, 0, 0, 0, 0)
        ):
            gaps = detector.find_gaps(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
                sync_first=True,
            )

        # First file exists, 2 missing
        assert len(gaps) == 2

    def test_skip_known_missing_on_receiver(self):
        """Test skipping files known to be missing on receiver."""
        detector = GapDetector()

        with patch.object(
            detector, "_check_archive_for_file", return_value=(False, "/path", None)
        ), patch.object(
            detector.file_tracker, "connect", return_value=True
        ), patch.object(
            detector.file_tracker, "is_file_missing", return_value=True
        ), patch.object(
            detector, "sync_archive_to_db", return_value=SyncResult(0, 0, 0, 0, 0)
        ):
            gaps = detector.find_gaps(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
                skip_missing_on_receiver=True,
            )

        # All files known missing on receiver, so no gaps to download
        assert len(gaps) == 0

    def test_include_known_missing_on_receiver(self):
        """Test including files known to be missing on receiver."""
        detector = GapDetector()

        with patch.object(
            detector, "_check_archive_for_file", return_value=(False, "/path", None)
        ), patch.object(
            detector.file_tracker, "connect", return_value=True
        ), patch.object(
            detector.file_tracker, "is_file_missing", return_value=True
        ), patch.object(
            detector, "sync_archive_to_db", return_value=SyncResult(0, 0, 0, 0, 0)
        ):
            gaps = detector.find_gaps(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
                skip_missing_on_receiver=False,
            )

        # Files included even though known missing
        assert len(gaps) == 3


class TestSyncArchiveToDb:
    """Test sync_archive_to_db functionality."""

    def test_sync_no_db_connection(self):
        """Test sync when database is not available."""
        detector = GapDetector()

        with patch.object(detector.file_tracker, "connect", return_value=False):
            result = detector.sync_archive_to_db(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
            )

        assert result.errors == 1
        assert result.files_found == 0

    def test_sync_with_files(self):
        """Test sync when files exist in archive."""
        detector = GapDetector()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        with patch.object(
            detector.file_tracker, "connect", return_value=True
        ), patch.object(detector.file_tracker, "_conn", mock_conn), patch.object(
            detector,
            "_check_archive_for_file",
            return_value=(True, "/path/test.gz", 100),
        ):
            # Mock cursor.fetchone to return None (new file)
            mock_cursor.fetchone.return_value = None

            result = detector.sync_archive_to_db(
                "TEST",
                "15s_24hr",
                date(2026, 2, 1),
                date(2026, 2, 3),
            )

        assert result.files_found == 3
        assert result.files_added == 3


class TestGapSummary:
    """Test gap summary functionality."""

    def test_gap_summary_single_station(self):
        """Test gap summary for single station."""
        detector = GapDetector()

        with patch.object(
            detector,
            "_generate_expected_files",
            return_value=[(date(2026, 2, 1), None)],
        ), patch.object(detector, "find_gaps", return_value=[]):
            summary = detector.get_gap_summary(
                ["TEST"],
                "15s_24hr",
                days_back=1,
            )

        assert summary["total_expected"] == 1
        assert summary["total_gaps"] == 0
        assert "TEST" in summary["stations"]

    def test_gap_summary_multiple_stations(self):
        """Test gap summary for multiple stations."""
        detector = GapDetector()

        with patch.object(
            detector,
            "_generate_expected_files",
            return_value=[(date(2026, 2, 1), None)],
        ), patch.object(
            detector,
            "find_gaps",
            return_value=[
                GapInfo("TEST1", "15s_24hr", date(2026, 2, 1), None, "not_in_archive")
            ],
        ):
            summary = detector.get_gap_summary(
                ["TEST1", "TEST2"],
                "15s_24hr",
                days_back=1,
            )

        assert summary["total_expected"] == 2
        assert summary["total_gaps"] == 2  # Both stations have 1 gap each


class TestContextManager:
    """Test context manager functionality."""

    def test_context_manager(self):
        """Test using GapDetector as context manager."""
        with patch.object(FileTracker, "close") as mock_close:
            with GapDetector() as detector:
                assert detector is not None

            mock_close.assert_called_once()
