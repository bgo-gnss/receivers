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


def _resolve_coord_tolerance(override=None) -> float:
    """One global coordinate-identity gate — receivers.cfg [rinex]
    position_gate_m (default 10 m) unless the caller overrides."""
    if override is not None:
        return float(override)
    try:
        from ..config.receivers_config import get_receivers_config

        return get_receivers_config().get_position_gate_m()
    except Exception:  # noqa: BLE001 - config optional
        return 10.0


logger = logging.getLogger("receivers.dissemination.qc")

# Discrepancy keys that compare_rinex_to_tos emits ONLY on a genuine mismatch.
# (receiver/antenna are emitted unconditionally → excluded; set-header handles them.)
# ``domes`` is added by this gate (EPOS 4.1.7), not by compare_rinex_to_tos.
DEFAULT_BLOCKING_FIELDS = frozenset(
    {"marker", "domes", "antenna_height", "coordinates"}
)


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
    """Read ``rinex_file``'s header and return the label → value dict.

    Streams only the header (up to ``END OF HEADER``) rather than slurping the
    whole file: at QC time ``rinex_file`` is the freshly-written plain RINEX obs,
    which for a daily 15s file is 20 MB+, while we need only the ~few-KB header.
    The old path (``read_rinex_header`` → ``read_text_file``) read the entire file
    with a strict UTF-8 decode and a broad ``except → None``, so the multi-MB slurp
    was both wasteful and a needless transient-failure point — a momentary glitch
    on one of 60+ stations in a sweep would silently fail QC for a valid file.
    Decode is tolerant (``errors='ignore'``), matching the downstream parser.

    Returns an empty dict if the header can't be read (no ``END OF HEADER`` / I/O
    error) — the caller treats that as a QC failure.
    """
    from tostools.rinex.reader import extract_header_info

    path = Path(rinex_file)
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                lines.append(line)
                if "END OF HEADER" in line:
                    break
            else:
                logger.warning("no END OF HEADER in %s", path.name)
                return {}
    except OSError as exc:
        logger.warning("could not read header of %s: %s", path.name, exc)
        return {}

    header_data = {
        "header": "".join(lines),
        "rinex file": [str(path.parent), path.name],
    }
    info: dict[str, str] = extract_header_info(header_data, loglevel=loglevel)
    return info


# Device groups TOS keeps in separate, overlapping device-history sessions.
_DEVICE_KEYS = ("gnss_receiver", "antenna", "radome", "monument")


def select_session(
    device_history: list[dict[str, Any]], observation_dt: datetime
) -> Optional[dict[str, Any]]:
    """Merge the device complement covering ``observation_dt`` into one session.

    A session matches when ``time_from <= observation_dt < time_to`` (an open
    ``time_to`` of None means "still current"). Returns None if nothing covers
    the date.

    TOS splits the receiver / antenna / radome / monument into **separate,
    overlapping** device-history sessions, so on any given date each device type
    lives in a different session. Returning the first match alone would yield an
    incomplete picture (e.g. monument-only for RHOF) — the header-QC gate and the
    reactive/cache fingerprint (:func:`tos_access.session_fingerprint`) would then
    miss receiver/antenna/radome changes. So every covering session is merged,
    taking each device key from its covering session (first match wins on the rare
    same-type overlap).
    """
    covering = []
    for session in device_history:
        start = session.get("time_from")
        end = session.get("time_to")
        if start is not None and observation_dt < start:
            continue
        if end is not None and observation_dt >= end:
            continue
        covering.append(session)
    if not covering:
        return None
    merged = dict(covering[0])
    for session in covering[1:]:
        for key in _DEVICE_KEYS:
            value = session.get(key)
            if value is not None and not merged.get(key):
                merged[key] = value
    return merged


def qc_check(
    rinex_file: Path,
    tos_session: Optional[dict[str, Any]],
    *,
    blocking_fields: frozenset[str] = DEFAULT_BLOCKING_FIELDS,
    coord_tolerance_m: Optional[float] = None,
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
        rinex_info,
        tos_session,
        loglevel=loglevel,
        coord_tolerance=_resolve_coord_tolerance(coord_tolerance_m),
    )
    discrepancies = dict(result.get("discrepancies", {}))
    matches = dict(result.get("matches", {}))

    # EPOS 4.1.7: the RINEX 3 MARKER NAME is the 9-char station ID whose 4-char
    # prefix is the TOS marker. compare_rinex_to_tos only knows the 4-char marker,
    # so it flags the 9-char form as a mismatch — it isn't one.
    marker_disc = discrepancies.get("marker")
    if isinstance(marker_disc, dict):
        rnx = str(marker_disc.get("rinex", "")).strip().upper()
        tos = str(marker_disc.get("tos", "")).strip().upper()
        if len(rnx) == 9 and rnx[:4] == tos:
            discrepancies.pop("marker")
            matches["marker"] = rnx

    # EPOS 4.1.7: the DOMES (when TOS has one) must be in MARKER NUMBER. The header
    # finalizer writes it; the gate verifies it (catches cfg data errors / drops).
    tos_domes = str(tos_session.get("domes") or "").strip().upper()
    if tos_domes:
        rnx_number = str(rinex_info.get("MARKER NUMBER") or "").strip().upper()
        if rnx_number == tos_domes:
            matches["domes"] = tos_domes
            discrepancies.pop("domes", None)
        else:
            discrepancies["domes"] = {"rinex": rnx_number, "tos": tos_domes}
    else:
        # No real DOMES: compare_rinex_to_tos still emits a (domes-or-marker)
        # fallback discrepancy for --fix-headers, but a missing/marker-only
        # MARKER NUMBER is NOT an EPOS DOMES violation and must not BLOCK QC —
        # finalize_epos_header sets it in the pipeline. Drop it here.
        discrepancies.pop("domes", None)
        matches.pop("domes", None)

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
        matches=matches,
        message=msg,
    )
