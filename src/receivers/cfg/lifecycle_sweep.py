"""Scheduled station-lifecycle drift sweep (report-only).

Step 3 of the 2026-10-10 ``station_status`` / TOS-inference architecture review.

**The gap this closes.** ``stations.cfg`` ``station_status`` gates whether the
scheduler polls a station at all, and it is hand-maintained. Steps 0–2 made the
common transitions self-correcting (``cfg move-device`` clears a stale
``inactive``; ``cfg sync-from-tos`` derives lifecycle from TOS), but both only
run when an operator happens to invoke them. Drift created any other way —
a receiver removed in the TOS web UI, a station decommissioned by another team,
an edit made directly in ``gps-config-data`` — accumulates silently. This sweep
makes it visible on a schedule.

**Report-only, by construction.** It never writes ``stations.cfg`` and never
writes TOS. Its only output is rows in ``cfg_discrepancy`` (tagged
``detected_by=lifecycle_sweep``), which ``cfg reconcile --open`` / ``cfg list``
and Icinga already surface. That is deliberate: the architecture review's
harshest finding was that an UNATTENDED lifecycle *writer* is dangerous, because
TOS entry lag can make a stale read look authoritative. An unattended READER is
harmless — worst case it reports a false alarm. Any actual fix stays a reviewed
operation (``cfg sync-from-tos``), where the same planner's directional-
asymmetry rule (activation auto-appliable; deactivation confirm-only) applies.

**Verdicts.** Drift is recorded with the same verdict vocabulary the rest of
``cfg_discrepancy`` uses, and the *detection values* are the output of
``_plan_station_status`` — the identical planner ``cfg sync-from-tos`` uses. So
a sweep finding says exactly what the fix command would propose, which is what
makes the report actionable:

* planner returns ``None``            → no row (and any open row auto-resolves)
* cfg empty, TOS has a lifecycle      → ``missing``
* cfg value present and disagrees     → ``conflict``
* TOS contradicts itself (``date_end``
  set AND an open receiver join)      → ``sources_disagree``

**Cost.** One ``get_complete_station_metadata`` per in-scope station (~190 on the
production fleet), run sequentially on the ``backfill`` executor. That is the
same order as ``cfg sync-from-tos --all``, and it is a nightly job — never a
per-minute one. Traffic is bounded deliberately: the sweep shares the
``backfill`` executor so it cannot compete with live downloads, and it is
gated off by default.

**Scope.** Only *operated* stations are meaningful (see
``_station_status_in_scope``): external/reference stations may legitimately have
no ``gnss_receiver`` in TOS at all, so deriving "inactive" for them would be
wrong. Out-of-scope stations produce no rows at all — not even auto-resolves,
since the sweep has no opinion about them.

Never raises: a sweep hiccup must not disturb the scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .discrepancy_log import DETECTED_BY_LIFECYCLE_SWEEP, LogSyncEntry, sync_station
from .reconciler import Verdict

logger = logging.getLogger(__name__)

#: cfg_key used for every row this sweep writes. One row per station (the
#: ``cfg_discrepancy`` natural key is ``(station_id, cfg_key)``).
CFG_KEY = "station_status"


@dataclass
class SweepResult:
    """Outcome of one sweep pass — for logs and tests, not persisted."""

    stations_checked: int = 0
    in_scope: int = 0
    drift: int = 0
    resolved: int = 0
    tos_failures: int = 0
    findings: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.stations_checked} checked, {self.in_scope} in scope, "
            f"{self.drift} drift, {self.resolved} converged, "
            f"{self.tos_failures} TOS-unavailable"
        )


def _cfg_lifecycle_fields() -> Dict[str, Dict[str, Any]]:
    """Every stations.cfg section's lifecycle-relevant fields, keyed by SID.

    Read directly with configparser rather than through
    ``gps_parser``/``get_station_config``, for two reasons:

    * ``get_station_config`` returns ``None`` for any station missing
      ``router_ip``/``receiver_type`` — exactly the shape a station whose
      receiver was just removed has, which is a case this sweep exists to
      catch. Enumerating sections ourselves sees it.
    * This pass needs three raw keys, not the full typed station config, and
      it runs over the whole fleet nightly — the light read is the right one.

    Returns ``{}`` on any read failure, which the caller treats as "no
    evidence" and skips the sweep (never as "every station was deleted").
    """
    import configparser

    from ..cli.cfg import _local_stations_cfg_path

    try:
        path = _local_stations_cfg_path()
        parser = configparser.ConfigParser(strict=False)
        parser.read(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("lifecycle-sweep: could not read stations.cfg: %s", exc)
        return {}

    fields: Dict[str, Dict[str, Any]] = {}
    for section in parser.sections():
        if not (len(section) == 4 and section.isupper()):
            continue
        fields[section] = {
            "station_status": parser.get(section, "station_status", fallback=None),
            "health_check": parser.get(section, "health_check", fallback=None),
            "station_role": parser.get(section, "station_role", fallback=None),
        }
    return fields


def _verdict_for(plan: tuple, cfg_status: Optional[str]) -> str:
    """Map a ``_plan_station_status`` result to a ``cfg_discrepancy`` verdict.

    The planner already decided *that* the two views disagree; this only picks
    the label. Isolating it keeps the mapping testable without TOS.
    """
    if plan[0] == "flag" and "hygiene" in str(plan[1]):
        # TOS contradicts itself (date_end set + open receiver join). That is
        # a sources-disagree condition, not a cfg-vs-TOS one.
        return Verdict.SOURCES_DISAGREE
    cfg = (cfg_status or "").strip()
    if not cfg:
        return Verdict.MISSING
    return Verdict.CONFLICT


def run_lifecycle_sweep(
    stations: Optional[Sequence[str]] = None,
    *,
    limit: Optional[int] = None,
) -> SweepResult:
    """One report-only lifecycle pass. See the module docstring.

    Args:
        stations: restrict to these markers (case-insensitive). ``None`` = the
            whole cfg fleet.
        limit: stop after this many stations. For manual/trial runs — the
            production schedule passes nothing.

    Returns:
        :class:`SweepResult`. Never raises — TOS failures are counted and
        skipped, because a partial sweep must still record what it did see.
    """
    # Lazy imports: the scheduler imports this module at job-dispatch time, and
    # cli.cfg pulls in the whole CLI surface (argparse wiring, operations, …).
    from ..cfg.probe import _query_tos
    from ..cfg.tos_adapter import current_lifecycle
    from ..cli.cfg import _plan_station_status, _station_status_in_scope

    result = SweepResult()
    cfg_fields = _cfg_lifecycle_fields()
    if not cfg_fields:
        logger.warning("lifecycle-sweep: no stations.cfg sections — skipped")
        return result

    sids = sorted(cfg_fields)
    if stations:
        wanted = {s.strip().upper() for s in stations if s.strip()}
        sids = [s for s in sids if s.upper() in wanted]
    if limit is not None:
        sids = sids[:limit]

    for sid in sids:
        fields = cfg_fields[sid]
        if not _station_status_in_scope(fields):
            continue  # external/reference: no valid lifecycle signal, no row
        result.stations_checked += 1
        result.in_scope += 1

        cfg_status = fields.get("station_status")
        try:
            tos_data = _query_tos(sid)
        except Exception as exc:  # noqa: BLE001 - one station must not end the pass
            logger.warning("lifecycle-sweep: TOS query failed for %s: %s", sid, exc)
            result.tos_failures += 1
            continue
        if not tos_data:
            # No TOS record: "absent TOS never clobbers cfg", and equally it is
            # not evidence of drift. Record nothing (leaves any open row alone).
            result.tos_failures += 1
            continue

        lifecycle, contradiction = current_lifecycle(tos_data)
        plan = _plan_station_status(cfg_status, lifecycle, contradiction, True)

        if plan is None:
            # In sync. Emit verdict=None so a previously-open row auto-resolves
            # — otherwise fixed drift would linger in `cfg reconcile --open`.
            entries = [LogSyncEntry(cfg_key=CFG_KEY, verdict=None)]
            result.resolved += sync_station(
                sid, entries, detected_by=DETECTED_BY_LIFECYCLE_SWEEP
            )
            continue

        verdict = _verdict_for(plan, cfg_status)
        result.drift += 1
        result.findings.append(
            {
                "station_id": sid,
                "cfg_value": cfg_status,
                "tos_value": lifecycle,
                "verdict": str(verdict),
                "proposed": f"{plan[0]}:{plan[1]}" if len(plan) > 1 else str(plan[0]),
            }
        )
        sync_station(
            sid,
            [
                LogSyncEntry(
                    cfg_key=CFG_KEY,
                    verdict=str(verdict),
                    cfg_value=cfg_status,
                    receiver_value=None,  # read-only sweep: no receiver probe
                    tos_value=lifecycle,
                )
            ],
            detected_by=DETECTED_BY_LIFECYCLE_SWEEP,
        )

    logger.info("lifecycle-sweep: %s", result.summary())
    return result


def _run_lifecycle_sweep_job(
    sweep_cfg: Optional[Dict[str, Any]] = None,
    station_filter: Optional[List[str]] = None,
) -> None:
    """APScheduler entry point. Module-level so it can be serialized.

    Honoured ``[lifecycle_sweep]`` keys: ``stations`` (explicit marker list —
    mostly for a manual/trial run) and ``limit``.

    Never raises: the scheduler must survive a bad sweep.
    """
    sweep_cfg = sweep_cfg or {}
    stations = sweep_cfg.get("stations") or station_filter
    try:
        result = run_lifecycle_sweep(stations, limit=sweep_cfg.get("limit"))
    except Exception:  # noqa: BLE001 - a sweep failure must not kill the job
        logger.exception("lifecycle-sweep: pass failed")
        return

    if result.findings:
        # Log the actionable lines explicitly. The rows are in cfg_discrepancy,
        # but a log line is what an operator watching the scheduler actually
        # sees — and the whole point of this job is that someone notices.
        for f in result.findings:
            logger.warning(
                "lifecycle-sweep DRIFT %s: cfg station_status=%r but TOS says %r "
                "(verdict=%s, would propose %s) — review with "
                "`receivers cfg sync-from-tos %s --only station_status`",
                f["station_id"],
                f["cfg_value"],
                f["tos_value"],
                f["verdict"],
                f["proposed"],
                f["station_id"],
            )
