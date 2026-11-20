"""Use another station's router as the SMS catcher for MSISDN discovery.

A SIM cannot read its own phone number, so ``cfg discover-phone`` texts a
*catcher* and reads the sender off it. Until now the catcher was the
operator's mobile, and a human had to read the sender and type it back. A
router in the fleet works just as well as a catcher — and its inbox can be
read programmatically, so the whole loop closes without a human:

    field router ──SMS(token)──▶ catcher router's SIM
                                  └─ read inbox, match token ─▶ sender = MSISDN

This is how SKOG's number was found on 2026-11-18 (VFLS caught it, read by
hand). The catcher is named by station marker (``--via VFLS``): its router
comes from stations.cfg ``router_ip`` and its own number from the TOS
``phone_number`` of its open ``sim_card``.

Both router families are readable:

* **Milesight** (UR32/UR32L/UR35) — ``/cgi`` core ``yruo_sms`` ``query_inbox``.
* **Teltonika** (RutOS) — ``gsmctl -S -l all`` over SSH.

The family is detected from the DEVICE (Milesight's unauthenticated identity
endpoint), never from stations.cfg ``router_type``, matching
``_is_milesight`` in the CLI.

Matching is by a random **token** in the message body, never by "the newest
message": a catcher inbox holds earlier discoveries (VFLS has SKOG's) and
SMS-command traffic, and a stale row must never be recorded as a new SIM's
number.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"^[A-Za-z0-9]{4}$")
_PHONE_RE = re.compile(r"^\+?\d{6,15}$")


class CatcherError(RuntimeError):
    """The catcher cannot be resolved or its inbox cannot be read."""


@dataclass(frozen=True)
class InboxMessage:
    """One received SMS on a catcher router."""

    sender: str
    content: str
    time: Optional[str] = None


@dataclass(frozen=True)
class Catcher:
    """A router that receives the discovery SMS, and the number it answers on."""

    host: str
    number: str
    label: str


def make_token() -> str:
    """A short random tag that makes one discovery SMS unambiguous."""
    return f"#{secrets.token_hex(3)}"


def parse_gsmctl_inbox(output: str) -> List[InboxMessage]:
    """Parse ``gsmctl -S -l all`` (RutOS) into messages.

    The listing is a run of blocks, each starting ``Index:`` and carrying
    ``Sender:`` and ``Text:`` among other ``Key: value`` lines (``Date``,
    ``Status``, multipart fields …). ``Text`` is the last field and is taken
    to run until the next ``Index:`` line, so a multi-line body survives.
    """
    messages: List[InboxMessage] = []
    sender: Optional[str] = None
    date: Optional[str] = None
    text_lines: Optional[List[str]] = None

    def _flush() -> None:
        if sender and text_lines is not None:
            messages.append(
                InboxMessage(
                    sender=sender, content="\n".join(text_lines).strip(), time=date
                )
            )

    for raw in output.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("Index:"):
            _flush()
            sender, date, text_lines = None, None, None
            continue
        if text_lines is not None:
            if line.strip() and not set(line.strip()) <= {"-"}:
                text_lines.append(line)
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Sender":
            sender = value
        elif key == "Date":
            date = value
        elif key == "Text":
            text_lines = [value] if value else []
    _flush()
    return messages


def _is_milesight_host(host: str) -> bool:
    try:
        from .milesight_api import MilesightClient

        return bool(MilesightClient(host).get_identity().model)
    except Exception:  # noqa: BLE001 — probe failure ⇒ assume Teltonika
        return False


def read_inbox(host: str, *, password: Optional[str] = None) -> List[InboxMessage]:
    """Read the SMS inbox of the router at ``host`` (Milesight or Teltonika).

    A plain read — sends nothing and costs nothing.

    Raises:
        CatcherError: the router cannot be reached or refuses the read.
    """
    if _is_milesight_host(host):
        from .milesight_api import MilesightClient, MilesightError

        try:
            with MilesightClient(host) as c:
                c.connect()
                rows = c.query_sms("inbox", limit=30)
        except MilesightError as exc:
            raise CatcherError(f"{host}: Milesight inbox read failed: {exc}") from exc
        out: List[InboxMessage] = []
        for row in rows:
            value = (row or {}).get("value") or {}
            sender = value.get("from")
            if sender:
                out.append(
                    InboxMessage(
                        sender=str(sender),
                        content=str(value.get("content") or ""),
                        time=value.get("time"),
                    )
                )
        return out

    from .conntrack_helper import _connect
    from .telemetry_probe import _ssh_run_bounded, resolve_credentials

    _user, pw = resolve_credentials(password=password)
    if not pw:
        raise CatcherError(
            f"{host}: no router password — set [teltonika] password / "
            f"password_pass_path in receivers.cfg"
        )
    try:
        client = _connect(host, ssh_user="root", password=pw, port=22, timeout=15)
    except Exception as exc:  # noqa: BLE001 — paramiko raises many types
        raise CatcherError(f"{host}: SSH connect failed: {exc}") from exc
    try:
        rc, out_s, err = _ssh_run_bounded(client, "gsmctl -S -l all", timeout=30)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass
    if rc != 0:
        raise CatcherError(f"{host}: gsmctl inbox read failed rc={rc}: {err.strip()}")
    return parse_gsmctl_inbox(out_s)


def find_sender(messages: List[InboxMessage], token: str) -> Optional[str]:
    """The sender of the message carrying ``token``, or ``None``.

    More than one match with DIFFERENT senders is refused rather than
    guessed: the token is random per run, so that would mean something is
    badly wrong and recording either number would be a guess.
    """
    senders = {m.sender for m in messages if token in m.content}
    if len(senders) > 1:
        raise CatcherError(
            f"token {token} arrived from several senders {sorted(senders)} — "
            f"refusing to pick one"
        )
    return next(iter(senders), None)


def wait_for_sender(
    host: str,
    token: str,
    *,
    timeout: float = 180.0,
    poll: float = 10.0,
    reader: Callable[[str], List[InboxMessage]] = read_inbox,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[str]:
    """Poll the catcher's inbox until the token arrives; return its sender.

    Returns ``None`` on timeout. A transient read failure is logged and
    retried until the deadline — SMS delivery and the catcher's own link are
    both slower than a single poll.
    """
    deadline = clock() + timeout
    while True:
        try:
            sender = find_sender(reader(host), token)
        except CatcherError as exc:
            if "several senders" in str(exc):
                raise
            logger.warning("catcher inbox read failed (will retry): %s", exc)
            sender = None
        if sender:
            return sender
        if clock() >= deadline:
            return None
        sleep(poll)


def resolve_catcher(
    via: str,
    *,
    number: Optional[str] = None,
    lookup_router_ip: Optional[Callable[[str], Optional[str]]] = None,
    lookup_sim_phone: Optional[Callable[[str], Optional[str]]] = None,
) -> Catcher:
    """Resolve ``--via`` (station marker or router host) to a :class:`Catcher`.

    A marker resolves its router from stations.cfg ``router_ip`` and its
    number from TOS (the open ``sim_card``'s ``phone_number``); ``number``
    overrides the TOS value. A bare host needs ``number``, since there is no
    station to look the SIM up on.

    Raises:
        CatcherError: the router or the number cannot be determined.
    """
    via = via.strip()
    if _MARKER_RE.match(via):
        marker = via.upper()
        if lookup_router_ip is None:
            lookup_router_ip = _cfg_router_ip
        host = lookup_router_ip(marker)
        if not host:
            raise CatcherError(
                f"no router_ip for catcher station {marker} in stations.cfg"
            )
        if not number:
            if lookup_sim_phone is None:
                lookup_sim_phone = _tos_sim_phone
            number = lookup_sim_phone(marker)
        if not number:
            raise CatcherError(
                f"catcher {marker}: its SIM has no phone_number in TOS — pass the "
                f"catcher's number explicitly"
            )
        label = marker
    else:
        host = via
        if not number:
            raise CatcherError(
                f"catcher {via} is a host, not a station marker — pass the catcher "
                f"SIM's number explicitly"
            )
        label = via
    if not _PHONE_RE.match(number.replace(" ", "")):
        raise CatcherError(
            f"catcher number {number!r} does not look like a phone number"
        )
    return Catcher(host=host, number=number.replace(" ", ""), label=label)


def _cfg_router_ip(marker: str) -> Optional[str]:
    from ..config_utils import get_station_config

    cfg = get_station_config(marker, silent=True) or {}
    return cfg.get("router_ip") or None


def _tos_sim_phone(marker: str) -> Optional[str]:
    from .operations import CfgOperationError, station_device_attribute

    try:
        return station_device_attribute(marker, subtype="sim_card", code="phone_number")
    except CfgOperationError as exc:
        raise CatcherError(f"catcher {marker}: {exc}") from exc
