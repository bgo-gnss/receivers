"""Enable the RutOS conntrack FTP helper over SSH so passive-mode FTP works through NAT.

RutOS 7+ (OpenWrt) ships with ``net.netfilter.nf_conntrack_helper=0``, which
disables *automatic* conntrack-helper assignment. With it off, the
``nf_conntrack_ftp`` helper never attaches to FTP control connections, so
passive-mode data ports are unreachable through the router's NAT and receiver
downloads stall on the data channel (see the ``ftp-pasv-villa-a-teltonika``
vault note and the FTP-helper memory).

Unlike :mod:`receivers.cfg.telemetry_probe` (REST) and ``cfg
ensure-port-forwards``, this is **not** exposed by the RutOS REST API (403 on
this firmware), so it must be applied over SSH (dropbear, ``root`` login —
the REST ``admin`` user shares the password).

The fix, verified live on a RUT241 (fw ``RUT2M_R_00.07.22.3`` / OpenWrt 21.02):

  - ``net.netfilter.nf_conntrack_helper=1`` — live via ``sysctl -w`` and
    persisted in ``/etc/sysctl.conf`` (the ``/etc/sysctl.d/*.conf`` files are
    upgrade-managed and explicitly say "Do not edit").
  - *optionally* extend ``nf_conntrack_ftp`` to also track the WAN-exposed port:
    the helper defaults to port 21, and the receiver's DNAT maps WAN 2160 → LAN
    21, so port-21 tracking already covers the receiver — ``ports=21,2160`` is
    only belt-and-suspenders for a direct-2160 reach. Off by default because it
    requires a module reload.

**Touches only connection tracking** — never firewall rules or routing — so it
cannot sever the SSH/management path. Idempotent, dry-run by default, with a
read-back verify after a live apply.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Reuse the telemetry-probe credential resolver + exception hierarchy so the
# operator gets the identical "[teltonika] cleartext or pass-path" convention.
from .telemetry_probe import (
    ProbeAuthError,
    ProbeError,
    ProbeUnreachableError,
    resolve_credentials,
)

logger = logging.getLogger(__name__)

DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "root"  # RutOS dropbear logs in as root, not the REST 'admin'
DEFAULT_TIMEOUT = 15
_HELPER_SYSCTL = "net.netfilter.nf_conntrack_helper"
_SYSCTL_PERSIST_FILE = "/etc/sysctl.conf"
_FTP_PORTS_PARAM = "/sys/module/nf_conntrack_ftp/parameters/ports"
_MODPROBE_FILE = "/etc/modprobe.d/nf_conntrack_ftp.conf"
_FTP_PORTS_RE = re.compile(r"^\d{1,5}(,\d{1,5})*$")


def _validate_ftp_ports(ftp_ports: str) -> str:
    """Return a normalised ``ports`` string (digits/commas), or raise.

    Guards the value before it is interpolated into the SSH ``modprobe`` command
    so there is no shell-injection surface from the ``--ftp-ports`` operator flag.
    """
    norm = ftp_ports.replace(" ", "")
    if not _FTP_PORTS_RE.match(norm):
        raise ProbeError(
            f"invalid --ftp-ports {ftp_ports!r}: expected comma-separated port "
            f"numbers (e.g. 21,2160)"
        )
    return norm


@dataclass
class ConntrackState:
    """Current router conntrack-helper state, as read over SSH."""

    helper_value: Optional[str] = None  # "0" / "1" / None if unreadable
    helper_persisted: bool = False  # is it set in /etc/sysctl.conf?
    ftp_ports: Optional[str] = None  # e.g. "21" or "21,2160"
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSH plumbing
# ---------------------------------------------------------------------------


def _connect(
    host: str,
    *,
    ssh_user: str,
    password: str,
    port: int,
    timeout: int,
) -> Any:
    """Open a paramiko SSH session (password auth, auto-add host key).

    paramiko is imported lazily so importing this module / the CLI doesn't hard
    require it until an SSH op actually runs.
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - dep declared in pyproject
        raise ProbeError(
            "paramiko is required for conntrack-helper SSH ops (pip install paramiko)"
        ) from exc

    client = paramiko.SSHClient()
    # Routers are reached by (often dynamic, cellular) IP with no pre-seeded
    # known_hosts entry — host-key pinning across the fleet isn't practical, and
    # the management path is already an internal/trusted network. Same rationale
    # as the [tool.bandit] "internal IPs we control" skips.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
    try:
        client.connect(
            host,
            port=port,
            username=ssh_user,
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException as exc:
        raise ProbeAuthError(
            f"{host}: SSH auth failed for {ssh_user!r} — RutOS SSH uses the "
            f"root password (same as the web/admin password)"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — socket/TLS/timeout → unreachable
        raise ProbeUnreachableError(f"{host}: SSH connect failed: {exc}") from exc
    return client


def _run(client: Any, cmd: str, *, timeout: int) -> Tuple[int, str, str]:
    """Run a command, return ``(exit_status, stdout, stderr)`` (stripped)."""
    try:
        # cmd is built from module constants + the validated ftp_ports string
        # (digits/commas only — see _validate_ftp_ports), never raw operator
        # input, so there is no shell-injection vector here.
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)  # nosec B601
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        raise ProbeUnreachableError(f"SSH command failed ({cmd!r}): {exc}") from exc
    return rc, out, err


# ---------------------------------------------------------------------------
# State read
# ---------------------------------------------------------------------------


def _read_state(client: Any, *, timeout: int) -> ConntrackState:
    """Read the live conntrack-helper state over an open SSH session."""
    st = ConntrackState()

    rc, out, _ = _run(client, f"sysctl -n {_HELPER_SYSCTL}", timeout=timeout)
    st.helper_value = out if rc == 0 and out in ("0", "1") else (out or None)

    rc, out, _ = _run(
        client,
        f"grep -E '^{_HELPER_SYSCTL}\\s*=\\s*1' {_SYSCTL_PERSIST_FILE}",
        timeout=timeout,
    )
    st.helper_persisted = rc == 0 and bool(out)

    rc, out, _ = _run(client, f"cat {_FTP_PORTS_PARAM} 2>/dev/null", timeout=timeout)
    st.ftp_ports = out or None

    return st


def check_conntrack_helper(
    host: str,
    *,
    ssh_user: str = DEFAULT_SSH_USER,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_port: int = DEFAULT_SSH_PORT,
    timeout: int = DEFAULT_TIMEOUT,
) -> ConntrackState:
    """Read-only: return the router's current conntrack-helper state."""
    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no [teltonika] password resolved for SSH (set "
            f"receivers.cfg [teltonika] password / password_pass_path, or pass "
            f"--password)"
        )
    client = _connect(
        host, ssh_user=ssh_user, password=pw, port=ssh_port, timeout=timeout
    )
    try:
        return _read_state(client, timeout=timeout)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def ensure_conntrack_helper(
    host: str,
    *,
    ssh_user: str = DEFAULT_SSH_USER,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_port: int = DEFAULT_SSH_PORT,
    ftp_ports: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Idempotently enable ``nf_conntrack_helper`` on a Teltonika router via SSH.

    Always (when needed): set the helper live (``sysctl -w``) and persist it in
    ``/etc/sysctl.conf``. When ``ftp_ports`` is given (e.g. ``"21,2160"``) and
    differs from the live value, also write ``/etc/modprobe.d`` and reload
    ``nf_conntrack_ftp`` (best-effort — a busy module reload is reported, not
    fatal; the persisted file applies on next boot).

    **Mutates the router (connection tracking only — no firewall/routing).**
    Dry-run by default: returns the planned shell ops without sending. Returns a
    dict with ``before`` / ``after`` :class:`ConntrackState`-derived values, the
    list of ``planned`` ops, ``changed`` (bool), and ``applied`` (bool).
    """
    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no [teltonika] password resolved for SSH (set "
            f"receivers.cfg [teltonika] password / password_pass_path, or pass "
            f"--password)"
        )

    client = _connect(
        host, ssh_user=ssh_user, password=pw, port=ssh_port, timeout=timeout
    )
    try:
        before = _read_state(client, timeout=timeout)

        planned: List[str] = []
        # 1. Helper toggle — the actual fix.
        need_live = before.helper_value != "1"
        need_persist = not before.helper_persisted
        if need_live:
            planned.append(f"sysctl -w {_HELPER_SYSCTL}=1")
        if need_persist:
            # Replace an existing line, else append — avoids duplicate keys.
            planned.append(
                f"if grep -qE '^{_HELPER_SYSCTL}' {_SYSCTL_PERSIST_FILE}; then "
                f"sed -i 's/^{_HELPER_SYSCTL}.*/{_HELPER_SYSCTL}=1/' "
                f"{_SYSCTL_PERSIST_FILE}; else echo '{_HELPER_SYSCTL}=1' >> "
                f"{_SYSCTL_PERSIST_FILE}; fi"
            )
        # 2. Optional FTP-ports extension (needs a module reload).
        want_ports = _validate_ftp_ports(ftp_ports) if ftp_ports else ""
        do_ports = bool(want_ports) and want_ports != (before.ftp_ports or "")
        if do_ports:
            planned.append(
                f"mkdir -p /etc/modprobe.d && printf 'options nf_conntrack_ftp "
                f"ports=%s\\n' '{want_ports}' > {_MODPROBE_FILE}"
            )
            planned.append(
                f"rmmod nf_conntrack_ftp 2>/dev/null && modprobe nf_conntrack_ftp "
                f"ports={want_ports}"
            )

        result: Dict[str, Any] = {
            "dry_run": dry_run,
            "host": host,
            "before": {
                "helper": before.helper_value,
                "persisted": before.helper_persisted,
                "ftp_ports": before.ftp_ports,
            },
            "planned": planned,
            "changed": bool(planned),
            "applied": False,
        }

        if dry_run or not planned:
            return result

        # Live apply.
        notes: List[str] = []
        for cmd in planned:
            rc, _out, err = _run(client, cmd, timeout=timeout)
            is_reload = cmd.startswith("rmmod ")
            if rc != 0 and not is_reload:
                raise ProbeError(f"{host}: command failed (rc={rc}): {cmd}\n{err}")
            if rc != 0 and is_reload:
                notes.append(
                    "nf_conntrack_ftp live reload skipped (module busy); the "
                    "persisted ports apply on next boot"
                )

        after = _read_state(client, timeout=timeout)
        result["after"] = {
            "helper": after.helper_value,
            "persisted": after.helper_persisted,
            "ftp_ports": after.ftp_ports,
        }
        result["applied"] = True
        result["notes"] = notes
        if after.helper_value != "1":
            raise ProbeError(
                f"{host}: helper still {after.helper_value!r} after apply — "
                f"expected '1'"
            )
        return result
    finally:
        client.close()
