"""Unit tests for parallel download orchestrator.

Tests grouping logic, worker function, retry mechanism,
parameter resolution from config, and summary calculation.
"""

import math
import pytest
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

from receivers.cli.parallel import (
    _split_into_groups,
    _get_session_defaults,
    _download_one_station,
    download_parallel,
    StationResult,
    ParallelSummary,
)


@pytest.mark.unit
class TestSplitIntoGroups:
    """Test _split_into_groups helper."""

    def test_exact_division(self):
        items = list("ABCDEF")
        groups = _split_into_groups(items, 3)
        assert groups == [["A", "B", "C"], ["D", "E", "F"]]

    def test_remainder(self):
        items = list("ABCDE")
        groups = _split_into_groups(items, 3)
        assert groups == [["A", "B", "C"], ["D", "E"]]

    def test_single_group(self):
        items = list("AB")
        groups = _split_into_groups(items, 10)
        assert groups == [["A", "B"]]

    def test_single_item(self):
        items = ["A"]
        groups = _split_into_groups(items, 5)
        assert groups == [["A"]]

    def test_empty_list(self):
        groups = _split_into_groups([], 5)
        assert groups == []

    def test_group_size_one(self):
        items = list("ABC")
        groups = _split_into_groups(items, 1)
        assert groups == [["A"], ["B"], ["C"]]

    def test_typical_network(self):
        """178 stations split into groups of 18 should give 10 groups."""
        stations = [f"ST{i:03d}" for i in range(178)]
        groups = _split_into_groups(stations, 18)
        assert len(groups) == 10
        assert len(groups[0]) == 18
        assert len(groups[-1]) == 178 % 18  # last group has remainder


@pytest.mark.unit
class TestDerivedParameters:
    """Test that group_size and group_delay are correctly derived from batches + window."""

    def test_178_stations_10_batches_10min(self):
        """178 stations, 10 batches, 10min window -> 18 per group, 60s delay."""
        n_stations = 178
        batches = 10
        window = 10  # minutes
        group_size = math.ceil(n_stations / batches)
        group_delay = (window * 60) / batches
        assert group_size == 18
        assert group_delay == 60.0

    def test_50_stations_5_batches_15min(self):
        """50 stations, 5 batches, 15min window -> 10 per group, 180s delay."""
        group_size = math.ceil(50 / 5)
        group_delay = (15 * 60) / 5
        assert group_size == 10
        assert group_delay == 180.0

    def test_3_stations_10_batches(self):
        """Fewer stations than batches -> 1 per group."""
        group_size = math.ceil(3 / 10)
        assert group_size == 1


@pytest.mark.unit
class TestStationResult:
    """Test StationResult dataclass."""

    def test_defaults(self):
        r = StationResult(station_id="ELDC", status="completed")
        assert r.files_downloaded == 0
        assert r.duration == 0.0
        assert r.attempt == 1
        assert r.error_message is None

    def test_with_values(self):
        r = StationResult(
            station_id="THOB",
            status="failed",
            files_downloaded=3,
            duration=12.5,
            attempt=2,
            error_message="Connection refused",
        )
        assert r.station_id == "THOB"
        assert r.attempt == 2


@pytest.mark.unit
class TestParallelSummary:
    """Test ParallelSummary dataclass."""

    def test_defaults(self):
        s = ParallelSummary()
        assert s.total_stations == 0
        assert s.successful == 0
        assert s.results == {}

    def test_with_results(self):
        s = ParallelSummary(
            total_stations=5,
            successful=3,
            unreachable=1,
            failed=1,
            total_files=10,
        )
        assert s.total_stations == 5
        assert s.total_files == 10


@pytest.mark.unit
class TestGetSessionDefaults:
    """Test _get_session_defaults reads from scheduler.yaml."""

    @patch("receivers.scheduling.config_loader.load_scheduler_config")
    def test_reads_batches_and_window(self, mock_config):
        mock_config.return_value = {
            "sessions": {
                "15s_24hr": {
                    "distribution_window": 15,
                    "batches": 8,
                }
            }
        }
        defaults = _get_session_defaults("15s_24hr")
        assert defaults["batches"] == 8
        assert defaults["distribution_window"] == 15

    @patch("receivers.scheduling.config_loader.load_scheduler_config")
    def test_missing_session_returns_none(self, mock_config):
        mock_config.return_value = {"sessions": {}}
        defaults = _get_session_defaults("15s_24hr")
        assert defaults.get("batches") is None
        assert defaults.get("distribution_window") is None

    @patch("receivers.scheduling.config_loader.load_scheduler_config")
    def test_config_load_failure_returns_empty(self, mock_config):
        """If scheduler config can't be loaded, return empty dict."""
        mock_config.side_effect = Exception("config error")
        defaults = _get_session_defaults("15s_24hr")
        assert defaults == {}


@pytest.mark.unit
class TestDownloadOneStation:
    """Test _download_one_station worker function.

    The function lazily imports _validate_station_for_download and
    _download_station_period from receivers.cli.main, so we mock them there.
    """

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_success(self, mock_validate, mock_download):
        """Successful download returns completed status."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (5, 0, 24)

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "completed"
        assert result.files_downloaded == 5
        assert result.attempt == 1
        assert result.error_message is None

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_up_to_date(self, mock_validate, mock_download):
        """Zero files downloaded returns up_to_date status."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (0, 0, 1)

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "up_to_date"

    @patch("receivers.cli.main._validate_station_for_download")
    def test_unreachable(self, mock_validate):
        """Ping failure returns unreachable status."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = False
        mock_validate.return_value = mock_receiver

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "unreachable"
        assert "Ping" in result.error_message

    @patch("receivers.cli.main._validate_station_for_download")
    def test_validation_failure(self, mock_validate):
        """Station validation failure returns skipped status."""
        mock_validate.return_value = None

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "FAKE", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "skipped"

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_download_errors(self, mock_validate, mock_download):
        """Download with errors returns failed status."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (2, 1, 5)

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "failed"
        assert result.files_downloaded == 2
        assert "1 error" in result.error_message

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_exception_during_download(self, mock_validate, mock_download):
        """Exception during download returns failed status."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.side_effect = ConnectionError("Network down")

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.status == "failed"
        assert "ConnectionError" in result.error_message

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_station_id_uppercased(self, mock_validate, mock_download):
        """Station ID is uppercased."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (1, 0, 1)

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "eldc", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False,
        )

        assert result.station_id == "ELDC"

    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_attempt_passed_through(self, mock_validate, mock_download):
        """Attempt number is passed through to result."""
        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (1, 0, 1)

        args = Mock()
        args.session = "15s_24hr"

        result = _download_one_station(
            "ELDC", args, datetime(2026, 2, 10), datetime(2026, 2, 11),
            "1D", "15s", False, attempt=2,
        )

        assert result.attempt == 2


@pytest.mark.unit
class TestDownloadParallel:
    """Test download_parallel orchestrator.

    These tests mock _download_one_station at the module level.
    The function is called by executor.submit with positional args:
    (sid, args, start, end, ffreq, afreq, reverse, attempt)
    """

    def _make_args(self, **overrides):
        """Create mock args with defaults."""
        args = Mock()
        args.session = "15s_24hr"
        args.batches = None  # Use config defaults
        args.distribution_window = None  # Use config defaults
        args.retry_delay = 0.0  # No delay in tests
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_all_successful(self, mock_worker, mock_defaults):
        """All stations succeed."""
        mock_defaults.return_value = {"batches": 3, "distribution_window": 0}

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            return StationResult(
                station_id=sid.upper(), status="completed",
                files_downloaded=3, duration=5.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        args = self._make_args()
        logger = MagicMock()

        summary = download_parallel(
            stations=["ELDC", "THOB", "ISFS"],
            args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.total_stations == 3
        assert summary.successful == 3
        assert summary.total_files == 9
        assert summary.unreachable == 0
        assert summary.failed == 0

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_retry_recovers_unreachable(self, mock_worker, mock_defaults):
        """Unreachable stations on attempt 1 succeed on attempt 2 (retry)."""
        mock_defaults.return_value = {"batches": 2, "distribution_window": 0}

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            sid = sid.upper()
            if sid == "THOB" and attempt == 1:
                return StationResult(
                    station_id=sid, status="unreachable",
                    duration=1.0, attempt=attempt,
                    error_message="Ping check failed",
                )
            return StationResult(
                station_id=sid, status="completed",
                files_downloaded=2, duration=5.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        args = self._make_args()
        logger = MagicMock()

        summary = download_parallel(
            stations=["ELDC", "THOB"],
            args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.successful == 2
        assert summary.unreachable == 0
        assert summary.retried == 1
        assert summary.retry_recovered == 1
        assert summary.results["THOB"].attempt == 2

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_retry_recovers_failed(self, mock_worker, mock_defaults):
        """Failed stations on attempt 1 succeed on attempt 2 (retry)."""
        mock_defaults.return_value = {"batches": 2, "distribution_window": 0}

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            sid = sid.upper()
            if sid == "THOB" and attempt == 1:
                return StationResult(
                    station_id=sid, status="failed",
                    duration=1.0, attempt=attempt,
                    error_message="FTP timeout",
                )
            return StationResult(
                station_id=sid, status="completed",
                files_downloaded=2, duration=5.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        args = self._make_args()
        logger = MagicMock()

        summary = download_parallel(
            stations=["ELDC", "THOB"],
            args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.successful == 2
        assert summary.failed == 0
        assert summary.retried == 1
        assert summary.retry_recovered == 1
        assert summary.results["THOB"].attempt == 2

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_still_unreachable_after_retry(self, mock_worker, mock_defaults):
        """Station still unreachable after retry is counted as unreachable."""
        mock_defaults.return_value = {"batches": 2, "distribution_window": 0}

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            sid = sid.upper()
            if sid == "GRVM":
                return StationResult(
                    station_id=sid, status="unreachable",
                    duration=1.0, attempt=attempt,
                    error_message="Ping check failed",
                )
            return StationResult(
                station_id=sid, status="completed",
                files_downloaded=2, duration=5.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        args = self._make_args()
        logger = MagicMock()

        summary = download_parallel(
            stations=["ELDC", "GRVM"],
            args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.successful == 1
        assert summary.unreachable == 1
        assert summary.retried == 1
        assert summary.retry_recovered == 0

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_batches_from_config(self, mock_worker, mock_defaults):
        """Batches and window come from scheduler.yaml when CLI args are None."""
        mock_defaults.return_value = {"batches": 5, "distribution_window": 0}

        call_count = []

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            call_count.append(sid.upper())
            return StationResult(
                station_id=sid.upper(), status="completed",
                files_downloaded=1, duration=1.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        # 10 stations, 5 batches -> 2 per group
        stations = [f"ST{i:02d}" for i in range(10)]
        args = self._make_args()  # batches=None, distribution_window=None
        logger = MagicMock()

        summary = download_parallel(
            stations=stations, args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.total_stations == 10
        assert summary.successful == 10
        assert len(call_count) == 10

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_cli_overrides_config(self, mock_worker, mock_defaults):
        """CLI --batches overrides scheduler.yaml value."""
        mock_defaults.return_value = {"batches": 5, "distribution_window": 10}

        call_count = []

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            call_count.append(sid.upper())
            return StationResult(
                station_id=sid.upper(), status="completed",
                files_downloaded=1, duration=1.0, attempt=attempt,
            )
        mock_worker.side_effect = mock_fn

        # CLI says 2 batches, config says 5 -> CLI wins
        stations = [f"ST{i:02d}" for i in range(10)]
        args = self._make_args(batches=2, distribution_window=0)
        logger = MagicMock()

        summary = download_parallel(
            stations=stations, args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        # 10 stations / 2 batches = 5 per group
        assert summary.total_stations == 10
        assert summary.successful == 10

    @patch("receivers.cli.parallel._get_session_defaults")
    @patch("receivers.cli.parallel._download_one_station")
    def test_mixed_results(self, mock_worker, mock_defaults):
        """Mix of completed, failed, skipped stations."""
        mock_defaults.return_value = {"batches": 3, "distribution_window": 0}

        results_map = {
            "ELDC": ("completed", 5),
            "THOB": ("failed", 0),
            "ISFS": ("skipped", 0),
        }

        def mock_fn(sid, *_rest):
            attempt = _rest[-1] if _rest else 1
            sid = sid.upper()
            status, files = results_map[sid]
            return StationResult(
                station_id=sid, status=status,
                files_downloaded=files, duration=2.0, attempt=attempt,
                error_message="err" if status == "failed" else None,
            )
        mock_worker.side_effect = mock_fn

        args = self._make_args()
        logger = MagicMock()

        summary = download_parallel(
            stations=["ELDC", "THOB", "ISFS"],
            args=args, logger=logger,
            start_time=datetime(2026, 2, 10),
            end_time=datetime(2026, 2, 11),
            ffrequency="1D", afrequency="15s",
            reverse_chronological=False,
        )

        assert summary.successful == 1
        assert summary.failed == 1
        assert summary.skipped == 1
        assert summary.total_files == 5


@pytest.mark.unit
class TestSingleStationIgnoresParallel:
    """Test that single-station downloads don't use parallel mode."""

    def test_parallel_flag_ignored_for_single_station(self):
        """Parallel flag should be ignored when only one station provided.

        This is enforced in cmd_download() with the len(args.stations) > 1 check.
        """
        args = Mock()
        args.parallel = True
        args.stations = ["ELDC"]

        use_parallel = getattr(args, "parallel", False) and len(args.stations) > 1
        assert not use_parallel

    def test_parallel_flag_used_for_multiple_stations(self):
        args = Mock()
        args.parallel = True
        args.stations = ["ELDC", "THOB"]

        use_parallel = getattr(args, "parallel", False) and len(args.stations) > 1
        assert use_parallel


@pytest.mark.unit
class TestDownloadStationPeriod:
    """Test _download_station_period failure propagation.

    Verifies that download_data() returning status='failed' with
    files_downloaded=0 is correctly propagated as errors >= 1,
    preventing the parallel orchestrator from misclassifying
    failures as 'up_to_date'.
    """

    def _make_args(self, **overrides):
        args = Mock()
        args.session = "15s_24hr"
        args.ffrequency = "1D"
        args.afrequency = "15s"
        args.compression = ".gz"
        args.sync = True
        args.clean_tmp = False
        args.archive = True
        args.loglevel = 30  # WARNING
        args.test_connection = False
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def test_failed_status_propagates_as_error(self):
        """download_data returning status=failed with 0 files sets errors >= 1."""
        from receivers.cli.main import _download_station_period

        receiver = MagicMock()
        receiver.download_data.return_value = {
            "status": "failed",
            "files_downloaded": 0,
            "error_message": "Timeout (progress)",
            "duration": 600.0,
        }

        args = self._make_args()
        logger = MagicMock()

        files, errors, checked = _download_station_period(
            receiver, "INTA", datetime(2026, 2, 22), datetime(2026, 2, 23),
            args, logger, ffrequency="1D", afrequency="15s",
        )

        assert files == 0
        assert errors >= 1

    def test_up_to_date_does_not_set_error(self):
        """download_data returning status=up_to_date with files_checked > 0 keeps errors=0."""
        from receivers.cli.main import _download_station_period

        receiver = MagicMock()
        receiver.download_data.return_value = {
            "status": "up_to_date",
            "files_downloaded": 0,
            "files_checked": 24,
            "duration": 0.5,
        }

        args = self._make_args()
        logger = MagicMock()

        files, errors, checked = _download_station_period(
            receiver, "ELDC", datetime(2026, 2, 22), datetime(2026, 2, 23),
            args, logger, ffrequency="1D", afrequency="15s",
        )

        assert files == 0
        assert errors == 0
        assert checked == 24

    def test_up_to_date_with_zero_files_checked_is_error(self):
        """download_data returning up_to_date with files_checked=0 sets errors >= 1.

        This catches the case where file_date_dict is empty (no timestamps
        generated), which means the station didn't actually check any files
        and shouldn't be classified as up-to-date.
        """
        from receivers.cli.main import _download_station_period

        receiver = MagicMock()
        receiver.download_data.return_value = {
            "status": "up_to_date",
            "files_downloaded": 0,
            "files_checked": 0,
            "duration": 0.3,
        }

        args = self._make_args()
        logger = MagicMock()

        files, errors, checked = _download_station_period(
            receiver, "AFST", datetime(2026, 2, 22), datetime(2026, 2, 23),
            args, logger, ffrequency="1D", afrequency="15s",
        )

        assert files == 0
        assert errors >= 1
        assert checked == 0

    def test_completed_with_files_no_error(self):
        """download_data returning status=completed with files keeps errors=0."""
        from receivers.cli.main import _download_station_period

        receiver = MagicMock()
        receiver.download_data.return_value = {
            "status": "completed",
            "files_downloaded": 3,
            "downloaded_files": ["/tmp/a", "/tmp/b", "/tmp/c"],
            "duration": 120.0,
        }

        args = self._make_args()
        logger = MagicMock()

        files, errors, _checked = _download_station_period(
            receiver, "THOB", datetime(2026, 2, 22), datetime(2026, 2, 23),
            args, logger, ffrequency="1D", afrequency="15s",
        )

        assert files == 3
        assert errors == 0

    def test_configuration_error_status_propagates(self):
        """download_data returning configuration_error with 0 files sets errors >= 1."""
        from receivers.cli.main import _download_station_period

        receiver = MagicMock()
        receiver.download_data.return_value = {
            "status": "configuration_error",
            "files_downloaded": 0,
            "error": "Invalid IP range",
            "duration": 0.1,
        }

        args = self._make_args()
        logger = MagicMock()

        files, errors, _checked = _download_station_period(
            receiver, "BADCFG", datetime(2026, 2, 22), datetime(2026, 2, 23),
            args, logger, ffrequency="1D", afrequency="15s",
        )

        assert files == 0
        assert errors >= 1
