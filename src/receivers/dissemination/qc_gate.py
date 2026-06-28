"""Header-QC gate — verify a converted RINEX header against TOS before push.

Never push a file whose header disagrees with TOS on a *semantic* field. Reuses
:func:`tostools.rinex.reader.read_rinex_header` / ``extract_header_info`` to read
the header and :func:`tostools.rinex.validator.compare_rinex_to_tos` to diff it
against the station's TOS session (one entry of ``device_history``).

Blocking vs non-blocking — ``compare_rinex_to_tos`` records *every* receiver and
antenna field as a "discrepancy" whether or not it actually differs (RINEX serial/
type strings almost never match the TOS formatting verbatim). Those are noise that
the header-setter (``correct_rinex_from_tos``, wired in T3) normalises, so they are
NOT blocking. The gate blocks only on fields the comparator reports *solely on a
real mismatch*: ``marker``, ``antenna_height`` (>1 mm), ``coordinates`` (beyond the
position tolerance), and an outright missing TOS session.

T2 builds the gate as a pure verdict over an injected ``tos_session`` (fully
testable offline). T3 supplies the live session via the TOS access layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.dissemination.qc")

# Discrepancy keys that compare_rinex_to_tos emits ONLY on a genuine mismatch.
# (receiver/antenna are emitted unconditionally → excluded; set-header handles them.)
DEFAULT_BLOCKING_FIELDS = frozenset({"marker", "antenna_height", "coordinates"})


@dataclass
class QCVerdict:
    """Outcome of a header-QC check."""

    passed: bool
    blocking: dict[str, Any] = field(default_factory=dict)
    """Discrepancies in blocking fields that caused a fail."""
    discrepancies: dict[str, Any] = field(default_factory=dict)
    """All discrepancies (blocking + non-blocking) for diagnostics."""
    missing_tos: list[str] = field(default_factory=list)
    matches: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def read_header_info(
    rinex_file: Path, loglevel: int = logging.WARNING
) -> dict[str, str]:
    """Read ``rinex_file`` and return the extracted header-label → value dict.

    Returns an empty dict if the header can't be read (no END OF HEADER, etc.).
    """
    from tostools.rinex.reader import extract_header_info, read_rinex_header

    header_data = read_rinex_header(rinex_file, loglevel=loglevel)
    if not header_data:
        return {}
    info: dict[str, str] = extract_header_info(header_data, loglevel=loglevel)
    return info


def select_session(
    device_history: list[dict[str, Any]], observation_dt: datetime
) -> Optional[dict[str, Any]]:
    """Pick the ``device_history`` session covering ``observation_dt``.

    A session matches when ``time_from <= observation_dt < time_to`` (an open
    ``time_to`` of None means "still current"). Returns None if nothing covers
    the date.
    """
    for session in device_history:
        start = session.get("time_from")
        end = session.get("time_to")
        if start is not None and observation_dt < start:
            continue
        if end is not None and observation_dt >= end:
            continue
        return session
    return None


def qc_check(
    rinex_file: Path,
    tos_session: Optional[dict[str, Any]],
    *,
    blocking_fields: frozenset[str] = DEFAULT_BLOCKING_FIELDS,
    coord_tolerance_m: float = 10.0,
    loglevel: int = logging.WARNING,
) -> QCVerdict:
    """Verify ``rinex_file``'s header against ``tos_session``.

    Fails (``passed=False``) when:
      * ``tos_session`` is None (no TOS coverage for the date), or
      * the header can't be read, or
      * any ``blocking_fields`` entry is in the comparator's discrepancies.

    Non-blocking discrepancies (receiver/antenna formatting) are recorded for
    diagnostics but do not fail the gate.
    """
    if tos_session is None:
        return QCVerdict(passed=False, message="no TOS session covers this date")

    rinex_info = read_header_info(rinex_file, loglevel=loglevel)
    if not rinex_info:
        return QCVerdict(
            passed=False, message=f"could not read header of {rinex_file.name}"
        )

    from tostools.rinex.validator import compare_rinex_to_tos

    result = compare_rinex_to_tos(
        rinex_info, tos_session, loglevel=loglevel, coord_tolerance=coord_tolerance_m
    )
    discrepancies = result.get("discrepancies", {})
    blocking = {k: v for k, v in discrepancies.items() if k in blocking_fields}

    if blocking:
        msg = "blocking header mismatch: " + ", ".join(sorted(blocking))
    else:
        msg = "header OK vs TOS"
    return QCVerdict(
        passed=not blocking,
        blocking=blocking,
        discrepancies=discrepancies,
        missing_tos=result.get("missing_tos", []),
        matches=result.get("matches", {}),
        message=msg,
    )
