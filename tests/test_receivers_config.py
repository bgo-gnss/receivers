"""Unit tests for :mod:`receivers.config.receivers_config`.

Covers config-accessor methods of :class:`ReceiversConfig`. No real config
file required — tests write a temporary cfg and instantiate the loader
against it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from receivers.config.receivers_config import ReceiversConfig


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg_path = tmp_path / "receivers.cfg"
    cfg_path.write_text(body)
    return cfg_path


def _make_config(tmp_path: Path, body: str) -> ReceiversConfig:
    cfg_path = _write_cfg(tmp_path, body)
    return ReceiversConfig(config_path=str(cfg_path))


# ---------------------------------------------------------------------------
# get_cold_archive_prepath
# ---------------------------------------------------------------------------


def test_cold_archive_prepath_returns_explicit_cfg_value(tmp_path):
    cfg = _make_config(
        tmp_path,
        "[archive_paths]\ncold_archive_prepath = /custom/path/rawgpsdata/\n",
    )
    assert cfg.get_cold_archive_prepath() == "/custom/path/rawgpsdata/"


def test_cold_archive_prepath_resolves_relative_to_cwd(tmp_path):
    cfg = _make_config(
        tmp_path,
        "[archive_paths]\ncold_archive_prepath = ./local_archive\n",
    )
    result = cfg.get_cold_archive_prepath()
    assert os.path.isabs(result)
    assert result.endswith("local_archive")


def test_cold_archive_prepath_fallback_probes_known_mounts(tmp_path):
    """When the cfg entry is missing, we probe well-known mount points
    and pick the first that exists. Patch os.path.isdir to control which
    'mounts' look like they're available."""
    cfg = _make_config(tmp_path, "[archive_paths]\ndata_prepath = /unused\n")

    # Simulate laptop layout: only /mnt_data/rawgpsdata exists.
    with patch("receivers.config.receivers_config.os.path.isdir") as mock_isdir:
        mock_isdir.side_effect = lambda p: p == "/mnt_data/rawgpsdata"
        assert cfg.get_cold_archive_prepath() == "/mnt_data/rawgpsdata"


def test_cold_archive_prepath_fallback_prefers_production_when_both_exist(tmp_path):
    """If both /mnt/rawgpsdata and /mnt_data/rawgpsdata exist, we prefer
    the production path (/mnt/rawgpsdata first in the probe order)."""
    cfg = _make_config(tmp_path, "[archive_paths]\ndata_prepath = /unused\n")

    with patch("receivers.config.receivers_config.os.path.isdir", return_value=True):
        assert cfg.get_cold_archive_prepath() == "/mnt/rawgpsdata"


def test_cold_archive_prepath_no_mounts_uses_hardcoded_fallback(tmp_path):
    """When the cfg entry is missing AND no known mount exists, we fall
    back to the production default."""
    cfg = _make_config(tmp_path, "[archive_paths]\ndata_prepath = /unused\n")

    with patch("receivers.config.receivers_config.os.path.isdir", return_value=False):
        result = cfg.get_cold_archive_prepath()

    assert result == "/mnt/rawgpsdata"


def test_cold_archive_prepath_missing_section_uses_fallback(tmp_path):
    """A cfg without an [archive_paths] section at all should still
    resolve via the fallback chain (no crash, no exception)."""
    cfg = _make_config(tmp_path, "[other_section]\nfoo = bar\n")

    with patch("receivers.config.receivers_config.os.path.isdir", return_value=False):
        result = cfg.get_cold_archive_prepath()

    assert result == "/mnt/rawgpsdata"


# ---------------------------------------------------------------------------
# Smoke: existing accessors still work alongside the new one
# ---------------------------------------------------------------------------


def test_existing_data_prepath_accessor_still_works(tmp_path):
    """The new cold_archive accessor must not regress
    :meth:`get_data_prepath` behavior."""
    cfg = _make_config(
        tmp_path,
        (
            "[archive_paths]\n"
            "data_prepath = /mnt/data/gpsdata/\n"
            "cold_archive_prepath = /mnt/rawgpsdata/\n"
        ),
    )
    assert cfg.get_data_prepath() == "/mnt/data/gpsdata/"
    assert cfg.get_cold_archive_prepath() == "/mnt/rawgpsdata/"
