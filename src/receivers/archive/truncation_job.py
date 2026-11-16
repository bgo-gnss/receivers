"""Scheduled, READ-ONLY truncation scan: ``archive-truncation plan`` on a timer.

The job never repairs anything. It builds the same plan an operator would with
``receivers archive-truncation plan``, saves it, and logs what it found:

* nothing broken -> one INFO line;
* anything broken -> a WARNING summary plus one WARNING per file, and the exact
  ``apply`` command for the saved plan. Repair stays a human decision.

Logger: ``receivers.scheduler.truncation``. Plans go to ``plan_dir`` (default
``~/.cache/gps_receivers/truncation_plans``); only the newest ``keep_plans``
are kept.

Configured in scheduler.yaml ``truncation_scan`` (disabled unless enabled):

    truncation_scan:
      enabled: true
      schedule: "cron: 40 6 * * 1"   # Monday 06:40
      days: 30
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("receivers.scheduler.truncation")

PLAN_PREFIX = "truncation-plan-"


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
    _build_plan=None,
    _connect=None,
) -> Optional[str]:
    """Scan the last ``days`` days read-only and save a plan. Never raises out.

    Module-level callable (APScheduler persists jobs by import path). Returns
    the saved plan path when something broken was found, else None.
    """
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
            return None
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
        if not plan.items:
            logger.info(
                "truncation scan %s..%s: no truncated or corrupt raw "
                "(%d files, %d probed)",
                start,
                end,
                stats.get("files", 0),
                stats.get("probed", 0),
            )
            return None

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
        return path
    except Exception:  # noqa: BLE001 — a scheduled job must never raise
        logger.exception("truncation scan failed")
        return None
