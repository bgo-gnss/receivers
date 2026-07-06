

class TestStationLogRetention:
    def test_dispatcher_default_30_days(self, tmp_path):
        from receivers.logging_config import StationLogDispatcher

        d = StationLogDispatcher(tmp_path / "stations")
        h = d._get_file_handler("RHOF")
        assert h.backupCount == 30
        d.close()

    def test_dispatcher_retention_override(self, tmp_path):
        from receivers.logging_config import StationLogDispatcher

        d = StationLogDispatcher(tmp_path / "stations", retention_days=7)
        assert d._get_file_handler("RHOF").backupCount == 7
        d.close()

    def test_station_log_days_from_cfg(self, tmp_path, monkeypatch):
        from receivers.logging_config import _station_log_days

        cfg = tmp_path / "database.cfg"
        cfg.write_text("[logging]\nstation_log_days = 45\n")
        monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path))
        assert _station_log_days() == 45

    def test_station_log_days_default_when_absent(self, tmp_path, monkeypatch):
        from receivers.logging_config import _station_log_days

        (tmp_path / "database.cfg").write_text("[postgresql]\nhost = x\n")
        monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path))
        assert _station_log_days() == 30
