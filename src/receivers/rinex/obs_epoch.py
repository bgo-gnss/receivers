"""The file's own first observation epoch — the right key for a TOS lookup.

TOS device sessions are bounded by real timestamps (``2026-09-08 15:00``), but
every caller that asks TOS "what hardware was here?" has, until now, handed it a
date derived from the FILENAME. A RINEX 2 daily name carries no time at all, so
``RFEL2510.26D`` resolved to ``2026-09-08 00:00`` — and a station whose receiver
was swapped at 15:00 that day got the header of the hardware it no longer had.

Measured on RFEL 2026-09-08: data runs 19:26→23:59:45 (PolaRX5, installed 15:00),
but the converted header said ``TRIMBLE NETRS 4921172756``. The neighbouring day
2026-09-10 — same code, same station — came out ``SEPT POLARX5 4103742``,
correctly. The defect only shows on a station's changeover day, which is exactly
the day whose header matters most.

The fix is to ask the file. ``TIME OF FIRST OBS`` carries the full epoch the
receiver actually started writing, so it lands on the correct side of a
mid-day session boundary.

**The refinement never changes the DAY.** ``resolve_tos_lookup_epoch`` returns
the claimed value untouched unless the header's first observation falls on that
same date. A file whose data starts on a different day is misfiled raw — a
condition ``_verify_conversion_identity`` exists to catch and delete — and this
module must not quietly paper over it by following the header to another day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# A RINEX header is short; stop well before reading an observation file's body.
_MAX_HEADER_LINES = 300


def read_obs_header_identity(
    rinex_file: Path,
) -> Tuple[Optional[datetime], Optional[tuple], Optional[str]]:
    """``(first_obs_epoch, approx_xyz, marker_name)`` from a RINEX obs header.

    ``first_obs_epoch`` is a full ``datetime`` — hour, minute and second
    included. Callers that only care about the day take ``.date()``; the time is
    what makes a mid-day TOS session boundary resolvable at all.

    Every field is independently optional: a header missing one still yields the
    others, and an unparseable value yields ``None`` rather than raising. The
    identity gate and the TOS lookup both fail open on ``None``.
    """
    first_obs = xyz = marker = None
    with open(rinex_file, encoding="latin-1", errors="replace") as fh:
        for i, line in enumerate(fh):
            label = line[60:].strip()
            if label == "TIME OF FIRST OBS":
                first_obs = parse_first_obs_value(line[:60])
            elif label == "APPROX POSITION XYZ":
                try:
                    x, y, z = (float(v) for v in line[:60].split()[:3])
                    xyz = (x, y, z)
                except (ValueError, IndexError):
                    pass
            elif label == "MARKER NAME":
                marker = line[:60].strip()
            elif label == "END OF HEADER" or i > _MAX_HEADER_LINES:
                break
    return first_obs, xyz, marker


def parse_first_obs_value(value: Optional[str]) -> Optional[datetime]:
    """``2026 9 8 19 26 45.0000000 GPS`` → ``datetime(2026, 9, 8, 19, 26, 45)``.

    Seconds are FLOORED and carried as a timedelta rather than passed to the
    ``datetime`` constructor: a header reading ``59.9999999`` is legal, and
    rounding it would raise ``second must be in 0..59``. The sub-second part is
    kept because it costs nothing, but nothing downstream depends on it.

    A header carrying only ``y m d`` (no time) parses to midnight — the same
    value the filename would have given, so such a file is simply not refined.
    """
    if value is None:
        return None
    parts = str(value).split()
    try:
        year, month, day = (int(p) for p in parts[:3])
        base = datetime(year, month, day)
    except (ValueError, IndexError):
        return None
    try:
        hour, minute = (int(p) for p in parts[3:5])
        seconds = float(parts[5])
    except (ValueError, IndexError):
        return base
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= seconds < 61):
        return base
    return base + timedelta(hours=hour, minutes=minute, seconds=seconds)


def resolve_tos_lookup_epoch(
    rinex_file: Path,
    claimed: Optional[datetime],
    log: Optional[Any] = None,
) -> Optional[datetime]:
    """The epoch to hand TOS for ``rinex_file``, refined from its own header.

    Returns the file's ``TIME OF FIRST OBS`` when that epoch falls on the same
    DATE as ``claimed``; otherwise returns ``claimed`` unchanged. The three ways
    that happens, all deliberate:

    * **no header / unparseable epoch** — nothing better than the filename;
    * **a different date** — misfiled raw, which belongs to the identity gate,
      not to a metadata lookup quietly relocating the file's era;
    * **midnight, or an hourly file already at its own hour** — the refinement
      is a no-op and the same value comes back.

    Read failures are swallowed (logged at debug): a metadata refinement must
    never be the reason a conversion fails.
    """
    if claimed is None:
        return None
    log = log or logger
    try:
        first_obs, _xyz, _marker = read_obs_header_identity(Path(rinex_file))
    except Exception as exc:  # noqa: BLE001 - refinement is fail-open
        log.debug(f"first-obs read failed for {rinex_file}: {exc}")
        return claimed
    return refine_tos_epoch(claimed, first_obs, log, Path(rinex_file).name)


def refine_tos_epoch(
    claimed: Optional[datetime],
    first_obs: Optional[datetime],
    log: Optional[Any] = None,
    source: str = "the header",
) -> Optional[datetime]:
    """Apply the day-preserving refinement to an ALREADY-PARSED first-obs epoch.

    Split out from :func:`resolve_tos_lookup_epoch` so the rule lives in exactly
    one place. The two callers reach the epoch differently and neither should
    re-implement the rule:

    * the archive converter has a plain file on disk and reads it;
    * ``--fix-headers`` already holds the decompressed header value (its files
      are ``.Z``/``.gz`` in the archive, so re-opening them to re-read one field
      would mean decompressing twice).
    """
    if claimed is None:
        return None
    log = log or logger
    if first_obs is None or first_obs.date() != claimed.date():
        return claimed
    if first_obs != claimed:
        log.debug(
            f"TOS lookup epoch refined {claimed:%H:%M:%S} → {first_obs:%H:%M:%S} "
            f"from {source}'s TIME OF FIRST OBS"
        )
    return first_obs


__all__ = [
    "read_obs_header_identity",
    "parse_first_obs_value",
    "refine_tos_epoch",
    "resolve_tos_lookup_epoch",
]
