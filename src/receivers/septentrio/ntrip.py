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
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

_NTR_RE = re.compile(r"^NTR\d+$")

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
