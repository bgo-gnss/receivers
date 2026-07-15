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


class TestStationExtractionAndTimestamp:
    """JSONFormatter/ProductionFormatter must (a) tag only real 4-char
    station ids from the logger name — not any 3+-segment tail — and
    (b) emit an explicit-UTC timestamp.

    Regression: the old ``record.name.count('.') >= 2`` heuristic
    mislabelled ``receivers.scheduler.reconciler`` as station
    "reconciler"; the old bare ``fromtimestamp()`` emitted a local-naive
    ISO string Loki/Grafana would misread as UTC.
    """

    @staticmethod
    def _record(name):
        import logging

        return logging.getLogger(name).makeRecord(
            name, logging.INFO, "x.py", 1, "msg", (), None
        )

    def test_station_logger_gets_station_id(self):
        import json

        from receivers.base.production_logging import JSONFormatter

        entry = json.loads(
            JSONFormatter().format(self._record("receivers.download.RHOF"))
        )
        assert entry["station_id"] == "RHOF"

    def test_module_logger_has_no_station_id(self):
        import json

        from receivers.base.production_logging import JSONFormatter

        for name in (
            "receivers.scheduler.reconciler",
            "receivers.health.db_writer",
            "receivers.cli.health_query",
        ):
            entry = json.loads(JSONFormatter().format(self._record(name)))
            assert "station_id" not in entry, name

    def test_explicit_station_id_attr_still_used(self):
        import json
        import logging

        from receivers.base.production_logging import JSONFormatter

        rec = logging.getLogger("receivers.scheduler").makeRecord(
            "receivers.scheduler", logging.INFO, "x.py", 1, "m", (), None
        )
        rec.station_id = "ELEY"
        entry = json.loads(JSONFormatter().format(rec))
        assert entry["station_id"] == "ELEY"

    def test_timestamp_is_utc_aware(self):
        import json
        from datetime import datetime

        from receivers.base.production_logging import JSONFormatter

        entry = json.loads(JSONFormatter().format(self._record("receivers.x")))
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.utcoffset() is not None  # tz-aware, not local-naive
        assert ts.utcoffset().total_seconds() == 0  # UTC

    def test_production_formatter_brackets_only_real_station(self):
        from receivers.base.production_logging import ProductionFormatter

        f = ProductionFormatter()
        assert "[RHOF]" in f.format(self._record("receivers.download.RHOF"))
        assert "[reconciler]" not in f.format(
            self._record("receivers.scheduler.reconciler")
        )


class TestLogRotationGate:
    """The receivers.log / audit handler must self-rotate on dev but defer to
    logrotate on the server. Regression: an unconditional RotatingFileHandler
    fought the server's logrotate over the same file and churned the most recent
    hours of logs off disk (backupCount deletion at ~19 MB/h).
    """

    def test_external_rotation_uses_watched_handler(self, tmp_path, monkeypatch):
        import logging.handlers

        from receivers.base.production_logging import make_log_file_handler

        monkeypatch.setenv("RECEIVERS_LOG_EXTERNAL_ROTATION", "1")
        h = make_log_file_handler(tmp_path / "receivers.log", 1024, 3)
        try:
            assert isinstance(h, logging.handlers.WatchedFileHandler)
            # WatchedFileHandler never deletes/rotates on its own.
            assert not isinstance(h, logging.handlers.RotatingFileHandler)
        finally:
            h.close()

    def test_no_external_rotation_uses_rotating_handler(self, tmp_path, monkeypatch):
        import logging.handlers

        from receivers.base.production_logging import make_log_file_handler

        monkeypatch.setenv("RECEIVERS_LOG_EXTERNAL_ROTATION", "0")
        h = make_log_file_handler(tmp_path / "receivers.log", 4096, 2)
        try:
            assert isinstance(h, logging.handlers.RotatingFileHandler)
            assert h.maxBytes == 4096 and h.backupCount == 2
        finally:
            h.close()
