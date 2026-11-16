"""The scheduled truncation scan: read-only, saves a plan, logs, never raises."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from receivers.archive import truncation_job as tj


def _item(action="salvage"):
    return SimpleNamespace(
        action=action,
        rel="2026/oct/VONC/1Hz_1hr/raw/VONC202610221700b.sbf.gz",
        receiver={"state": "gone"},
        decision={"reason": "receiver no longer holds it"},
    )


def _plan(items):
    return SimpleNamespace(
        items=items,
        stats={"files": 10, "probed": 2, "actions": {"salvage": len(items)}},
        to_json=lambda: '{"plan": true}',
    )


class _Conn:
    closed = False

    def close(self):
        _Conn.closed = True


def _run(tmp_path, items, **kw):
    seen = {}

    def build(conn, **kwargs):
        seen.update(kwargs)
        return _plan(items)

    path = tj.run_truncation_scan_job(
        read_root=str(tmp_path),
        local_root="/local",
        plan_dir=str(tmp_path / "plans"),
        _build_plan=build,
        _connect=_Conn,
        **kw,
    )
    return path, seen


@pytest.mark.unit
def test_clean_scan_logs_info_and_writes_no_plan(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="receivers.scheduler.truncation")
    path, seen = _run(tmp_path, [])
    assert path is None
    assert not (tmp_path / "plans").exists()
    assert "no truncated or corrupt raw" in caplog.text
    assert seen["archive_root"] == str(tmp_path) and seen["local_root"] == "/local"


@pytest.mark.unit
def test_findings_save_a_plan_and_warn_with_the_apply_command(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="receivers.scheduler.truncation")
    path, _ = _run(tmp_path, [_item(), _item("refetch")])
    assert path and open(path).read() == '{"plan": true}'
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3  # summary + one per file
    assert f"receivers archive-truncation apply {path}" in warnings[0].getMessage()
    assert "REFETCH" in warnings[2].getMessage()


@pytest.mark.unit
def test_only_the_newest_plans_are_kept(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    for i in range(5):
        (plans / f"{tj.PLAN_PREFIX}2026010{i}T000000.json").write_text("{}")
    path, _ = _run(tmp_path, [_item()], keep_plans=3)
    left = sorted(p.name for p in plans.iterdir())
    assert len(left) == 3 and path.endswith(left[-1])


@pytest.mark.unit
def test_unmounted_archive_skips_without_scanning(tmp_path, caplog):
    called = []
    out = tj.run_truncation_scan_job(
        read_root=str(tmp_path / "not-mounted"),
        local_root="/local",
        _build_plan=lambda *a, **k: called.append(1),
        _connect=_Conn,
    )
    assert out is None and not called
    assert "not mounted" in caplog.text


@pytest.mark.unit
def test_a_failing_scan_never_raises(tmp_path, caplog):
    def boom(conn, **kw):
        raise RuntimeError("db down")

    out = tj.run_truncation_scan_job(
        read_root=str(tmp_path), local_root="/l", _build_plan=boom, _connect=_Conn
    )
    assert out is None and "truncation scan failed" in caplog.text
    assert _Conn.closed


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def _fake_scheduler(cfg):
    return SimpleNamespace(
        yaml_config=cfg,
        scheduler=MagicMock(),
        logger=MagicMock(),
        _mark_disabled=MagicMock(),
    )


@pytest.mark.unit
def test_registrar_is_disabled_by_default():
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    fake = _fake_scheduler({})
    BulkDownloadScheduler._schedule_truncation_scan(fake)
    fake._mark_disabled.assert_called_once_with("Truncation scan", "truncation_scan")
    fake.scheduler.add_job.assert_not_called()


@pytest.mark.unit
def test_registrar_schedules_the_read_only_job():
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    fake = _fake_scheduler(
        {
            "truncation_scan": {
                "enabled": True,
                "schedule": "cron: 40 6 * * 1",
                "days": 14,
            }
        }
    )
    BulkDownloadScheduler._schedule_truncation_scan(fake)
    kw = fake.scheduler.add_job.call_args.kwargs
    assert kw["func"] is tj.run_truncation_scan_job
    assert kw["id"] == "truncation_scan" and kw["executor"] == "backfill"
    assert kw["max_instances"] == 1
    assert kw["kwargs"]["days"] == 14
    assert kw["trigger"] == "cron" and kw["minute"] == "40" and kw["hour"] == "6"


@pytest.mark.unit
def test_the_registrar_is_called_when_the_scheduler_sets_up():
    """The orphaned-abstraction trap: a registrar nobody calls is dead code."""
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    src = inspect.getsource(BulkDownloadScheduler.schedule_all_sessions)
    assert "self._schedule_truncation_scan()" in src
