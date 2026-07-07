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


class TestFormatterExceptionInfo:
    """Both custom formatters must emit logger.exception() tracebacks.

    Regression guard: these format() overrides previously dropped exc_info, so
    logger.exception() produced no traceback in either the console/journal or
    the JSON file log — which hid an EPOS dissemination failure entirely.
    """

    @staticmethod
    def _record_with_exc():
        import logging
        import sys

        logger = logging.getLogger("receivers.dissemination.job")
        try:
            raise RuntimeError("boom-in-push")
        except RuntimeError:
            return logger.makeRecord(
                "receivers.dissemination.job",
                logging.ERROR,
                "job.py",
                255,
                "epos-disseminate RHOF 2026-07-05: run failed",
                (),
                sys.exc_info(),
            )

    def test_production_formatter_appends_traceback(self):
        from receivers.base.production_logging import ProductionFormatter

        out = ProductionFormatter().format(self._record_with_exc())
        assert "run failed" in out
        assert "Traceback (most recent call last)" in out
        assert "RuntimeError: boom-in-push" in out

    def test_json_formatter_serializes_exc_info_single_line(self):
        import json
        import logging

        from receivers.base.production_logging import JSONFormatter

        out = JSONFormatter().format(self._record_with_exc())
        assert len(out.splitlines()) == 1  # one JSON object per log line
        entry = json.loads(out)
        assert "Traceback" in entry["exc_info"]
        assert "RuntimeError: boom-in-push" in entry["exc_info"]

        # Regression: a record without exc_info must not gain the key.
        plain = logging.getLogger("receivers.download.RHOF").makeRecord(
            "receivers.download.RHOF", logging.INFO, "x.py", 1, "ok", (), None
        )
        assert "exc_info" not in json.loads(JSONFormatter().format(plain))
