"""Tests for receivers.cfg.global_sync — gps-config-data repo write + git commit.

Uses a real temp git repo (no network for push tests — push targets a second
local bare repo). The helper sets its own git identity, so a fresh `git init`
works without host config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from receivers.cfg.global_sync import (
    CfgOperationError,
    git_commit_cfg,
    resolve_global_repo,
)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q", "-b", "main")
    (path / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.5.0\n")
    _run(path, "-c", "user.name=t", "-c", "user.email=t@t", "add", "stations.cfg")
    _run(
        path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"
    )
    return path


# --- resolve_global_repo ----------------------------------------------------


def test_resolve_explicit_repo(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    assert resolve_global_repo(str(repo)) == repo


def test_resolve_env_var(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "cfgdata")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(repo))
    assert resolve_global_repo() == repo


def test_resolve_missing_repo_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(tmp_path / "nope"))
    with pytest.raises(CfgOperationError, match="not found"):
        resolve_global_repo()


def test_resolve_not_a_git_worktree_errors(tmp_path, monkeypatch):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "stations.cfg").write_text("[X]\n")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(d))
    with pytest.raises(CfgOperationError, match="not a git"):
        resolve_global_repo()


def test_resolve_no_stations_cfg_errors(tmp_path, monkeypatch):
    d = tmp_path / "norepo"
    d.mkdir()
    _run(d, "init", "-q")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(d))
    with pytest.raises(CfgOperationError, match="no stations.cfg"):
        resolve_global_repo()


# --- git_commit_cfg ---------------------------------------------------------


def test_commit_changes(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "stations(ROTH): fw 5.7.0")
    assert res["committed"] is True
    assert res["commit"]
    # the change is the new HEAD
    msg = _run(repo, "log", "-1", "--format=%s")
    assert "fw 5.7.0" in msg


def test_no_op_when_clean(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    res = git_commit_cfg(repo, ["stations.cfg"], "no change")
    assert res["committed"] is False
    assert res.get("reason") == "no changes"


def test_dry_run_shows_diff_no_commit(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    head_before = _run(repo, "rev-parse", "HEAD")
    res = git_commit_cfg(repo, ["stations.cfg"], "msg", dry_run=True)
    assert res["committed"] is False
    assert res["dry_run"] is True
    assert "5.7.0" in res["diff"]
    assert _run(repo, "rev-parse", "HEAD") == head_before  # nothing committed


def test_detached_head_refused(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    sha = _run(repo, "rev-parse", "HEAD").strip()
    _run(repo, "checkout", "-q", sha)  # detach
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    with pytest.raises(CfgOperationError, match="detached"):
        git_commit_cfg(repo, ["stations.cfg"], "msg")


def test_push_to_local_bare(tmp_path):
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    repo = _init_repo(tmp_path / "cfgdata")
    _run(repo, "remote", "add", "origin", str(bare))
    _run(repo, "push", "-q", "-u", "origin", "main")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "stations(ROTH): fw", push=True)
    assert res["committed"] is True
    assert res["pushed"] is True
    # the bare remote now has the commit (ref the pushed branch explicitly —
    # the bare repo's default HEAD may point at an unborn 'master')
    remote_head = _run(bare, "log", "-1", "--format=%s", "main")
    assert "fw" in remote_head


def test_push_failure_keeps_commit(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")  # no remote configured
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "msg", push=True)
    assert res["committed"] is True  # commit stands
    assert res["pushed"] is False
    assert res.get("push_error")  # failure surfaced, not raised
