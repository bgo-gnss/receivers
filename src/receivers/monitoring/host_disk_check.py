"""Icinga/Nagios check for rek-d01 host disk usage + scheduler liveness.

The durable, out-of-process "watch it while I'm away" alert that would have
caught the 2026-07-21 outage: ``/home`` (the small OS LV) filled to 100%, every
scheduler write failed with ``OSError: [Errno 28]``, yet systemd still reported
the unit ``active (running)`` and downloads silently stopped for ~2 days.

Two independent signals, both queryable with zero DB/log-scraping:

  1. **Host disk usage** — ``shutil.disk_usage()`` per configured mount. WARN at
     ``--warn-pct`` (default 85), CRIT at ``--crit-pct`` (default 92). Covers the
     OS volumes the app's own guardrails ignore (``local_prune`` only watches
     ``data_prepath``; the receiver-side disk checks watch the *stations*, not the
     server). ``/mnt/rawgpsdata`` is deliberately NOT a default mount — it sits
     chronically ~96% and is tracked by days-to-full forecast instead (todo #73).

  2. **Scheduler liveness** — age of the newest *activity* file (the download
     audit trail and, once it exists, the scheduler heartbeat). During the freeze
     these stopped advancing while the process stayed "active", so a stale
     activity age is the exact "active but wedged" detector. WARN past
     ``--activity-warn-minutes`` (default 20), CRIT at ``--activity-crit-minutes``
     (default 60 — live downloads run at least hourly, so 60 min idle is wedged).

Why out-of-process: the frozen scheduler's own logging died with the disk, so any
in-scheduler watchdog would have been mute too. This runs on its own systemd
timer; the pushed ``--ttl`` makes Icinga flag the service stale if THIS timer
also stops — so nothing in the chain fails silently.

The check is STATELESS — Icinga owns renotification/dedup and (via ``ttl``)
staleness. Also usable as a plain Nagios plugin (exit 0/1/2/3 +
``output | perfdata``) for check_by_ssh.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("receivers.monitoring.host_disk_check")

NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3

_LABEL = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}

# Default OS/data mounts to watch on rek-d01. Overridable with --mount (repeat).
# /mnt/rawgpsdata excluded on purpose (chronic ~96%, forecast-tracked — todo #73).
DEFAULT_MOUNTS = ["/", "/home", "/var", "/mnt/data"]

# Default activity files whose freshness proves the scheduler is doing work.
# The audit trail exists today; the heartbeat is added by _schedule_heartbeat()
# (build step 3). Newest mtime across whichever exist wins.
DEFAULT_ACTIVITY_FILES = [
    "~/.cache/gps_receivers/logs/download_audit.jsonl",
    "~/.cache/gps_receivers/heartbeat",
]


@dataclass
class HostHealthResult:
    """Outcome of a host disk + liveness evaluation."""

    exit_status: int
    summary: str  # one-line reason(s), worst-first
    perfdata: str  # Nagios performance data
    reasons: List[str] = field(default_factory=list)

    @property
    def plugin_output(self) -> str:
        return f"{_LABEL[self.exit_status]} - {self.summary}"


def _sanitize(label: str) -> str:
    """Make a mount path safe as a perfdata label key (``/home`` -> ``home``)."""
    return label.strip("/").replace("/", "_") or "root"


def evaluate_disk(
    mounts: List[str],
    *,
    warn_pct: float,
    crit_pct: float,
) -> tuple[int, List[str], List[str]]:
    """Return (worst_exit, reasons, perfdata_tokens) for the given mounts.

    A mount that does not exist is reported UNKNOWN (a vanished mount is itself a
    problem worth surfacing), not silently skipped.
    """
    worst = NAGIOS_OK
    reasons: List[str] = []
    perf: List[str] = []
    for m in mounts:
        p = Path(m)
        key = _sanitize(m)
        if not p.is_dir():
            worst = max(worst, NAGIOS_UNKNOWN)
            reasons.append(f"{m} not present")
            perf.append(f"disk_{key}=U")
            continue
        try:
            usage = shutil.disk_usage(p)
        except OSError as exc:
            worst = max(worst, NAGIOS_UNKNOWN)
            reasons.append(f"{m} unreadable ({exc})")
            perf.append(f"disk_{key}=U")
            continue
        pct = usage.used / usage.total * 100.0 if usage.total else 0.0
        # perfdata: value%;warn;crit;0;100
        perf.append(f"disk_{key}={pct:.0f}%;{warn_pct:.0f};{crit_pct:.0f};0;100")
        if pct >= crit_pct:
            worst = max(worst, NAGIOS_CRITICAL)
            reasons.append(f"{m} {pct:.0f}% (CRIT >= {crit_pct:.0f})")
        elif pct >= warn_pct:
            worst = max(worst, NAGIOS_WARNING)
            reasons.append(f"{m} {pct:.0f}% (WARN >= {warn_pct:.0f})")
    return worst, reasons, perf


def evaluate_liveness(
    activity_files: List[str],
    *,
    warn_minutes: float,
    crit_minutes: float,
) -> tuple[int, List[str], List[str]]:
    """Return (exit, reasons, perfdata_tokens) from the newest activity file age.

    Uses the most-recent mtime across the given files (whichever exist). If none
    exist, that is UNKNOWN — we cannot prove liveness either way.
    """
    now = time.time()
    newest: Optional[float] = None
    for f in activity_files:
        p = Path(f).expanduser()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return (
            NAGIOS_UNKNOWN,
            ["no scheduler activity file found (cannot assess liveness)"],
            ["activity_age_min=U"],
        )
    age_min = max(0.0, (now - newest) / 60.0)
    perf = [f"activity_age_min={age_min:.0f};{warn_minutes:.0f};{crit_minutes:.0f}"]
    if age_min >= crit_minutes:
        return (
            NAGIOS_CRITICAL,
            [
                f"no scheduler activity for {age_min:.0f} min (CRIT >= {crit_minutes:.0f})"
            ],
            perf,
        )
    if age_min >= warn_minutes:
        return (
            NAGIOS_WARNING,
            [
                f"no scheduler activity for {age_min:.0f} min (WARN >= {warn_minutes:.0f})"
            ],
            perf,
        )
    return NAGIOS_OK, [], perf


def evaluate_forecast(
    volumes: List[str],
    *,
    state_path: str,
    warn_days: float,
    crit_days: float,
    today=None,
) -> tuple[int, List[str], List[str]]:
    """Return (exit, reasons, perfdata_tokens) from days-to-full trajectory.

    Complements the percent-used signal: catches a mount that is chronically full
    but slowly filling (e.g. ``/mnt/rawgpsdata`` ~96%, todo #73), where a static
    percent threshold either always fires or never does. Reuses
    ``archive.prune.record_and_forecast`` (the same estimator + ``disk_history.json``
    state that ``local_prune`` already writes daily), so this just surfaces the
    existing forecast to Icinga. Lower days-to-full is worse: CRIT at
    ``<= crit_days``, WARN at ``<= warn_days``. ``None`` (not filling / no trend
    yet) is OK. Empty ``volumes`` disables the signal (returns OK, no perfdata).
    """
    if not volumes:
        return NAGIOS_OK, [], []
    try:
        from ..archive.prune import record_and_forecast
    except Exception as exc:  # noqa: BLE001 - forecast is optional, degrade cleanly
        return NAGIOS_UNKNOWN, [f"forecast unavailable ({exc})"], ["days_to_full=U"]

    worst = NAGIOS_OK
    reasons: List[str] = []
    perf: List[str] = []
    for v in volumes:
        key = _sanitize(v)
        try:
            dtf = record_and_forecast(
                Path(v),
                Path(state_path).expanduser(),
                warn_days_to_full=int(warn_days),
                today=today,
            )
        except Exception as exc:  # noqa: BLE001 - one volume failing != crash
            worst = max(worst, NAGIOS_UNKNOWN)
            reasons.append(f"{v} forecast failed ({exc})")
            perf.append(f"days_to_full_{key}=U")
            continue
        if dtf is None:
            perf.append(f"days_to_full_{key}=U")  # not filling / no baseline yet
            continue
        perf.append(f"days_to_full_{key}={dtf:.0f};{warn_days:.0f};{crit_days:.0f}")
        if dtf <= crit_days:
            worst = max(worst, NAGIOS_CRITICAL)
            reasons.append(f"{v} ~{dtf:.0f}d to full (CRIT <= {crit_days:.0f})")
        elif dtf <= warn_days:
            worst = max(worst, NAGIOS_WARNING)
            reasons.append(f"{v} ~{dtf:.0f}d to full (WARN <= {warn_days:.0f})")
    return worst, reasons, perf


def _rank(code: int) -> int:
    """Urgency rank so a real WARN/CRIT outranks an UNKNOWN in worst-of."""
    return {NAGIOS_OK: 0, NAGIOS_UNKNOWN: 1, NAGIOS_WARNING: 2, NAGIOS_CRITICAL: 3}[
        code
    ]


def evaluate_host(
    *,
    mounts: List[str],
    activity_files: List[str],
    warn_pct: float,
    crit_pct: float,
    activity_warn_minutes: float,
    activity_crit_minutes: float,
    forecast_volumes: Optional[List[str]] = None,
    forecast_state: str = "~/.cache/gps_receivers/disk_history.json",
    forecast_warn_days: float = 21.0,
    forecast_crit_days: float = 7.0,
    today=None,
) -> HostHealthResult:
    """Combine disk + liveness + (optional) days-to-full forecast, worst-of."""
    d_exit, d_reasons, d_perf = evaluate_disk(
        mounts, warn_pct=warn_pct, crit_pct=crit_pct
    )
    l_exit, l_reasons, l_perf = evaluate_liveness(
        activity_files,
        warn_minutes=activity_warn_minutes,
        crit_minutes=activity_crit_minutes,
    )
    f_exit, f_reasons, f_perf = evaluate_forecast(
        forecast_volumes or [],
        state_path=forecast_state,
        warn_days=forecast_warn_days,
        crit_days=forecast_crit_days,
        today=today,
    )

    exit_status = max((d_exit, l_exit, f_exit), key=_rank)
    reasons = d_reasons + l_reasons + f_reasons
    if not reasons:
        reasons = ["disk + liveness OK"]
    summary = "; ".join(reasons)
    perfdata = " ".join(d_perf + l_perf + f_perf)
    return HostHealthResult(
        exit_status=exit_status,
        summary=summary,
        perfdata=perfdata,
        reasons=reasons,
    )


def push_to_icinga(
    result: HostHealthResult,
    *,
    icinga_host: str,
    check_name: str = "Host disk and liveness",
    ttl: Optional[int] = None,
) -> bool:
    """Push the result to Icinga as a passive check. Returns True on success.

    ``ttl`` (seconds) makes Icinga mark the service stale if no fresh result
    arrives in time — the timer/host-down detector. Best-effort.
    """
    try:
        from .icinga_client import CheckResult, IcingaClient

        check = CheckResult(
            station=icinga_host,
            check_name=check_name,
            exit_status=result.exit_status,
            plugin_output=result.plugin_output,
            performance_data=result.perfdata,
            ttl=ttl,
        )
        resp = IcingaClient().send_check_result(check)
        ok = bool(resp.get("success")) if isinstance(resp, dict) else bool(resp)
        if not ok:
            logger.warning("Icinga push did not succeed: %s", resp)
        return ok
    except Exception as exc:  # noqa: BLE001 - alert must never crash the timer
        logger.warning("Icinga push failed: %s", exc)
        return False


def main() -> None:
    """Nagios-plugin entry: print ``output | perfdata`` and exit 0/1/2/3.

    With --icinga, also push the result as a passive check. Intended to be run by
    the gps-host-monitor systemd timer on rek-d01.
    """
    parser = argparse.ArgumentParser(
        description="Icinga/Nagios check for rek-d01 host disk usage + scheduler liveness"
    )
    parser.add_argument(
        "--mount",
        action="append",
        dest="mounts",
        metavar="PATH",
        help=f"mount to check (repeatable); default: {' '.join(DEFAULT_MOUNTS)}",
    )
    parser.add_argument("--warn-pct", type=float, default=85.0)
    parser.add_argument("--crit-pct", type=float, default=92.0)
    parser.add_argument(
        "--activity-file",
        action="append",
        dest="activity_files",
        metavar="PATH",
        help="activity file whose freshness proves liveness (repeatable); "
        f"default: {' '.join(DEFAULT_ACTIVITY_FILES)}",
    )
    parser.add_argument("--activity-warn-minutes", type=float, default=20.0)
    parser.add_argument("--activity-crit-minutes", type=float, default=60.0)
    parser.add_argument(
        "--icinga", action="store_true", help="also push a passive result to Icinga"
    )
    parser.add_argument(
        "--icinga-host",
        default="rek-d01",
        help="Icinga host object for the pushed service (default: rek-d01)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="Icinga staleness TTL in seconds (recommend ~3x the timer interval)",
    )
    parser.add_argument(
        "--forecast-volume",
        action="append",
        dest="forecast_volumes",
        metavar="PATH",
        help="volume to forecast days-to-full (repeatable); off by default. "
        "Use for chronically-full-but-growing mounts like /mnt/rawgpsdata.",
    )
    parser.add_argument(
        "--forecast-state",
        default="~/.cache/gps_receivers/disk_history.json",
        help="JSON history file for the forecast (shared with local_prune)",
    )
    parser.add_argument("--forecast-warn-days", type=float, default=21.0)
    parser.add_argument("--forecast-crit-days", type=float, default=7.0)
    args = parser.parse_args()

    mounts = args.mounts or list(DEFAULT_MOUNTS)
    activity_files = args.activity_files or list(DEFAULT_ACTIVITY_FILES)

    try:
        result = evaluate_host(
            mounts=mounts,
            activity_files=activity_files,
            warn_pct=args.warn_pct,
            crit_pct=args.crit_pct,
            activity_warn_minutes=args.activity_warn_minutes,
            activity_crit_minutes=args.activity_crit_minutes,
            forecast_volumes=args.forecast_volumes,
            forecast_state=args.forecast_state,
            forecast_warn_days=args.forecast_warn_days,
            forecast_crit_days=args.forecast_crit_days,
        )
        if args.icinga:
            push_to_icinga(result, icinga_host=args.icinga_host, ttl=args.ttl)
    except Exception as exc:  # noqa: BLE001 - degrade to UNKNOWN, never crash
        print(f"UNKNOWN - host-disk check failed: {exc}")
        sys.exit(NAGIOS_UNKNOWN)

    print(f"{result.plugin_output} | {result.perfdata}")
    sys.exit(result.exit_status)


if __name__ == "__main__":
    main()
