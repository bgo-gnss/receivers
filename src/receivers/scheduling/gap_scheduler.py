"""Periodic gap detection for scheduled downloads.

Scans archive directories for missing files across all configured session
types.  Runs on the 'backfill' executor at a configurable interval (default
every 2 hours).

Reuses the GapDetector from health.file_tracker — this module is just the
APScheduler-compatible entry point.
"""

import logging
from typing import List

logger = logging.getLogger("gps_scheduler.gap_detection")


def _run_gap_detection_job(
    session_types: List[str],
    days_back: int = 7,
) -> None:
    """APScheduler job: scan for gaps in archived files.

    Iterates active stations, calls GapDetector.get_gap_summary() for each
    session type, and logs the results.  max_instances=1 prevents overlap.

    Args:
        session_types: List of session types to scan (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
    """
    try:
        from ..health.file_tracker import GapDetector
        from ..cli.main import get_all_station_configs
    except ImportError as e:
        logger.debug(f"Gap detection dependencies not available: {e}")
        return

    try:
        # Get active station IDs
        all_stations = get_all_station_configs()
        station_ids = [
            sid for sid, cfg in all_stations.items()
            if cfg.get('enabled', True)
            and cfg.get('station_status') not in ('discontinued', 'inactive')
            and cfg.get('health_check') != 'passive'
        ]

        if not station_ids:
            logger.info("Gap detection: no active stations")
            return

        logger.info(
            f"Gap detection: scanning {len(station_ids)} stations, "
            f"{len(session_types)} sessions, {days_back} days back"
        )

        with GapDetector() as detector:
            for session_type in session_types:
                summary = detector.get_gap_summary(
                    station_ids,
                    session_type,
                    days_back=days_back,
                )

                total_gaps = summary.get('total_gaps', 0)
                total_expected = summary.get('total_expected', 0)
                total_archived = summary.get('total_archived', 0)

                if total_gaps > 0:
                    # Find top stations with gaps
                    stations_with_gaps = [
                        (sid, info) for sid, info in summary.get('stations', {}).items()
                        if info.get('gaps', 0) > 0
                    ]
                    top_stations = sorted(
                        stations_with_gaps, key=lambda x: -x[1]['gaps']
                    )[:5]
                    top_str = ", ".join(
                        f"{sid}({info['gaps']})" for sid, info in top_stations
                    )
                    logger.info(
                        f"Gap detection {session_type}: "
                        f"{total_gaps} gaps / {total_expected} expected "
                        f"({total_archived} archived). "
                        f"Top: {top_str}"
                    )
                else:
                    logger.info(
                        f"Gap detection {session_type}: "
                        f"no gaps ({total_archived}/{total_expected} archived)"
                    )

    except Exception as e:
        logger.error(f"Gap detection failed: {type(e).__name__}: {e}")
