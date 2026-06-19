"""Archive-sync freshness — alert when a target's feed stalls.

The archive is fed by a single writer (rawdata), so a silently-stalled sync is
a single point of failure: data keeps being collected locally but stops reaching
the long-term archive, and nothing notices until someone looks. This evaluates
each target's ``sync_state.last_success_ts`` against a max-age threshold so a
stall surfaces as a WARNING / non-zero status instead of a silent gap.

Compares in the naive-local time domain (``sync_state`` stores TIMESTAMP without
tz; see migration 051). See design 1781867391 (decision 2, freshness monitor).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import SyncTarget
from .state import get_last_success

logger = logging.getLogger("receivers.archive.freshness")

DEFAULT_MAX_AGE_MINUTES = 120  # an hourly :45 feed: stale after a missed run + margin

# Freshness states.
OK = "ok"
STALE = "stale"
NEVER = "never"
INACTIVE = "inactive"


@dataclass(frozen=True)
class FreshnessStatus:
    """One target's freshness verdict."""

    target: str
    state: str
    last_success: Optional[datetime]
    age_seconds: Optional[float]
    threshold_seconds: int

    @property
    def is_alerting(self) -> bool:
        """True for states a monitor should treat as a problem."""
        return self.state in (STALE, NEVER)


def evaluate_freshness(
    conn,
    target: SyncTarget,
    *,
    now: datetime,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
) -> FreshnessStatus:
    """Verdict for one target. ``now`` must be naive-local (file-mtime domain)."""
    threshold = max_age_minutes * 60
    if not target.active:
        return FreshnessStatus(target.name, INACTIVE, None, None, threshold)

    last = get_last_success(conn, target.name)
    if last is None:
        return FreshnessStatus(target.name, NEVER, None, None, threshold)

    age = (now - last).total_seconds()
    state = STALE if age > threshold else OK
    return FreshnessStatus(target.name, state, last, age, threshold)


def evaluate_all(
    conn,
    targets: list[SyncTarget],
    *,
    now: datetime,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
) -> list[FreshnessStatus]:
    """Evaluate every target and log a WARNING for each alerting one."""
    results = []
    for target in targets:
        status = evaluate_freshness(
            conn, target, now=now, max_age_minutes=max_age_minutes
        )
        if status.state == STALE:
            logger.warning(
                "archive sync STALE: %s last succeeded %.0f min ago (threshold %d min)",
                status.target,
                (status.age_seconds or 0) / 60,
                max_age_minutes,
            )
        elif status.state == NEVER:
            logger.warning("archive sync NEVER succeeded: %s", status.target)
        results.append(status)
    return results
