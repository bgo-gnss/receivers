"""Tests for the --global CLI wiring helpers in receivers.cli.cfg.

Covers the shared helpers (_resolve_global_target mutex + path, _maybe_commit_global
dispatch) in isolation — the verb handlers thread these the same way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from receivers.cfg.operations import CfgOperationError
from receivers.cli.cfg import _maybe_commit_global, _resolve_global_target


def _args(**kw):
    base = dict(global_cfg=False, cfg_path=None, push=False)
    base.update(kw)
    return SimpleNamespace(**base)


# --- _resolve_global_target -------------------------------------------------


def test_target_none_without_global():
    assert _resolve_global_target(_args()) is None


def test_target_resolves_repo_stations_cfg(tmp_path):
    repo = tmp_path / "cfgdata"
    with patch("receivers.cfg.global_sync.resolve_global_repo", return_value=repo):
        out = _resolve_global_target(_args(global_cfg=True))
    assert out == repo / "stations.cfg"


def test_global_and_cfg_path_mutually_exclusive():
    with pytest.raises(CfgOperationError, match="mutually exclusive"):
        _resolve_global_target(_args(global_cfg=True, cfg_path="/some/path"))


# --- _maybe_commit_global ---------------------------------------------------


def test_commit_noop_without_global():
    # No --global → never calls git, even if changed.
    with patch("receivers.cfg.global_sync.git_commit_cfg") as gc:
        _maybe_commit_global(_args(), "msg", changed=True, dry_run=False)
    gc.assert_not_called()


def test_commit_called_when_global_and_changed(tmp_path, capsys):
    repo = tmp_path / "cfgdata"
    with (
        patch("receivers.cfg.global_sync.resolve_global_repo", return_value=repo),
        patch(
            "receivers.cfg.global_sync.git_commit_cfg",
            return_value={"committed": True, "commit": "abc1234", "pushed": False},
        ) as gc,
    ):
        _maybe_commit_global(
            _args(global_cfg=True), "stations(ROTH): x", changed=True, dry_run=False
        )
    gc.assert_called_once()
    # message + paths threaded through
    assert gc.call_args.args[1] == ["stations.cfg"]
    assert "committed abc1234" in capsys.readouterr().out


def test_commit_skipped_when_global_but_no_change(tmp_path, capsys):
    repo = tmp_path / "cfgdata"
    with (
        patch("receivers.cfg.global_sync.resolve_global_repo", return_value=repo),
        patch("receivers.cfg.global_sync.git_commit_cfg") as gc,
    ):
        _maybe_commit_global(
            _args(global_cfg=True), "msg", changed=False, dry_run=False
        )
    gc.assert_not_called()  # nothing changed → no commit attempt
    assert "no cfg changes" in capsys.readouterr().out


def test_dry_run_previews_without_commit(tmp_path, capsys):
    repo = tmp_path / "cfgdata"
    with (
        patch("receivers.cfg.global_sync.resolve_global_repo", return_value=repo),
        patch(
            "receivers.cfg.global_sync.git_commit_cfg",
            return_value={"committed": False, "dry_run": True, "diff": "some diff"},
        ) as gc,
    ):
        _maybe_commit_global(_args(global_cfg=True), "msg", changed=True, dry_run=True)
    # dry-run calls git_commit_cfg with dry_run=True (preview), never commits
    assert gc.call_args.kwargs.get("dry_run") is True
    assert "would commit" in capsys.readouterr().out


def test_push_flag_forwarded(tmp_path):
    repo = tmp_path / "cfgdata"
    with (
        patch("receivers.cfg.global_sync.resolve_global_repo", return_value=repo),
        patch(
            "receivers.cfg.global_sync.git_commit_cfg",
            return_value={"committed": True, "commit": "abc", "pushed": True},
        ) as gc,
    ):
        _maybe_commit_global(
            _args(global_cfg=True, push=True), "msg", changed=True, dry_run=False
        )
    assert gc.call_args.kwargs.get("push") is True
