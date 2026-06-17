"""Tests for the station_areas.yaml config refactor.

Covers:
- seeder.resolve_areas_yaml() precedence (GPS_CONFIG_DATA_REPO → gps_parser
  deployed dir → bundled default).
- the scheduler's station_areas.yaml mtime watch + auto-reseed.

No DB / network — the reseed itself is mocked.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from receivers.db import seeder as seeder_mod
from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

# --- resolve_areas_yaml -----------------------------------------------------


def test_resolve_prefers_gps_config_data_repo(tmp_path, monkeypatch):
    f = tmp_path / "station_areas.yaml"
    f.write_text("volcanic_areas: {}\n")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(tmp_path))
    assert seeder_mod.resolve_areas_yaml() == f


def test_resolve_env_skipped_when_file_absent_falls_back(tmp_path, monkeypatch):
    # Env points at a dir WITHOUT the file → skip; gps_parser made to fail →
    # bundled default.
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(tmp_path))  # no file here

    import builtins

    real_import = builtins.__import__

    def _no_gps_parser(name, *a, **k):
        if name == "gps_parser":
            raise ImportError("forced")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_gps_parser)
    assert seeder_mod.resolve_areas_yaml() == (
        seeder_mod.CONFIG_DIR / "defaults" / "station_areas.yaml"
    )


def test_bundled_default_exists_and_parses():
    # The fallback the resolver returns must actually exist + be valid YAML.
    import yaml

    p = seeder_mod.CONFIG_DIR / "defaults" / "station_areas.yaml"
    assert p.exists()
    data = yaml.safe_load(p.read_text())
    assert "volcanic_areas" in data and "regional_areas" in data


# --- scheduler area watch ---------------------------------------------------


def _bind_watch(obj):
    """Bind the real _check_areas_config_changes to a stub object."""
    return BulkDownloadScheduler._check_areas_config_changes.__get__(obj)


def test_watch_reseeds_on_first_call_then_on_change(tmp_path, monkeypatch):
    yaml_path = tmp_path / "station_areas.yaml"
    yaml_path.write_text("volcanic_areas: {}\n")
    monkeypatch.setattr(seeder_mod, "resolve_areas_yaml", lambda: yaml_path)

    obj = SimpleNamespace(logger=logging.getLogger("test.areas"))
    obj._reseed_areas = MagicMock()
    watch = _bind_watch(obj)

    # First call (startup) → reseed once + record mtime.
    watch()
    assert obj._reseed_areas.call_count == 1

    # No change → no reseed.
    watch()
    assert obj._reseed_areas.call_count == 1

    # Bump mtime → reseed again.
    new_mtime = yaml_path.stat().st_mtime + 100
    os.utime(yaml_path, (new_mtime, new_mtime))
    watch()
    assert obj._reseed_areas.call_count == 2


def test_watch_missing_file_is_noop(tmp_path, monkeypatch):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(seeder_mod, "resolve_areas_yaml", lambda: missing)
    obj = SimpleNamespace(logger=logging.getLogger("test.areas"))
    obj._reseed_areas = MagicMock()
    _bind_watch(obj)()
    obj._reseed_areas.assert_not_called()


def test_reseed_areas_invokes_seeder(monkeypatch):
    fake_seeder = MagicMock()
    fake_seeder.seed_areas.return_value = {"areas": 19, "members": 3}
    monkeypatch.setattr(
        "receivers.db.seeder.Seeder", MagicMock(return_value=fake_seeder)
    )
    obj = SimpleNamespace(logger=logging.getLogger("test.areas"))
    BulkDownloadScheduler._reseed_areas.__get__(obj)()
    fake_seeder.seed_areas.assert_called_once()


def test_reseed_areas_swallows_errors(monkeypatch):
    boom = MagicMock()
    boom.seed_areas.side_effect = RuntimeError("db down")
    monkeypatch.setattr("receivers.db.seeder.Seeder", MagicMock(return_value=boom))
    obj = SimpleNamespace(logger=logging.getLogger("test.areas"))
    # Must not raise — a reseed failure can't be allowed to kill the scheduler job.
    BulkDownloadScheduler._reseed_areas.__get__(obj)()
