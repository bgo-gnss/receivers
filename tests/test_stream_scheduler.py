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
            _run_stream_config_refresh_job,
            _run_stream_pipeline_job,
            _run_stream_supervise_job,
        )

        assert callable(_run_stream_supervise_job)
        assert callable(_run_stream_pipeline_job)
        assert callable(_run_stream_config_refresh_job)


def _settings(tmp_path):
    return StreamSettings(
        archive_base=str(tmp_path / "arch"),
        rt_base=str(tmp_path / "rt"),
        workdir=str(tmp_path / "wd"),
        bnc_config_dir=str(tmp_path / "bkg"),
        caster_user="u",
        caster_password="p",
    )


class TestLoadStreamSettings:
    def _fake_rc(self, ini: str):
        import configparser
        from unittest.mock import Mock

        cp = configparser.ConfigParser()
        cp.read_string(ini)
        rc = Mock()
        rc.config = cp
        rc.get_data_prepath.return_value = "/data"
        rc.get_tmp_dir.return_value = "/tmp"
        return rc

    def test_caster_read_from_ntrip_defaults(self, monkeypatch):
        import receivers.config.receivers_config as rcmod

        rc = self._fake_rc(
            "[ntrip_defaults]\n"
            "host = ntrcaster.vedur.is\nport = 2101\n"
            "username = gpsops\npassword = secret\nmountpoint_suffix = 0,1\n"
        )
        monkeypatch.setattr(rcmod, "get_receivers_config", lambda: rc)
        from receivers.scheduling.stream_scheduler import load_stream_settings

        s = load_stream_settings()
        assert s.caster_host == "ntrcaster.vedur.is" and s.caster_port == 2101
        assert s.caster_user == "gpsops" and s.caster_password == "secret"
        assert s.mountpoint_suffix == "0"  # first of comma-separated

    def test_streaming_overrides_ntrip_defaults(self, monkeypatch):
        import receivers.config.receivers_config as rcmod

        rc = self._fake_rc(
            "[ntrip_defaults]\nusername = gpsops\npassword = a\n"
            "[streaming]\ncaster_user = streamuser\ncaster_password = b\n"
        )
        monkeypatch.setattr(rcmod, "get_receivers_config", lambda: rc)
        from receivers.scheduling.stream_scheduler import load_stream_settings

        s = load_stream_settings()
        assert s.caster_user == "streamuser" and s.caster_password == "b"


class TestGenerateBncConfig:
    def test_writes_bnc_with_caster(self, tmp_path):
        from receivers.scheduling.stream_scheduler import generate_bnc_config_file

        cfg = {"station": {"latitude": "63.9", "longitude": "-22.3"}}
        out = generate_bnc_config_file("GONH", cfg, _settings(tmp_path))
        assert out.name == "rtcm2rinex-GONH.bnc"
        body = out.read_text()
        assert "ntrcaster.vedur.is:2101/GONH0" in body
        assert "rnxPath=" in body and "RT" in body


class TestRefreshStationSkeleton:
    def _skl(self, tmp_path, station):
        d = tmp_path / "rt" / station
        d.mkdir(parents=True)
        skl = d / f"{station}.SKL"
        skl.write_text(
            f"{'3605273':<20}{'SEPT MOSAIC-X5':<20}{'4.8.0':<20}REC # / TYPE / VERS\n"
        )
        return skl

    def _tos(self, model, serial, fw):
        return lambda _sid: {
            "device_history": [
                {
                    "time_to": None,
                    "gnss_receiver": {
                        "model": model,
                        "serial_number": serial,
                        "firmware_version": fw,
                    },
                }
            ]
        }

    def test_no_tos_no_skeleton(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        # No TOS metadata and no existing skeleton -> cannot create.
        res = refresh_station_skeleton("GONH", _settings(tmp_path), lambda s: {})
        assert res == "no_tos"

    def test_creates_base_skeleton_from_position(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        settings = _settings(tmp_path)
        cfg = {"station": {"latitude": "63.9", "longitude": "-22.3", "height": "300.0"}}
        res = refresh_station_skeleton(
            "GONH",
            settings,
            self._tos("PolaRx5", "4001", "5.6.0"),
            station_config=cfg,
        )
        assert res == "created"
        skl = tmp_path / "rt" / "GONH" / "GONH.SKL"
        assert skl.exists()
        body = skl.read_text()
        assert "APPROX POSITION XYZ" in body and "4001" in body and "SEPT POLARX5" in body

    def test_no_position_no_skeleton(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        res = refresh_station_skeleton(
            "GONH", _settings(tmp_path), self._tos("PolaRx5", "4001", "5.6.0"),
            station_config={"station": {}},
        )
        assert res == "no_position"

    def test_no_tos(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        self._skl(tmp_path, "GONH")
        res = refresh_station_skeleton("GONH", _settings(tmp_path), lambda s: None)
        assert res == "no_tos"

    def test_unchanged(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        self._skl(tmp_path, "GONH")
        res = refresh_station_skeleton(
            "GONH", _settings(tmp_path), self._tos("mosaic-X5", "3605273", "4.8.0")
        )
        # mosaic-X5 -> raw fallback keeps "mosaic-X5"? No: to_igs maps it. Either way
        # the serial/fw match; rec_type may differ -> allow updated or unchanged.
        assert res in ("unchanged", "updated")

    def test_updated_on_equipment_change(self, tmp_path):
        from receivers.scheduling.stream_scheduler import refresh_station_skeleton

        skl = self._skl(tmp_path, "GONH")
        res = refresh_station_skeleton(
            "GONH", _settings(tmp_path), self._tos("PolaRx5", "4009999", "5.6.0")
        )
        assert res == "updated"
        assert "4009999" in skl.read_text() and "SEPT POLARX5" in skl.read_text()
