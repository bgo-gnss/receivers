"""Scheduled, READ-ONLY truncation scan: ``archive-truncation plan`` on a timer.

The job never repairs anything. It builds the same plan an operator would with
``receivers archive-truncation plan``, saves it, logs what it found and (when
``icinga`` is on) pushes one passive check result:

* nothing broken -> INFO line; Icinga OK;
* anything broken -> a WARNING summary plus one WARNING per file, the exact
  ``apply`` command for the saved plan; Icinga WARNING. Repair stays a human
  decision;
* the scan could not run (archive not mounted, database down, any error) ->
  ERROR log; Icinga UNKNOWN. Never silently "clean".

The Icinga result carries a TTL (default 8 days), so a weekly run that never
happens (scheduler down, job lost) goes stale instead of staying green.
Service: ``<icinga_host>.gps.vedur.is!Truncation scan`` — the passive service
object must exist on the Icinga server (docs/monitoring/truncation-scan-alert.md).

Logger: ``receivers.scheduler.truncation``. Plans go to ``plan_dir`` (default
``~/.cache/gps_receivers/truncation_plans``); only the newest ``keep_plans``
are kept.

Configured in scheduler.yaml ``truncation_scan`` (disabled unless enabled):

    truncation_scan:
      enabled: true
      schedule: "cron: 40 6 * * 1"   # Monday 06:40 (standard cron weekdays)
      days: 30
      icinga: true                   # push "Truncation scan" for icinga_host
      icinga_host: rek-d01
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger("receivers.scheduler.truncation")

PLAN_PREFIX = "truncation-plan-"
CHECK_NAME = "Truncation scan"
DEFAULT_TTL = 8 * 86400  # one weekly run plus a day

EXIT_OK, EXIT_WARNING, EXIT_UNKNOWN = 0, 1, 3
_LABEL = {EXIT_OK: "OK", EXIT_WARNING: "WARNING", EXIT_UNKNOWN: "UNKNOWN"}


@dataclass(frozen=True)
class ScanOutcome:
    """What one scan established — the Icinga result is derived from this."""

    exit_status: int
    summary: str
    perfdata: str = ""
    plan_path: Optional[str] = None

    @property
    def plugin_output(self) -> str:
        return f"{_LABEL[self.exit_status]} - {self.summary}"


def default_plan_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "gps_receivers", "truncation_plans")


def _prune_plans(plan_dir: str, keep: int) -> None:
    plans = sorted(
        f
        for f in os.listdir(plan_dir)
        if f.startswith(PLAN_PREFIX) and f.endswith(".json")
    )
    for old in plans[: max(0, len(plans) - keep)]:
        try:
            os.unlink(os.path.join(plan_dir, old))
        except OSError:
            pass


def _perfdata(stats: dict, broken: int) -> str:
    actions = stats.get("actions", {}) or {}
    parts = [
        f"broken={broken}",
        *(
            f"{a}={actions.get(a, 0)}"
            for a in ("replace", "refetch", "deferred", "salvage", "unrecoverable")
        ),
        f"files={stats.get('files', 0)}",
        f"probed={stats.get('probed', 0)}",
    ]
    return " ".join(parts)


def push_to_icinga(outcome: ScanOutcome, *, icinga_host: str, ttl: int) -> bool:
    """Push ``outcome`` as a passive check result. Best-effort: never raises."""
    try:
        from ..monitoring.icinga_client import CheckResult, IcingaClient

        check = CheckResult(
            station=icinga_host,
            check_name=CHECK_NAME,
            exit_status=outcome.exit_status,
            plugin_output=outcome.plugin_output,
            performance_data=outcome.perfdata,
            ttl=ttl,
        )
        resp = IcingaClient().send_check_result(check)
        ok = bool(resp.get("success")) if isinstance(resp, dict) else bool(resp)
        if not ok:
            logger.warning("truncation scan: Icinga push did not succeed: %s", resp)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("truncation scan: Icinga push failed: %s", exc)
        return False


def scan(
    *,
    days: int,
    sessions: Optional[list],
    settle_hours: float,
    horizon_max_age_hours: float,
    read_root: Optional[str],
    local_root: Optional[str],
    plan_dir: Optional[str],
    keep_plans: int,
    host: Optional[str],
    _build_plan=None,
    _connect=None,
) -> ScanOutcome:
    """Run the read-only scan and say what it found. Never raises."""
    try:
        from .truncation_scan import RAW_SESSIONS, build_plan

        build = _build_plan or build_plan
        if read_root is None:
            from ..scheduling.config_loader import load_scheduler_config

            read_root = (load_scheduler_config().get("archive_verify") or {}).get(
                "read_root"
            )
        if not read_root or not os.path.isdir(read_root):
            logger.error(
                "truncation scan skipped: archive read root %r is not mounted "
                "(set truncation_scan.read_root or archive_verify.read_root)",
                read_root,
            )
            return ScanOutcome(
                EXIT_UNKNOWN,
                f"scan not run: archive read root {read_root!r} not mounted",
            )
        if local_root is None:
            from ..config.receivers_config import get_receivers_config

            local_root = get_receivers_config().get_data_prepath()

        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        if _connect is None:
            from ..db.connection import get_connection

            conn = get_connection(host_override=host, single_host=True)
        else:
            conn = _connect()
        try:
            plan = build(
                conn,
                local_root=local_root.rstrip("/"),
                archive_root=read_root.rstrip("/"),
                start=start,
                end=end,
                sessions=tuple(sessions or RAW_SESSIONS),
                settle_hours=settle_hours,
                horizon_max_age_hours=horizon_max_age_hours,
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        stats = plan.stats
        perf = _perfdata(stats, len(plan.items))
        if not plan.items:
            logger.info(
                "truncation scan %s..%s: no truncated or corrupt raw "
                "(%d files, %d probed)",
                start,
                end,
                stats.get("files", 0),
                stats.get("probed", 0),
            )
            return ScanOutcome(
                EXIT_OK,
                f"no truncated raw files {start}..{end} "
                f"({stats.get('files', 0)} files checked)",
                perf,
            )

        plan_dir = plan_dir or default_plan_dir()
        os.makedirs(plan_dir, exist_ok=True)
        path = os.path.join(
            plan_dir, f"{PLAN_PREFIX}{datetime.now():%Y%m%dT%H%M%S}.json"
        )
        with open(path, "w") as fh:
            fh.write(plan.to_json())
        _prune_plans(plan_dir, keep_plans)

        logger.warning(
            "truncation scan %s..%s: %d broken raw file(s) %s — review, then: "
            "receivers archive-truncation apply %s --catalog-prod  (add --yes to write)",
            start,
            end,
            len(plan.items),
            stats.get("actions", {}),
            path,
        )
        for it in plan.items:
            logger.warning(
                "truncation scan: %s %s (receiver %s) — %s",
                it.action.upper(),
                it.rel,
                it.receiver.get("state"),
                it.decision.get("reason"),
            )
        names = ", ".join(it.rel.rsplit("/", 1)[-1] for it in plan.items[:5])
        more = f" +{len(plan.items) - 5} more" if len(plan.items) > 5 else ""
        return ScanOutcome(
            EXIT_WARNING,
            f"{len(plan.items)} truncated raw file(s): {names}{more}. "
            f"Review: receivers archive-truncation apply {path}",
            perf,
            path,
        )
    except Exception as exc:  # noqa: BLE001 — a scheduled job must never raise
        logger.exception("truncation scan failed")
        return ScanOutcome(EXIT_UNKNOWN, f"scan failed: {exc}"[:300])


def run_truncation_scan_job(
    days: int = 30,
    sessions: Optional[list] = None,
    settle_hours: float = 6.0,
    horizon_max_age_hours: float = 48.0,
    read_root: Optional[str] = None,
    local_root: Optional[str] = None,
    plan_dir: Optional[str] = None,
    keep_plans: int = 8,
    host: Optional[str] = None,
    icinga: bool = False,
    icinga_host: str = "rek-d01",
    icinga_ttl: int = DEFAULT_TTL,
    _build_plan=None,
    _connect=None,
    _push: Optional[Callable[..., bool]] = None,
) -> Optional[str]:
    """Scheduled entry point. Never raises out.

    Module-level callable (APScheduler persists jobs by import path). Returns
    the saved plan path when something broken was found, else None.
    """
    outcome = scan(
        days=days,
        sessions=sessions,
        settle_hours=settle_hours,
        horizon_max_age_hours=horizon_max_age_hours,
        read_root=read_root,
        local_root=local_root,
        plan_dir=plan_dir,
        keep_plans=keep_plans,
        host=host,
        _build_plan=_build_plan,
        _connect=_connect,
    )
    if icinga:
        (_push or push_to_icinga)(outcome, icinga_host=icinga_host, ttl=icinga_ttl)
    return outcome.plan_path
