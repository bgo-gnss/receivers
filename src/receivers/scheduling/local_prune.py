"""Scheduled local ring-buffer prune job (see receivers.archive.prune).

Standalone job function (APScheduler-friendly): assembles the data root from
receivers.cfg, the archive storage-location name from the sync target, and
the gps_health connection for the catalog gate — then runs one prune pass.
Never raises (a prune hiccup must not disturb the scheduler).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("receivers.scheduler.prune")


def _run_local_prune_job(prune_cfg: dict, dry_run: bool = False) -> None:
    """One ring-buffer pass; config dict = scheduler.yaml [local_prune]."""
    try:
        from ..archive import load_sync_config
        from ..archive.prune import PruneConfig, run_prune, scratch_report
        from ..config.receivers_config import ReceiversConfig

        rcfg = ReceiversConfig()
        root = Path(rcfg.get_data_prepath())
        if not root.is_dir():
            logger.error("local-prune: data root %s not found — skipped", root)
            return

        # Storage-location name of the long-term archive (catalog rows key).
        target = next(
            (
                t
                for t in load_sync_config(None)
                if getattr(t, "tier", None) == "archive"
            ),
            None,
        )
        archive_location = prune_cfg.get(
            "archive_location", getattr(target, "name", None)
        )
        cfg = PruneConfig.from_dict(prune_cfg)
        if cfg.require_catalog and not archive_location:
            logger.error(
                "local-prune: no archive tier target / archive_location — "
                "catalog gate impossible, nothing deleted"
            )
            return

        conn = None
        try:
            if cfg.require_catalog:
                from ..db.connection import get_connection

                conn = get_connection()
            run_prune(
                root,
                cfg,
                archive_location=archive_location or "",
                conn=conn,
                dry_run=dry_run,
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        # Scratch-volume guardrail (download staging) — log-only.
        try:
            scratch_report(Path(rcfg.get_tmp_dir()))
        except Exception:  # noqa: BLE001 - best-effort
            pass

        # Days-to-full forecast: the data volume + any extra volumes listed
        # in config (e.g. the long-term archive mount /mnt/rawgpsdata, where
        # IT needs ~3 weeks lead time to add space incrementally).
        try:
            from ..archive.prune import record_and_forecast

            state = Path.home() / ".cache" / "gps_receivers" / "disk_history.json"
            warn_days = int(prune_cfg.get("warn_days_to_full", 21))
            for vol in [root, *prune_cfg.get("forecast_volumes", [])]:
                record_and_forecast(Path(vol), state, warn_days_to_full=warn_days)
        except Exception:  # noqa: BLE001 - forecast is best-effort
            logger.exception("disk forecast failed")
    except Exception:  # noqa: BLE001 - job must never kill the scheduler
        logger.exception("local-prune job failed")
