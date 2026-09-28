"""Build identity-safe PolaRX5 NTRIP-server-stream control commands.

`rec-config --ntrip-stream NTR2 off` / `--disable-mount HRIC1` emit ONLY
``setNtripSettings`` (+ optional ``setSBFOutput …, none`` for the SBF stream
feeding that connection, + boot save). They never touch the marker, the OTHER
NTRIP connections, log sessions, or file logging — surgical on/off of a single
NTRIP server stream, the same identity-safe spirit as ``--tracking``.

Turning a stream off (and dropping its SBF generation) reclaims the radio
bandwidth + transmit power it consumed — the win for a wind/solar station that
pushes a redundant feed (e.g. HRIC's SBF mountpoint HRIC1 alongside the RTCM
HRIC0). Disabling sets the connection mode ``Server``→``off`` but leaves its
caster/user/password/mountpoint intact, so it is trivially re-enabled later.

The other direction — :func:`build_ntrip_server_commands` (configure a server
connection) and :func:`build_sbf_bind_commands` (feed it an SBF stream) — is the
same surgery in reverse, and carries the sharper risk: an SBF stream index is
finite and ALREADY IN USE for file logging. ``Stream1 -> LOG1`` is a station's
daily 15 s data. Re-pointing an occupied stream would silently stop that log with
no error anywhere, so both builders REQUIRE the receiver's current config and
REFUSE to overwrite anything already configured. They never free a slot for you.

Grammar (verified against three live PolaRx5 configs, 2026-09-28)::

    setNtripSettings, NTRn, Mode, Caster, Port, User, Password, MountPoint
    setSBFOutput,  StreamN, <destination>          # 3 fields = the wiring
    setSBFOutput,  StreamN, , <blocks>             # message content
    setSBFOutput,  StreamN, , , <interval>         # rate
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_NTR_RE = re.compile(r"^NTR\d+$")
_STREAM_RE = re.compile(r"^Stream\d+$")

#: PolaRx5 SBF output stream slots. Streams 1-7 were occupied on every station
#: inspected; 8-10 were free. Bump only against a receiver that really has more.
MAX_SBF_STREAMS = 10

#: User-facing NTRIP state tokens → PolaRX5 ``setNtripSettings`` mode values.
_STATE_MAP = {
    "off": "off",
    "disable": "off",
    "disabled": "off",
    "on": "Server",
    "server": "Server",
    "enable": "Server",
    "enabled": "Server",
    "client": "Client",
}


def normalize_ntrip_state(state: str) -> str:
    """Map a user state (``off``/``on``/``server``/``client``) to the PolaRX5 mode."""
    mode = _STATE_MAP.get(state.strip().lower())
    if mode is None:
        raise ValueError(
            f"unknown NTRIP state {state!r}; use off / on / server / client"
        )
    return mode


def parse_ntrip_mounts(config_text: str) -> Dict[str, str]:
    """Map mountpoint → NTRIP connection (``NTRx``) from ``setNtripSettings`` lines.

    The PolaRX5 config carries the mountpoint as the 8th positional field, e.g.::

        setNtripSettings, NTR2, , , , , , "HRIC1"   → {"HRIC1": "NTR2"}

    Lines that set other fields (mode, caster, credentials) have fewer fields and
    are skipped, so only the mountpoint assignment contributes.
    """
    mounts: Dict[str, str] = {}
    for line in config_text.splitlines():
        fields = [f.strip().strip('"') for f in line.split(",")]
        if fields[0] != "setNtripSettings" or len(fields) < 8:
            continue
        conn, mount = fields[1], fields[7]
        if _NTR_RE.match(conn) and mount:
            mounts[mount] = conn
    return mounts


def sbf_streams_for_conn(config_text: str, conn: str) -> List[str]:
    """Return the SBF streams whose output destination is ``conn``.

    A destination assignment is exactly ``setSBFOutput, StreamN, <dest>`` (3
    fields); the message-content and interval forms have an empty 3rd field and
    are ignored, so only the stream→connection wiring is matched.
    """
    streams: List[str] = []
    for line in config_text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if (
            fields[0] == "setSBFOutput"
            and len(fields) == 3
            and fields[2] == conn
            and fields[1] not in streams
        ):
            streams.append(fields[1])
    return streams


def build_ntrip_stream_commands(
    conn: str,
    state: str,
    *,
    drop_sbf_streams: Optional[List[str]] = None,
) -> List[str]:
    """``setNtripSettings`` mode change (+ optional SBF-stream drops) + boot save.

    Args:
        conn: NTRIP connection id, e.g. ``NTR2``.
        state: Desired state — ``off`` / ``on`` / ``server`` / ``client``.
        drop_sbf_streams: SBF streams to also disable (``setSBFOutput, S, none``)
            — typically the stream(s) that fed this connection, so the receiver
            stops generating an output that no longer goes anywhere.

    Returns:
        Ordered command list ending with ``eccf, Current, Boot`` so the change
        persists across the station's power cycles.
    """
    if not _NTR_RE.match(conn):
        raise ValueError(f"invalid NTRIP connection {conn!r}; expected NTR1/NTR2/…")
    cmds = [f"setNtripSettings, {conn}, {normalize_ntrip_state(state)}"]
    for stream in drop_sbf_streams or []:
        cmds.append(f"setSBFOutput, {stream}, none")
    cmds.append("eccf, Current, Boot")
    return cmds


def parse_sbf_destinations(config_text: str) -> Dict[str, str]:
    """Map SBF stream → its output destination (``LOG1`` / ``IPS1`` / ``NTR2`` / …).

    The destination assignment is exactly the 3-field form (see the module
    grammar); the block-list and interval forms leave field 3 empty and are
    skipped. A stream absent from this map has no destination and is free.
    """
    dests: Dict[str, str] = {}
    for line in config_text.splitlines():
        fields = [f.strip().strip('"') for f in line.split(",")]
        if (
            fields[0] == "setSBFOutput"
            and len(fields) == 3
            and _STREAM_RE.match(fields[1])
            and fields[2]
        ):
            dests[fields[1]] = fields[2]
    return dests


def free_sbf_streams(
    config_text: str, *, max_streams: int = MAX_SBF_STREAMS
) -> List[str]:
    """Stream slots with NO destination assignment, lowest first.

    "Free" means nothing is wired to it — not that it is unused for anything
    else. Callers should take the first entry rather than guessing an index:
    on the stations inspected, ``Stream6`` was free on SENG but already driving
    ``IP10`` on GEVK and VOGC, so a hard-coded index would have silently
    replaced a live output on two of three receivers.
    """
    used = set(parse_sbf_destinations(config_text))
    return [f"Stream{n}" for n in range(1, max_streams + 1) if f"Stream{n}" not in used]


def build_sbf_bind_commands(
    config_text: str,
    stream: str,
    conn: str,
    blocks: str,
    interval: str,
) -> List[str]:
    """Wire an SBF stream to an NTRIP connection, with content and rate.

    This is the half that makes a configured-but-silent mountpoint actually
    carry data — the GEVK case, where ``NTR2``/``GEVK1`` existed in ``Server``
    mode with nothing feeding it.

    Args:
        config_text: the receiver's CURRENT config. Required, not optional: the
            occupied-stream refusal below cannot be made without it.
        stream: SBF stream slot, e.g. ``Stream8``. Must be free.
        conn: NTRIP connection, e.g. ``NTR2``. Must already exist — this
            function feeds a connection, it does not create one.
        blocks: SBF block list, e.g. ``MeasEpoch+GPSNav+…``.
        interval: output rate token, e.g. ``sec1``.

    Raises:
        ValueError: malformed ids, empty blocks/interval, an occupied stream, or
            a connection with no configured mountpoint. Every one of these is a
            refusal to guess — see the module docstring on why overwriting an
            occupied stream is silent data loss.
    """
    if not _STREAM_RE.match(stream):
        raise ValueError(f"invalid SBF stream {stream!r}; expected Stream1/Stream2/…")
    if not _NTR_RE.match(conn):
        raise ValueError(f"invalid NTRIP connection {conn!r}; expected NTR1/NTR2/…")
    if not blocks.strip():
        raise ValueError(
            "blocks must not be empty — an SBF stream with no blocks "
            "produces nothing and silently occupies a finite slot"
        )
    if not interval.strip():
        raise ValueError("interval must not be empty (e.g. 'sec1')")
    # A comma inside either field shifts every later field along by one, so
    # `setSBFOutput, S, , <blocks>` silently becomes `…, , , <blocks>` — the
    # receiver then reads the block list as the INTERVAL. Block lists are
    # '+'-separated and rates are single tokens, so a comma is always a caller
    # bug (it caught a leading ", " from a config-scraping one-liner of mine).
    for label, value in (("blocks", blocks), ("interval", interval)):
        if "," in value:
            raise ValueError(
                f"{label} must not contain a comma ({value.strip()[:40]!r}): it "
                f"would shift the remaining setSBFOutput fields and be read as "
                f"the wrong parameter. Block lists are '+'-separated."
            )

    occupied = parse_sbf_destinations(config_text)
    if stream in occupied:
        raise ValueError(
            f"{stream} already outputs to {occupied[stream]} — refusing to "
            f"re-point it. Re-pointing an occupied stream stops that output with "
            f"no error (Stream1->LOG1 is the daily 15s log). "
            f"Free slots: {free_sbf_streams(config_text) or 'NONE'}"
        )
    if conn not in set(parse_ntrip_mounts(config_text).values()):
        raise ValueError(
            f"{conn} has no configured mountpoint — create the connection first "
            f"(build_ntrip_server_commands). Known: "
            f"{sorted(parse_ntrip_mounts(config_text)) or 'none'}"
        )

    return [
        f"setSBFOutput, {stream}, {conn}",
        f"setSBFOutput, {stream}, , {blocks}",
        f"setSBFOutput, {stream}, , , {interval}",
        "eccf, Current, Boot",
    ]


def build_ntrip_server_commands(
    config_text: str,
    conn: str,
    *,
    caster: str,
    mountpoint: str,
    user: str,
    password: str,
    port: Optional[int] = None,
) -> List[str]:
    """Configure an NTRIP SERVER connection (caster, credentials, mountpoint).

    The VOGC case: only ``NTR1``/``VOGC0`` existed, so the SBF mount needs its
    connection built before anything can feed it.

    Emits one field per line, matching the form the receiver itself reports —
    so a later ``parse_ntrip_mounts`` reads back exactly what was written, and a
    diff against the config stays line-for-line legible.

    No caster-side provisioning is needed first: the mountpoint is created on the
    caster when the receiver connects and authenticates, so configuring the
    receiver end is sufficient (confirmed by bgo, 2026-09-28 — an earlier note
    here claimed the opposite).

    Raises:
        ValueError: malformed ids, empty required fields, or a connection that
            already carries a mountpoint — overwriting one would silently
            repoint a live feed and discard its caster credentials.
    """
    if not _NTR_RE.match(conn):
        raise ValueError(f"invalid NTRIP connection {conn!r}; expected NTR1/NTR2/…")
    for label, value in (
        ("caster", caster),
        ("mountpoint", mountpoint),
        ("user", user),
        ("password", password),
    ):
        if not str(value).strip():
            raise ValueError(f"{label} must not be empty")
    if port is not None and not (0 < int(port) < 65536):
        raise ValueError(f"port {port!r} out of range")

    existing = {c: m for m, c in parse_ntrip_mounts(config_text).items()}
    if conn in existing:
        raise ValueError(
            f"{conn} already serves mountpoint {existing[conn]!r} — refusing to "
            f"overwrite its caster/credentials. Use a free connection id, or "
            f"--ntrip-stream to toggle the existing one."
        )

    cmds = [
        f"setNtripSettings, {conn}, Server",
        f'setNtripSettings, {conn}, , "{caster}"',
    ]
    if port is not None:
        cmds.append(f"setNtripSettings, {conn}, , , {int(port)}")
    cmds += [
        f'setNtripSettings, {conn}, , , , "{user}"',
        f'setNtripSettings, {conn}, , , , , "{password}"',
        f'setNtripSettings, {conn}, , , , , , "{mountpoint}"',
        "eccf, Current, Boot",
    ]
    return cmds


def redact_secrets(commands: List[str]) -> List[str]:
    """Same commands with the NTRIP password field masked.

    The password is positional field 6 of ``setNtripSettings``. A dry-run prints
    its command list and the push path logs it, so anything user-facing must go
    through this — otherwise enabling a mount writes a caster credential into
    the terminal scrollback and the log file.
    """
    out: List[str] = []
    for cmd in commands:
        fields = [f.strip() for f in cmd.split(",")]
        if fields[0] == "setNtripSettings" and len(fields) == 7 and fields[6]:
            fields[6] = '"********"'
            out.append(", ".join(fields))
        else:
            out.append(cmd)
    return out


def sbf_stream_content(config_text: str, stream: str) -> Tuple[str, str]:
    """``(blocks, interval)`` for ``stream``, read by FIELD POSITION.

    ``setSBFOutput, StreamN, , <blocks>`` is four fields with an empty third;
    ``setSBFOutput, StreamN, , , <interval>`` is five. Splitting on the first
    two commas instead would carry the empty destination field into the value —
    which is exactly how a stray leading comma reached a built command once.

    Returns empty strings for whichever part the config does not set.
    """
    blocks = interval = ""
    for line in config_text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if fields[0] != "setSBFOutput" or len(fields) < 4 or fields[1] != stream:
            continue
        if fields[2]:
            continue  # a destination line, not content
        if len(fields) == 4 and fields[3]:
            blocks = fields[3]
        elif len(fields) == 5 and fields[4]:
            interval = fields[4]
    return blocks, interval


def model_sbf_feed(config_text: str) -> Tuple[Optional[str], str, str]:
    """The SBF feed a station already pushes: ``(conn, blocks, interval)``.

    Finds the NTRIP connection that has an SBF stream wired to it — the thing
    worth copying to another station. Returns ``(None, "", "")`` when the
    station pushes no SBF at all (so the caller can say so rather than emit an
    empty stream).
    """
    for mount, conn in sorted(parse_ntrip_mounts(config_text).items()):
        streams = sbf_streams_for_conn(config_text, conn)
        if streams:
            blocks, interval = sbf_stream_content(config_text, streams[0])
            return conn, blocks, interval
    return None, "", ""
