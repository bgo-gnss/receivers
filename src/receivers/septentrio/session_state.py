"""Parse Septentrio logging-session config into normalized state.

A PolaRx5 session is fully defined by a small set of incremental `setX`
commands across three command families:

* `setSBFOutput, Stream<n>, LOG<m>`            — stream → log binding
* `setSBFOutput, Stream<n>, , <blocks>`        — SBF block list for stream
* `setSBFOutput, Stream<n>, , , <interval>`    — stream interval
* `setLogSession, LOG<n>, <state>`             — log slot state
* `setLogSession, LOG<n>, , , <name>`          — log session name
* `setLogSession, LOG<n>, , , , <retention>`   — log retention
* `setLogSession, LOG<n>, , , , , <priority>`  — log priority
* `setFileNaming, LOG<n>, <format>`            — filename format
* `setFileNaming, LOG<n>, , , <enable>`        — filename enable on/off

`parse_session_state(commands, session_name)` walks a list of such commands
(either a canonical template or the output of `lstConfigFile, Current`) and
returns the assembled `SessionState`, or None if no LOG slot has the named
session.

`diff_session_state(receiver, template)` compares two states and returns
a list of human-readable drift strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional


@dataclass
class SessionState:
    """Normalized state of a single PolaRX5 logging session."""

    name: str
    log_slot: str  # e.g. "LOG5"
    state: str  # Enabled / Disabled / Unused
    stream_slot: Optional[str] = None  # primary stream feeding the log
    sbf_blocks: frozenset = field(default_factory=frozenset)
    interval: Optional[str] = None  # e.g. "sec60"
    extra_stream_slots: tuple = ()  # legacy/alternate bindings to the same log
    retention: Optional[str] = None  # e.g. "After1Year"
    priority: Optional[str] = None  # e.g. "High"
    file_naming_format: Optional[str] = None  # e.g. "IGS1H"
    file_naming_enabled: Optional[bool] = None


def _split_fields(line: str) -> List[str]:
    """Comma-split a setX line and strip whitespace + quotes."""
    parts = [p.strip() for p in line.split(",")]
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
            out.append(p[1:-1])
        else:
            out.append(p)
    return out


def parse_session_state(
    commands: Iterable[str], session_name: str
) -> Optional[SessionState]:
    """Parse setX commands and assemble state for the named session.

    Args:
        commands: iterable of setX command strings (one per line).
            Comments and blank lines are skipped.
        session_name: target session name (case-insensitive match).

    Returns:
        Assembled SessionState, or None if no LOG slot carries that name.
    """
    target = session_name.strip().lower()

    # Per-slot accumulators
    log_state: dict = {}  # LOGn -> state
    log_name: dict = {}  # LOGn -> name (lowercased for match, original kept for echo)
    log_retention: dict = {}
    log_priority: dict = {}
    log_filename_format: dict = {}
    log_filename_enabled: dict = {}
    stream_target: dict = {}  # Stream<n> -> LOG<m>
    stream_blocks: dict = {}  # Stream<n> -> frozenset
    stream_interval: dict = {}  # Stream<n> -> interval

    for raw in commands:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = _split_fields(line)
        if not fields:
            continue
        head = fields[0]

        # setLogSession, LOG<n>, <state>[, <disk>, <name>, <retention>, <priority>, ...]
        if head == "setLogSession" and len(fields) >= 3:
            slot = fields[1]
            if len(fields) > 2 and fields[2]:
                log_state[slot] = fields[2]
            if len(fields) > 4 and fields[4]:
                log_name[slot] = fields[4]
            if len(fields) > 5 and fields[5]:
                log_retention[slot] = fields[5]
            if len(fields) > 6 and fields[6]:
                log_priority[slot] = fields[6]
            continue

        # setSBFOutput, Stream<n>, [<target>][, <blocks>][, <interval>]
        if head == "setSBFOutput" and len(fields) >= 3:
            stream = fields[1]
            if len(fields) > 2 and fields[2]:
                stream_target[stream] = fields[2]
            if len(fields) > 3 and fields[3]:
                blocks = frozenset(
                    b.strip() for b in fields[3].split("+") if b.strip()
                )
                stream_blocks[stream] = blocks
            if len(fields) > 4 and fields[4]:
                stream_interval[stream] = fields[4]
            continue

        # setFileNaming, LOG<n>, <format>[, <gnss>][, <enable>]
        if head == "setFileNaming" and len(fields) >= 3:
            slot = fields[1]
            if len(fields) > 2 and fields[2]:
                log_filename_format[slot] = fields[2]
            if len(fields) > 4 and fields[4]:
                log_filename_enabled[slot] = fields[4].lower() == "on"
            continue

    # Find the LOG slot carrying the target name.
    matched_slot: Optional[str] = None
    for slot, name in log_name.items():
        if name.strip().lower() == target:
            # Prefer Enabled if multiple slots claim the same name (defensive).
            if log_state.get(slot, "").lower() == "enabled":
                matched_slot = slot
                break
            if matched_slot is None:
                matched_slot = slot
    if matched_slot is None:
        return None

    # Find ALL streams feeding the matched LOG slot. Some stations carry a
    # legacy binding (e.g. Stream2 → LOG5) alongside our canonical push
    # (Stream7 → LOG5). Prefer the stream with the most non-empty SBF
    # blocks — that's the one actually producing data into the log.
    candidate_streams = [
        s for s, tgt in stream_target.items() if tgt == matched_slot
    ]
    extra_streams: List[str] = []
    matched_stream: Optional[str] = None
    if candidate_streams:
        # Sort: most blocks first, then prefer canonical Stream7, then alpha.
        candidate_streams.sort(
            key=lambda s: (
                -len(stream_blocks.get(s, frozenset())),
                0 if s == "Stream7" else 1,
                s,
            )
        )
        matched_stream = candidate_streams[0]
        extra_streams = candidate_streams[1:]

    return SessionState(
        name=log_name[matched_slot],
        log_slot=matched_slot,
        state=log_state.get(matched_slot, ""),
        stream_slot=matched_stream,
        sbf_blocks=stream_blocks.get(matched_stream, frozenset())
        if matched_stream
        else frozenset(),
        interval=stream_interval.get(matched_stream) if matched_stream else None,
        extra_stream_slots=tuple(extra_streams),
        retention=log_retention.get(matched_slot),
        priority=log_priority.get(matched_slot),
        file_naming_format=log_filename_format.get(matched_slot),
        file_naming_enabled=log_filename_enabled.get(matched_slot),
    )


def diff_session_state(
    receiver: SessionState, template: SessionState
) -> List[str]:
    """Compare receiver state to template; return list of drift descriptions.

    Slot identifiers (which LOG / which Stream) are NOT compared — a station
    can carry the canonical session in LOG3+Stream4 and still be in spec as
    long as block list, interval, state, retention, priority, and file
    naming all match. Returns [] when the receiver matches the template.
    """
    diffs: List[str] = []

    if receiver.state.lower() != template.state.lower():
        diffs.append(
            f"state: receiver={receiver.state!r} template={template.state!r}"
        )

    if receiver.sbf_blocks != template.sbf_blocks:
        missing = template.sbf_blocks - receiver.sbf_blocks
        extra = receiver.sbf_blocks - template.sbf_blocks
        parts = []
        if missing:
            parts.append(f"missing={sorted(missing)}")
        if extra:
            parts.append(f"extra={sorted(extra)}")
        diffs.append(
            f"sbf_blocks: receiver has {len(receiver.sbf_blocks)}, "
            f"template has {len(template.sbf_blocks)} ({'; '.join(parts)})"
        )

    if (receiver.interval or "") != (template.interval or ""):
        diffs.append(
            f"interval: receiver={receiver.interval!r} template={template.interval!r}"
        )

    if (receiver.retention or "") != (template.retention or ""):
        diffs.append(
            f"retention: receiver={receiver.retention!r} "
            f"template={template.retention!r}"
        )

    if (receiver.priority or "") != (template.priority or ""):
        diffs.append(
            f"priority: receiver={receiver.priority!r} "
            f"template={template.priority!r}"
        )

    if (receiver.file_naming_format or "") != (template.file_naming_format or ""):
        diffs.append(
            f"file_naming_format: receiver={receiver.file_naming_format!r} "
            f"template={template.file_naming_format!r}"
        )

    if bool(receiver.file_naming_enabled) != bool(template.file_naming_enabled):
        diffs.append(
            f"file_naming_enabled: receiver={receiver.file_naming_enabled} "
            f"template={template.file_naming_enabled}"
        )

    # Surface leftover bindings — separate Streams pointing at the same LOG
    # are usually legacy cruft, not active data flows, but worth flagging.
    if receiver.extra_stream_slots:
        diffs.append(
            f"extra_stream_bindings: {list(receiver.extra_stream_slots)} also "
            f"target {receiver.log_slot} (only {receiver.stream_slot} is in use)"
        )

    return diffs
