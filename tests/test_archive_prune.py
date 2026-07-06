"""Local ring-buffer prune: catalog gate, retention, disk guardrails."""

from datetime import date, timedelta
from pathlib import Path

from receivers.archive import prune
from receivers.archive.prune import (
    PruneConfig,
    file_observation_date,
    run_prune,
)
from receivers.utils.canonical_key import canonical_key

TODAY = date(2026, 7, 6)


class TestFileObservationDate:
    def test_raw_name(self):
        assert file_observation_date("RHOF202607061400b.sbf.gz") == date(2026, 7, 6)
        assert file_observation_date("ALFD202606300600b.T02.gz") == date(2026, 6, 30)

    def test_rinex_short_daily_and_hourly(self):
        assert file_observation_date("RHOF1870.26D.Z") == date(2026, 7, 6)
        assert file_observation_date("ALHV156g.26D.Z") == date(2026, 6, 5)
        assert file_observation_date("RHOF0970.18d.Z") == date(2018, 4, 7)

    def test_century_split(self):
        assert file_observation_date("RHOF0010.99D.Z") == date(1999, 1, 1)

    def test_unparseable(self):
        assert file_observation_date("random.txt") is None
        assert (
            file_observation_date("RHOF00ISL_R_20261870000_01D_15S_MO.crx.gz") is None
        )


class _Cur:
    def __init__(self, keys):
        self._keys = keys

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        pass

    def fetchall(self):
        return [(k,) for k in self._keys]


class _Conn:
    def __init__(self, keys):
        self._keys = keys

    def cursor(self):
        return _Cur(self._keys)


def _mk(root: Path, rel: str, size: int = 1000) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def _plenty_of_disk(root, cfg):
    return "normal", 500.0


class TestRunPrune:
    def _tree(self, root: Path):
        # old (2025) + young (2026 jul) 1Hz files, and an old 15s file
        old_1hz = _mk(root, "2025/jun/RHOF/1Hz_1hr/raw/RHOF202506051400b.sbf.gz")
        old_1hz_uncat = _mk(root, "2025/jun/RHOF/1Hz_1hr/raw/RHOF202506051500b.sbf.gz")
        young_1hz = _mk(root, "2026/jul/RHOF/1Hz_1hr/raw/RHOF202607051400b.sbf.gz")
        old_15s = _mk(root, "2025/jun/RHOF/15s_24hr/rinex/RHOF1560.25D.Z")
        return old_1hz, old_1hz_uncat, young_1hz, old_15s

    def test_deletes_only_old_and_cataloged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        old_1hz, old_uncat, young, old_15s = self._tree(tmp_path)
        cfg = PruneConfig(
            retention_days={"1Hz_1hr": 21, "15s_24hr": 365},
        )
        conn = _Conn({canonical_key(old_1hz.name)})  # only ONE file archived
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="imo_archive",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert not old_1hz.exists()  # old + catalog-confirmed → pruned
        assert old_uncat.exists()  # old but NOT archived → kept
        assert young.exists()  # young → kept
        assert old_15s.exists()  # 15s retention 365d, 2025-06-05 is inside
        assert stats.deleted == 1 and stats.kept_uncataloged >= 1

    def test_dry_run_deletes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        old_1hz, *_ = self._tree(tmp_path)
        cfg = PruneConfig(retention_days={"1Hz_1hr": 21})
        conn = _Conn({canonical_key(old_1hz.name)})
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=True,
            today=TODAY,
        )
        assert old_1hz.exists() and stats.deleted == 1  # counted, not deleted

    def test_no_conn_with_catalog_gate_deletes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        old_1hz, *_ = self._tree(tmp_path)
        cfg = PruneConfig(retention_days={"1Hz_1hr": 21}, require_catalog=True)
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=None,
            dry_run=False,
            today=TODAY,
        )
        assert old_1hz.exists() and stats.deleted == 0

    def test_emergency_mode_applies_shorter_retention(self, tmp_path, monkeypatch):
        # 10 days old: survives normal 1Hz retention (21d) but not emergency (7d)
        monkeypatch.setattr(prune, "disk_mode", lambda r, c: ("emergency", 50.0))
        f = _mk(tmp_path, "2026/jun/RHOF/1Hz_1hr/raw/RHOF202606261400b.sbf.gz")
        cfg = PruneConfig(
            retention_days={"1Hz_1hr": 21},
            emergency_retention_days={"1Hz_1hr": 7},
        )
        conn = _Conn({canonical_key(f.name)})
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert not f.exists() and stats.mode == "emergency"

    def test_normal_mode_keeps_the_same_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        f = _mk(tmp_path, "2026/jun/RHOF/1Hz_1hr/raw/RHOF202606261400b.sbf.gz")
        cfg = PruneConfig(
            retention_days={"1Hz_1hr": 21},
            emergency_retention_days={"1Hz_1hr": 7},
        )
        conn = _Conn({canonical_key(f.name)})
        run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert f.exists()

    def test_max_delete_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        files = [
            _mk(tmp_path, f"2025/jun/RHOF/1Hz_1hr/raw/RHOF2025060{i}1400b.sbf.gz")
            for i in range(1, 6)
        ]
        cfg = PruneConfig(retention_days={"1Hz_1hr": 21}, max_delete_per_run=2)
        conn = _Conn({canonical_key(f.name) for f in files})
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert stats.deleted == 2 and stats.capped
        assert sum(1 for f in files if f.exists()) == 3

    def test_zero_or_negative_retention_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        f = _mk(tmp_path, "2025/jun/RHOF/1Hz_1hr/raw/RHOF202506051400b.sbf.gz")
        cfg = PruneConfig(retention_days={"1Hz_1hr": 0})
        conn = _Conn({canonical_key(f.name)})
        stats = run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert f.exists() and stats.deleted == 0

    def test_empty_dirs_cleaned_after_prune(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prune, "disk_mode", _plenty_of_disk)
        f = _mk(tmp_path, "2025/jun/RHOF/1Hz_1hr/raw/RHOF202506051400b.sbf.gz")
        cfg = PruneConfig(retention_days={"1Hz_1hr": 21})
        conn = _Conn({canonical_key(f.name)})
        run_prune(
            tmp_path,
            cfg,
            archive_location="a",
            conn=conn,
            dry_run=False,
            today=TODAY,
        )
        assert not (tmp_path / "2025").exists()  # whole empty chain removed


class TestDiskFillForecast:
    def _run(self, tmp_path, monkeypatch, free_sequence, warn_days=21):
        """Feed a sequence of (day-offset, free_gb) samples; return final ETA."""
        from receivers.archive.prune import record_and_forecast

        state = tmp_path / "hist.json"
        vol = tmp_path
        eta = None
        for off, free in free_sequence:
            monkeypatch.setattr(prune, "disk_free_gb", lambda p, f=free: (f, 1000.0))
            eta = record_and_forecast(
                vol,
                state,
                warn_days_to_full=warn_days,
                today=TODAY + timedelta(days=off),
            )
        return eta

    def test_filling_volume_projects_days_to_full(self, tmp_path, monkeypatch):
        # losing 10 GB/day from 300 GB free → ~28 days to full
        eta = self._run(
            tmp_path,
            monkeypatch,
            [(0, 300.0), (1, 290.0), (2, 280.0), (3, 270.0)],
        )
        assert eta is not None and 26 <= eta <= 29

    def test_error_when_inside_warn_window(self, tmp_path, monkeypatch, caplog):
        import logging as _logging

        with caplog.at_level(_logging.ERROR, logger="receivers.archive.prune"):
            eta = self._run(
                tmp_path,
                monkeypatch,
                [(0, 100.0), (2, 60.0), (4, 20.0)],  # 20 GB/day, 1 day left
            )
        assert eta is not None and eta < 21
        assert any("DISK FILL FORECAST" in r.message for r in caplog.records)

    def test_not_filling_returns_none(self, tmp_path, monkeypatch):
        eta = self._run(
            tmp_path,
            monkeypatch,
            [(0, 300.0), (2, 300.0), (4, 305.0)],  # stable/growing free
        )
        assert eta is None

    def test_single_sample_no_trend(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, [(0, 300.0)]) is None

    def test_history_persists_and_trims(self, tmp_path, monkeypatch):
        import json

        self._run(tmp_path, monkeypatch, [(i, 300.0 - i) for i in range(5)])
        hist = json.loads((tmp_path / "hist.json").read_text())
        samples = hist[str(tmp_path)]
        assert len(samples) == 5
        assert samples[-1][1] == 296.0
