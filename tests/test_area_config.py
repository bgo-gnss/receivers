"""Tests for the station_areas.yaml config refactor.

Covers:
- seeder.resolve_areas_yaml() precedence (GPS_CONFIG_DATA_REPO → gps_parser
  deployed dir → bundled default).
- the scheduler's station_areas.yaml mtime watch + auto-reseed.
- schema v-next compat: the optional per-area keys (name_is, cdn) added for
  the aflogun_tmp region source must not change what seed_areas() loads.

No DB / network — the reseed itself is mocked; the compat test uses the
dry-run path, which parses the yaml without a connection.
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


# --- schema v-next compat (name_is / cdn keys) --------------------------------

BUNDLED_AREAS = seeder_mod.CONFIG_DIR / "defaults" / "station_areas.yaml"


def _seed_areas_dry_run(areas_file):
    """Run the real seed_areas dry-run path without a DB connection."""
    stub = SimpleNamespace()
    return seeder_mod.Seeder.seed_areas.__get__(stub)(
        areas_file=areas_file, dry_run=True
    )


def test_seed_areas_unaffected_by_optional_area_keys():
    # seed_areas must load exactly the areas/members the yaml declares, with
    # the new optional keys (name_is, cdn) present but ignored.
    import yaml

    data = yaml.safe_load(BUNDLED_AREAS.read_text())
    expected_areas = sum(len(data[t]) for t in ("volcanic_areas", "regional_areas"))
    expected_members = sum(
        len(area["stations"])
        for t in ("volcanic_areas", "regional_areas")
        for area in data[t].values()
    )

    counts = _seed_areas_dry_run(BUNDLED_AREAS)

    assert counts == {"areas": expected_areas, "members": expected_members}
    # 14 volcanic + 7 regional; seismic_areas/monitoring_areas sections are
    # invisible to the seeder until it opts in — that's the compat contract.
    assert expected_areas == 21


def test_optional_keys_follow_schema_rules():
    # name_is everywhere; cdn only where a CDN plot dir exists, and the
    # svartsengi area maps to the thorbjorn dir.
    import yaml

    data = yaml.safe_load(BUNDLED_AREAS.read_text())
    areas = {
        area_id: area
        for t in ("volcanic_areas", "regional_areas")
        for area_id, area in data[t].items()
    }

    assert all("name_is" in area for area in areas.values())
    assert areas["svartsengi"]["cdn"] == "thorbjorn"

    cdn_slugs = {a["cdn"] for a in areas.values() if "cdn" in a}
    # volcanic/regional slugs only — tjornes, seydisfjordur, svinafellsheidi
    # and eskaftarketill live in the seismic/monitoring sections.
    assert cdn_slugs == {
        "askja",
        "bardarbunga",
        "eyjafjallajokull",
        "grimsvotn",
        "hekla",
        "hengill",
        "katla",
        "kverkfjoll",
        "ljosufjoll",
        "oraefajokull",
        "reykjanes",
        "thorbjorn",
        "torfajokull",
    }
