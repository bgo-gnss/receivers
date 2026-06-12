"""Tests for the stream-capture composition root + scheduler wiring."""

from datetime import UTC, datetime
from unittest.mock import Mock

from receivers.scheduling.stream_scheduler import (
    StreamSettings,
    _make_file_tracking_recorder,
    build_stream_pipeline,
    enumerate_stream_stations,
)
from receivers.streaming import StreamPipeline


class TestEnumerate:
    def test_filters_by_acquisition_mode(self):
        cfgs = {
            "GONH": {"station": {"acquisition_mode": "stream"}},
            "THOB": {"station": {}},  # default download
            "MOHA": {"acquisition_mode": "stream"},  # top-level
            "ELDC": {"station": {"acquisition_mode": "download"}},
        }
        assert enumerate_stream_stations(cfgs) == ["GONH", "MOHA"]

    def test_empty(self):
        assert enumerate_stream_stations({}) == []


class TestBuildPipeline:
    def test_builds_pipeline_with_seams(self, tmp_path):
        settings = StreamSettings(
            archive_base=str(tmp_path / "arch"),
            rt_base=str(tmp_path / "rt"),
            workdir=str(tmp_path / "wd"),
        )
        sentinel_dl = Mock()
        pipe = build_stream_pipeline(
            settings, tracker_recorder=Mock(), downloader=sentinel_dl
        )
        assert isinstance(pipe, StreamPipeline)
        assert pipe.archive_base == tmp_path / "arch"
        assert pipe.downloader is sentinel_dl
        assert pipe.ingestor.session_type == "1Hz_1hr"


class TestFileTrackingRecorder:
    def test_adapts_to_mark_file_archived(self, tmp_path):
        tracker = Mock()
        record = _make_file_tracking_recorder(tracker)
        f = tmp_path / "GONH162a.26D.Z"
        f.write_bytes(b"x" * 42)
        record("GONH", f, datetime(2026, 6, 11, 5, tzinfo=UTC), "1Hz_1hr")
        tracker.mark_file_archived.assert_called_once()
        call = tracker.mark_file_archived.call_args
        assert call.args[0] == "GONH" and call.args[1] == "1Hz_1hr"
        assert call.kwargs["file_hour"] == 5
        assert call.kwargs["filename"] == "GONH162a.26D.Z"
        assert call.kwargs["file_size"] == 42


class TestJobFunctionsImportable:
    def test_jobs_are_callable(self):
        from receivers.scheduling.stream_scheduler import (
            _run_stream_pipeline_job,
            _run_stream_supervise_job,
        )

        assert callable(_run_stream_supervise_job)
        assert callable(_run_stream_pipeline_job)
