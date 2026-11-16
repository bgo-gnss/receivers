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


# ---------------------------------------------------------------------------
# Icinga
# ---------------------------------------------------------------------------


def _pushes(tmp_path, items=None, *, build=None, read_root=None, icinga=True):
    sent = []
    tj.run_truncation_scan_job(
        read_root=read_root or str(tmp_path),
        local_root="/local",
        plan_dir=str(tmp_path / "plans"),
        _build_plan=build or (lambda conn, **k: _plan(items or [])),
        _connect=_Conn,
        icinga=icinga,
        icinga_host="rek-d01",
        _push=lambda outcome, **kw: sent.append((outcome, kw)) or True,
    )
    return sent


@pytest.mark.unit
def test_clean_scan_pushes_ok_with_a_ttl(tmp_path):
    ((outcome, kw),) = _pushes(tmp_path, [])
    assert outcome.exit_status == tj.EXIT_OK
    assert outcome.plugin_output.startswith("OK - no truncated raw files")
    assert "broken=0" in outcome.perfdata
    assert kw == {"icinga_host": "rek-d01", "ttl": tj.DEFAULT_TTL}


@pytest.mark.unit
def test_findings_push_warning_with_the_plan_to_review(tmp_path):
    ((outcome, _),) = _pushes(tmp_path, [_item(), _item("refetch")])
    assert outcome.exit_status == tj.EXIT_WARNING
    assert "2 truncated raw file(s)" in outcome.plugin_output
    assert outcome.plan_path and outcome.plan_path in outcome.plugin_output
    assert "broken=2" in outcome.perfdata and "salvage=2" in outcome.perfdata


@pytest.mark.unit
def test_a_scan_that_could_not_run_is_unknown_never_ok(tmp_path):
    ((unmounted, _),) = _pushes(tmp_path, read_root=str(tmp_path / "nope"))
    assert (
        unmounted.exit_status == tj.EXIT_UNKNOWN and "not mounted" in unmounted.summary
    )

    def boom(conn, **kw):
        raise RuntimeError("db down")

    ((failed, _),) = _pushes(tmp_path, build=boom)
    assert failed.exit_status == tj.EXIT_UNKNOWN and "db down" in failed.summary


@pytest.mark.unit
def test_nothing_is_pushed_unless_icinga_is_enabled(tmp_path):
    assert _pushes(tmp_path, [_item()], icinga=False) == []


@pytest.mark.unit
def test_push_builds_the_rek_d01_truncation_scan_service(monkeypatch):
    from receivers.monitoring import icinga_client as ic

    sent = []
    monkeypatch.setattr(
        ic.IcingaClient, "__init__", lambda self, *a, **k: None, raising=True
    )
    monkeypatch.setattr(
        ic.IcingaClient,
        "send_check_result",
        lambda self, c: sent.append(c) or {"success": True},
    )
    out = tj.ScanOutcome(tj.EXIT_WARNING, "1 truncated raw file(s)", "broken=1")
    assert tj.push_to_icinga(out, icinga_host="rek-d01", ttl=691200) is True
    (c,) = sent
    assert c.to_service_name() == "rek-d01.gps.vedur.is!Truncation scan"
    assert (c.exit_status, c.ttl, c.performance_data) == (1, 691200, "broken=1")
    assert c.plugin_output == "WARNING - 1 truncated raw file(s)"


@pytest.mark.unit
def test_an_unreachable_icinga_never_breaks_the_job(monkeypatch):
    from receivers.monitoring import icinga_client as ic

    def boom(self, *a, **k):
        raise ConnectionError("icinga unreachable")

    monkeypatch.setattr(ic.IcingaClient, "__init__", boom)
    out = tj.ScanOutcome(tj.EXIT_OK, "clean")
    assert tj.push_to_icinga(out, icinga_host="rek-d01", ttl=1) is False


@pytest.mark.unit
def test_registrar_passes_the_icinga_settings():
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    fake = _fake_scheduler(
        {"truncation_scan": {"enabled": True, "icinga": True, "icinga_host": "rek-d01"}}
    )
    BulkDownloadScheduler._schedule_truncation_scan(fake)
    kw = fake.scheduler.add_job.call_args.kwargs["kwargs"]
    assert (kw["icinga"], kw["icinga_host"], kw["icinga_ttl"]) == (
        True,
        "rek-d01",
        8 * 86400,
    )
    # off unless configured: a dev scheduler must never report as rek-d01
    fake2 = _fake_scheduler({"truncation_scan": {"enabled": True}})
    BulkDownloadScheduler._schedule_truncation_scan(fake2)
    assert fake2.scheduler.add_job.call_args.kwargs["kwargs"]["icinga"] is False
