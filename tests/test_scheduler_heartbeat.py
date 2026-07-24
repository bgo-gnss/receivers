"""Tests for the scheduler liveness heartbeat (_heartbeat_job).

The heartbeat is the in-process half of the 2026-07-21 "active but wedged"
detector: a 1-min file touch that the external gps-host-monitor check watches for
staleness. Here we verify the job writes a fresh file next to the log dir and
never raises (a failed write is the signal, not a crash).
"""

from __future__ import annotations

import time
import types
from pathlib import Path

from receivers.scheduling import bulk_scheduler as bs


def test_heartbeat_written_next_to_log_dir(tmp_path, monkeypatch):
    fake = types.SimpleNamespace(log_dir=tmp_path / "logs")
    monkeypatch.setattr(bs, "_scheduler_instance", fake)

    bs._heartbeat_job()

    hb = tmp_path / "heartbeat"
    assert hb.exists()
    assert time.time() - hb.stat().st_mtime < 5


def test_heartbeat_falls_back_to_default_path(tmp_path, monkeypatch):
    """With no scheduler instance, it uses the default cache path (and must not raise)."""
    monkeypatch.setattr(bs, "_scheduler_instance", None)
    monkeypatch.setattr(bs.Path, "home", staticmethod(lambda: tmp_path))

    bs._heartbeat_job()

    hb = tmp_path / ".cache" / "gps_receivers" / "heartbeat"
    assert hb.exists()


def test_heartbeat_swallows_write_failure(monkeypatch):
    """A failed write (e.g. disk full) must not raise — staleness is the signal."""
    bad = types.SimpleNamespace(log_dir=Path("/proc/nonexistent/logs"))
    monkeypatch.setattr(bs, "_scheduler_instance", bad)

    # Must not raise even though the path is unwritable.
    bs._heartbeat_job()
