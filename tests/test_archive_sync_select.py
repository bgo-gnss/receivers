"""Tests for the targeted archive-sync selection mode (station/session/time window).

Covers the new ``ArchiveSync.enumerate_selection`` / ``ArchiveSync.select`` path
and the push_explicit dry-run guard added for side-effect-free previews.
"""

import os
from datetime import datetime
from pathlib import Path

import pytest

from receivers.archive.config import SyncTarget
from receivers.archive.engine import ArchiveSync
from receivers.archive.path_parse import parse_archive_path

CUTOVER = datetime(2026, 6, 22, 0, 0, 0)


def _target(tmp_root, **over):
    base = dict(
        name="imo_archive",
        active=True,
        tier="archive",
        host="",
        user="",
        dest=str(tmp_root / "dest"),
        source_root=str(tmp_root / "src"),
        sessions=("15s_24hr", "1Hz_1hr", "status_1hr"),
        file_categories=("raw", "rinex"),
        exclude_stations=frozenset({"DYNA"}),
        cutover=CUTOVER,
        overlap_minutes=5,
    )
    base.update(over)
    return SyncTarget(**base)


def _mkfile(src: Path, rel: str) -> str:
    p = src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return str(p)


def _tree(tmp_path) -> Path:
    """A small archive tree with two stations, two sessions, and two months."""
    src = tmp_path / "src"
    _mkfile(src, "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz")
    _mkfile(src, "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z")
    _mkfile(src, "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz")
    _mkfile(src, "2026/aug/OTHER/15s_24hr/raw/OTHER202608120000a.sbf.gz")
    _mkfile(src, "2026/aug/DYNA/15s_24hr/raw/DYNA202608120000a.sbf.gz")
    _mkfile(src, "2026/jul/MYVA/15s_24hr/raw/MYVA202607120000a.sbf.gz")
    return src


# ---------------------------------------------------------------------- file window
class TestFileWindow:
    def test_daily_covers_whole_day(self):
        parsed = parse_archive_path(
            "/mnt/data/gpsdata/2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "/mnt/data/gpsdata",
        )
        lo, hi = ArchiveSync._file_window(parsed)
        assert lo == datetime(2026, 8, 12, 0, 0, 0)
        assert hi == datetime(2026, 8, 12, 23, 59, 59)

    def test_hourly_covers_single_hour(self):
        parsed = parse_archive_path(
            "/mnt/data/gpsdata/2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "/mnt/data/gpsdata",
        )
        lo, hi = ArchiveSync._file_window(parsed)
        assert lo == datetime(2026, 8, 12, 3, 0, 0)
        assert hi == datetime(2026, 8, 12, 3, 59, 59)


# ---------------------------------------------------------------------- enumeration
class TestEnumerateSelection:
    def _eng(self, tmp_path):
        t = _target(tmp_path)
        return ArchiveSync(t, conn=None, catalog_conns=[])

    def test_station_filter(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src) for p in eng.enumerate_selection(station="MYVA")
        )
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "2026/jul/MYVA/15s_24hr/raw/MYVA202607120000a.sbf.gz",
        ]

    def test_session_filter(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src) for p in eng.enumerate_selection(session="1Hz_1hr")
        )
        assert got == ["2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz"]

    def test_whole_day_window(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                station="MYVA",
                start=datetime(2026, 8, 12),
                end=datetime(2026, 8, 12, 23, 59, 59),
            )
        )
        # the jul file is excluded; the three aug-12 MYVA files remain
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
        ]

    def test_hour_window_overlaps_daily_files(self, tmp_path):
        """A 03:00 window overlaps daily files (whole day) AND the hourly hour-3 file."""
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                station="MYVA",
                start=datetime(2026, 8, 12, 3),
                end=datetime(2026, 8, 12, 3),
            )
        )
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
        ]

    def test_date_window_without_station(self, tmp_path):
        """A date window (no station) walks only the in-range months."""
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                start=datetime(2026, 8, 12), end=datetime(2026, 8, 12, 23, 59, 59)
            )
        )
        # aug-12 files from both real stations, excluding DYNA (exclude_stations)
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "2026/aug/OTHER/15s_24hr/raw/OTHER202608120000a.sbf.gz",
        ]

    def test_excluded_station_is_never_selected(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        assert eng.enumerate_selection(station="DYNA") == []

    def test_unknown_station_returns_empty(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        assert eng.enumerate_selection(station="ZZZZ") == []


# --------------------------------------------------------------------------- select
class TestSelect:
    def _eng(self, tmp_path, **kw):
        t = _target(tmp_path)
        return ArchiveSync(t, conn=None, catalog_conns=[], **kw)

    def test_select_pushes_without_advancing_watermark(self, tmp_path, monkeypatch):
        import receivers.archive.engine as eng_mod

        _tree(tmp_path)
        eng = self._eng(tmp_path)
        monkeypatch.setattr(
            eng, "push_explicit", lambda paths, **kw: (len(paths), [], {})
        )
        monkeypatch.setattr(
            eng_mod,
            "record_run",
            lambda *a, **k: pytest.fail("watermark must not advance"),
        )

        res = eng.select(
            station="MYVA",
            start=datetime(2026, 8, 12),
            end=datetime(2026, 8, 12, 23, 59, 59),
        )
        assert res.ok is True
        assert res.delta_count == 3
        assert res.transferred == 3
        assert res.cataloged == 3
        assert "watermark NOT advanced" in res.message

    def test_select_no_match_is_ok(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        res = eng.select(station="ZZZZ")
        assert res.ok is True
        assert res.delta_count == 0
        assert res.message == "0 files matched the selection"

    def test_select_dry_run_reports_zero_cataloged(self, tmp_path, monkeypatch):
        _tree(tmp_path)
        eng = self._eng(tmp_path, dry_run=True)
        monkeypatch.setattr(
            eng, "push_explicit", lambda paths, **kw: (len(paths), [], {})
        )
        res = eng.select(
            station="MYVA",
            start=datetime(2026, 8, 12),
            end=datetime(2026, 8, 12, 23, 59, 59),
        )
        assert res.transferred == 3
        assert res.cataloged == 0
        assert "would transfer" in res.message

    def test_select_inactive_target_skipped(self, tmp_path):
        _tree(tmp_path)
        t = _target(tmp_path, active=False)
        eng = ArchiveSync(t, conn=None, catalog_conns=[])
        res = eng.select(station="MYVA")
        assert res.ok is True
        assert res.message == "target inactive — skipped"


# ---------------------------------------------------------------- push dry-run guard
class TestPushExplicitDryRun:
    def test_dry_run_does_not_catalog(self, tmp_path, monkeypatch):
        src = _tree(tmp_path)
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[], dry_run=True)
        monkeypatch.setattr(
            eng, "_rsync", lambda rels, imm, **kw: (True, list(rels), "")
        )
        monkeypatch.setattr(
            eng,
            "_catalog_transferred",
            lambda *a, **k: pytest.fail("dry-run must not write the catalog"),
        )

        pushed, errors, _ = eng.push_explicit(
            [
                os.path.join(
                    str(src), "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz"
                )
            ]
        )
        assert pushed == 1
        assert errors == []


# ------------------------------------------------------------------- rsync timeout
class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture_run(monkeypatch):
    """Record the ``timeout`` every ``subprocess.run`` in the engine receives."""
    import receivers.archive.engine as eng_mod

    seen: list = []

    def fake_run(cmd, **kw):
        seen.append(kw.get("timeout"))
        return _Proc()

    monkeypatch.setattr(eng_mod.subprocess, "run", fake_run)
    return seen


class TestRsyncTimeout:
    """Selection mode used the 60 s write-through limit, so a bulk push was killed
    mid-transfer. It must use ``rsync_timeout`` (CLI ``--timeout``, 0 = unlimited)."""

    def _select(self, tmp_path, monkeypatch, **kw):
        _tree(tmp_path)
        seen = _capture_run(monkeypatch)
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[], **kw)
        eng.select(station="MYVA")
        return seen

    def test_select_uses_rsync_timeout_not_push_limit(self, tmp_path, monkeypatch):
        seen = self._select(tmp_path, monkeypatch, rsync_timeout=900)
        assert seen and set(seen) == {900}

    def test_select_default_is_long_timeout(self, tmp_path, monkeypatch):
        seen = self._select(tmp_path, monkeypatch)
        assert seen and set(seen) == {1800}

    def test_select_zero_is_unlimited(self, tmp_path, monkeypatch):
        seen = self._select(tmp_path, monkeypatch, rsync_timeout=0)
        assert seen and set(seen) == {None}

    def test_push_on_download_keeps_short_limit(self, tmp_path, monkeypatch):
        from receivers.archive.engine import _PUSH_RSYNC_TIMEOUT

        src = _tree(tmp_path)
        seen = _capture_run(monkeypatch)
        eng = ArchiveSync(
            _target(tmp_path), conn=None, catalog_conns=[], rsync_timeout=0
        )
        eng.push_explicit(
            [str(src / "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz")]
        )
        assert seen == [_PUSH_RSYNC_TIMEOUT]

    def test_explicit_zero_overrides_engine_default(self, tmp_path, monkeypatch):
        """0 passed per call means unlimited even when the engine default is not."""
        src = _tree(tmp_path)
        seen = _capture_run(monkeypatch)
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[])
        eng.push_explicit(
            [str(src / "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz")],
            timeout=0,
        )
        assert seen == [None]

    def test_sweep_zero_is_unlimited(self, tmp_path, monkeypatch):
        """The watermark sweep's ``find`` and rsync share the same knob."""
        _tree(tmp_path)
        seen = _capture_run(monkeypatch)
        eng = ArchiveSync(
            _target(tmp_path), conn=None, catalog_conns=[], rsync_timeout=0
        )
        eng.find_delta(CUTOVER, "raw")
        eng._rsync(["x"], "--ignore-existing")
        assert seen == [None, None]


class TestCliTimeout:
    def _parse(self, *argv):
        import argparse

        from receivers.cli.archive_sync import create_archive_sync_parser

        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers()
        create_archive_sync_parser(sub)
        return ap.parse_args(["archive-sync", *argv])

    def _run(self, tmp_path, monkeypatch, *argv):
        """Run cmd_archive_sync end-to-end with rsync and the DB stubbed out."""
        import contextlib

        import receivers.archive as archive_pkg

        _tree(tmp_path)
        seen = _capture_run(monkeypatch)
        monkeypatch.setattr(
            archive_pkg, "load_sync_config", lambda path: [_target(tmp_path)]
        )
        monkeypatch.setattr(
            archive_pkg,
            "open_catalog_conns",
            lambda **kw: contextlib.nullcontext([]),
        )
        args = self._parse("--station", "MYVA", "--dry-run", *argv)
        return args.func(args), seen

    def test_default_is_1800(self):
        assert self._parse().timeout == 1800

    def test_cli_timeout_reaches_rsync(self, tmp_path, monkeypatch):
        rc, seen = self._run(tmp_path, monkeypatch, "--timeout", "120")
        assert rc == 0
        assert seen and set(seen) == {120}

    def test_cli_zero_is_unlimited(self, tmp_path, monkeypatch):
        rc, seen = self._run(tmp_path, monkeypatch, "--timeout", "0")
        assert rc == 0
        assert seen and set(seen) == {None}

    def test_cli_negative_rejected(self, tmp_path, monkeypatch, capsys):
        rc, seen = self._run(tmp_path, monkeypatch, "--timeout", "-5")
        assert rc == 1
        assert seen == []
        assert "--timeout" in capsys.readouterr().out


# --------------------------------------------------------- interrupted transfer
@pytest.mark.skipif(__import__("shutil").which("rsync") is None, reason="needs rsync")
class TestInterruptedTransfer:
    """A timed-out rsync must not leave a truncated file under the real name.

    With ``--partial`` the killed transfer left a truncated file newer than its
    source on the archive, so every later --update / --ignore-existing run
    skipped it (HOFN/REYK, 2026-10-15). The timeout must also not raise out of
    the best-effort push path.
    """

    REL = "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz"

    def _engine(self, tmp_path):
        src = tmp_path / "src"
        f = src / self.REL
        f.parent.mkdir(parents=True)
        f.write_bytes(os.urandom(20_000_000))
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[])
        return eng, tmp_path / "dest" / self.REL

    def test_build_cmd_has_no_partial(self, tmp_path):
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[])
        cmd = eng._build_rsync_cmd("/dev/null", "--update")
        assert not any(a.startswith("--partial") for a in cmd)

    def test_killed_transfer_leaves_no_truncated_file(self, tmp_path):
        eng, dest = self._engine(tmp_path)
        ok, transferred, msg = eng._rsync(
            [self.REL], "--ignore-existing", timeout=1, extra_args=["--bwlimit=2000"]
        )
        assert ok is False
        assert transferred == []
        assert "timed out" in msg
        import time

        time.sleep(1)  # let the orphaned receiver finish its cleanup
        assert not dest.exists(), f"truncated {dest.stat().st_size} B left behind"

    def test_push_explicit_does_not_raise_on_timeout(self, tmp_path, monkeypatch):
        import receivers.archive.engine as eng_mod

        def boom(cmd, **kw):
            raise eng_mod.subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        eng, _dest = self._engine(tmp_path)
        monkeypatch.setattr(eng_mod.subprocess, "run", boom)
        pushed, errors, digests = eng.push_explicit([str(tmp_path / "src" / self.REL)])
        assert (pushed, digests) == (0, {})
        assert len(errors) == 1 and "timed out" in errors[0]
