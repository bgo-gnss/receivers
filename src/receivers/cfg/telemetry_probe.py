"""Teltonika RutOS telemetry-identity probe for ``cfg replace-modem/replace-sim``.

Auto-extracts a station's router (``modem_gsm``) and SIM (``sim_card``) identity
from a live Teltonika router over the RutOS 7.x **REST API**, so an operator
recording a field swap doesn't transcribe serials/MAC/ICCID by hand. Mirrors the
GNSS-receiver :mod:`receivers.cfg.device_probe` surface: one
:func:`probe_teltonika` entry point returning a uniform :class:`TelemetryIdentity`.

Field map verified live 2026-06-06 against a RUT241 (fw RUT2M_R_00.07.22.3); see
the ``reference_teltonika_rutos_rest_api`` memory for the full endpoint dump.

  Router identity   ← GET /api/system/device/status → data
      mnfinfo.serial            router serial_number
      static.device_name        model (e.g. "RUT241")
      ports[] WAN mac           mac_address (colon-formatted)
  SIM / mobile      ← GET /api/modems/status → data[0]
      iccid                     SIM serial_number
      operator                  provider
      conntype "4G (LTE)"       modem subtype "4G"
      imsi / imei               module identity (informational)
  WAN IP            ← GET /api/interfaces/status → default-route iface
      ipv4-address[0].address   SIM ip_address

Transport: REST over HTTPS with a self-signed cert (verify disabled). Auth is a
short-lived bearer token (``POST /api/login``, ~299 s TTL) — fine for a probe
that fires a handful of GETs and exits. Credentials come from ``receivers.cfg``
``[teltonika]`` (cleartext ``username``/``password`` OR ``*_pass_path`` via
pass(1)), with explicit overrides; same convention as the TOS ``[tos]`` section.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# RutOS bearer tokens expire after ~299 s; a probe finishes in well under that,
# so we authenticate once per probe and don't bother refreshing.
DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class TelemetryIdentity:
    """Probed identity for a station's telemetry hardware (router + SIM).

    Split into the two TOS device subtypes the values map onto. Every field is
    optional — a probe that can't read one (older firmware, no SIM inserted)
    leaves it ``None`` and the operator supplies it via an explicit flag.
    """

    host: str
    # modem_gsm (router)
    router_serial: Optional[str] = None
    router_model: Optional[str] = None
    router_manufacturer: Optional[str] = None
    router_mac: Optional[str] = None
    router_firmware: Optional[str] = None
    modem_subtype: Optional[str] = None  # e.g. "4G" — derived from conntype
    # sim_card
    sim_iccid: Optional[str] = None
    sim_ip_address: Optional[str] = None
    provider: Optional[str] = None
    imsi: Optional[str] = None  # informational (module identity)
    imei: Optional[str] = None  # informational (module identity)


class ProbeError(Exception):
    """Base class for telemetry-probe failures."""


class ProbeUnreachableError(ProbeError):
    """Network / socket / TLS failure — router did not respond."""


class ProbeAuthError(ProbeError):
    """Router responded but rejected the credentials (HTTP 401/403 on login)."""


class ProbeCredentialsError(ProbeError):
    """No usable credentials could be resolved (config + overrides both empty)."""


# ---------------------------------------------------------------------------
# Credential resolution — mirrors tostools [tos] (cleartext OR pass-path)
# ---------------------------------------------------------------------------


def _find_receivers_cfg(cfg_path: Optional[str] = None) -> Optional[str]:
    """Locate receivers.cfg, reusing the package's own resolver."""
    if cfg_path:
        return cfg_path
    try:
        from ..config.receivers_config import ReceiversConfig

        return ReceiversConfig().config_path
    except Exception:  # noqa: BLE001 — fall through to None; caller handles
        return None


def resolve_credentials(
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve Teltonika router credentials.

    Precedence (each of username/password independently): explicit override →
    ``[teltonika] *_pass_path`` (via pass(1)) → ``[teltonika]`` cleartext. The
    pass-path support reuses :func:`tostools.api.tos_writer._load_from_pass`,
    the same helper backing the ``[tos]`` credentials, so the operator gets the
    identical "cleartext for convenience, pass for secrecy" choice.

    Returns ``(username, password)`` — either may be ``None`` if unresolved.
    """
    # Reuse the battle-tested pass resolver from tostools (already a dependency).
    try:
        from tostools.api.tos_writer import _load_from_pass
    except Exception:  # noqa: BLE001 — pass support optional; cleartext still works

        def _load_from_pass(pass_spec: str) -> Optional[str]:  # type: ignore[misc]
            return None

    path = _find_receivers_cfg(cfg_path)
    cfg_user = cfg_pass = None
    if path:
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(path)
            if cp.has_section("teltonika"):
                u_pp = cp.get("teltonika", "username_pass_path", fallback=None)
                if u_pp:
                    cfg_user = _load_from_pass(u_pp.strip())
                if not cfg_user:
                    cfg_user = cp.get("teltonika", "username", fallback=None) or None
                p_pp = cp.get("teltonika", "password_pass_path", fallback=None)
                if p_pp:
                    cfg_pass = _load_from_pass(p_pp.strip())
                if not cfg_pass:
                    cfg_pass = cp.get("teltonika", "password", fallback=None) or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("teltonika cred load from %s failed: %s", path, exc)

    return (username or cfg_user, password or cfg_pass)


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


def _conntype_to_subtype(conntype: Optional[str]) -> Optional[str]:
    """Map a RutOS ``conntype`` (e.g. ``"4G (LTE)"``) to a TOS modem subtype.

    TOS records modem ``subtype`` as a bare generation string (``"3G"``/``"4G"``/
    ``"5G"`` — verified against existing router entities). RutOS reports richer
    strings like ``"4G (LTE)"``; take the leading ``<n>G`` token.
    """
    if not conntype:
        return None
    token = conntype.strip().split()[0]  # "4G (LTE)" → "4G"
    return token or None


def _wan_ip(interfaces_data: Any) -> Optional[str]:
    """Pull the WAN IPv4 from /api/interfaces/status: the default-route iface.

    The WAN interface is the one carrying a default route (``route[].target ==
    "0.0.0.0"``); its ``ipv4-address[0].address`` is the public/SIM IP. Falls
    back to the first non-loopback, non-private-LAN address if no default route
    is found.
    """
    if not isinstance(interfaces_data, list):
        return None
    fallback: Optional[str] = None
    for iface in interfaces_data:
        if not isinstance(iface, dict):
            continue
        addrs = iface.get("ipv4-address") or []
        addr = addrs[0].get("address") if addrs and isinstance(addrs[0], dict) else None
        if not addr:
            continue
        routes = iface.get("route") or []
        has_default = any(
            isinstance(r, dict) and r.get("target") == "0.0.0.0" for r in routes
        )
        if has_default:
            return addr
        if fallback is None and addr != "127.0.0.1":
            fallback = addr
    return fallback


def probe_teltonika(
    host: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify_tls: bool = False,
) -> TelemetryIdentity:
    """Probe a Teltonika RutOS router and return its telemetry identity.

    Args:
        host: Router IP or hostname (scheme optional; HTTPS assumed).
        username / password: Explicit credential overrides; resolved from
            ``receivers.cfg [teltonika]`` when omitted.
        cfg_path: Override receivers.cfg location (testing / non-standard host).
        timeout: Per-request timeout in seconds.
        verify_tls: Verify the router's TLS cert. Default ``False`` — fleet
            routers use self-signed certs. Set ``True`` only with a real cert.

    Returns:
        :class:`TelemetryIdentity` with whatever fields the router exposed.

    Raises:
        ProbeCredentialsError: No usable credentials.
        ProbeAuthError: Login rejected (bad credentials).
        ProbeUnreachableError: Network/TLS failure or unexpected HTTP error.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover — requests is a hard dep
        raise ProbeError("requests not installed — cannot probe router") from exc

    user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not user or not pw:
        raise ProbeCredentialsError(
            "No Teltonika credentials. Add a [teltonika] section to "
            "receivers.cfg (username/password or username_pass_path/"
            "password_pass_path) or pass --username/--password."
        )

    base = host if host.startswith("http") else f"https://{host}"
    base = base.rstrip("/")

    session = requests.Session()
    session.verify = verify_tls
    if not verify_tls:
        # Suppress the noisy per-request InsecureRequestWarning — self-signed
        # router certs are expected and the operator opted in via verify_tls.
        try:
            from urllib3.exceptions import InsecureRequestWarning

            requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
                InsecureRequestWarning
            )
        except Exception:  # noqa: BLE001
            pass

    # --- Login -----------------------------------------------------------
    try:
        resp = session.post(
            f"{base}/api/login",
            json={"username": user, "password": pw},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise ProbeUnreachableError(f"{host}: login request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise ProbeAuthError(f"{host}: login rejected (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise ProbeUnreachableError(
            f"{host}: unexpected login status HTTP {resp.status_code}"
        )
    try:
        token = (resp.json().get("data") or {}).get("token")
    except (ValueError, AttributeError) as exc:
        raise ProbeUnreachableError(f"{host}: login response not JSON") from exc
    if not token:
        raise ProbeAuthError(f"{host}: login succeeded but no token returned")

    headers = {"Authorization": f"Bearer {token}"}

    def _get(endpoint: str) -> Optional[Any]:
        """GET an endpoint; return its ``data`` payload, or None on any failure.

        Probes are best-effort per-field — a missing/forbidden endpoint degrades
        the identity (fields stay None) rather than failing the whole probe.
        """
        try:
            r = session.get(f"{base}{endpoint}", headers=headers, timeout=timeout)
            if r.status_code != 200:
                logger.debug("%s %s → HTTP %s", host, endpoint, r.status_code)
                return None
            return r.json().get("data")
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.debug("%s %s failed: %s", host, endpoint, exc)
            return None

    identity = TelemetryIdentity(host=host)

    # --- Router identity: /api/system/device/status ----------------------
    dev = _get("/api/system/device/status")
    if isinstance(dev, dict):
        mnf = dev.get("mnfinfo") or {}
        identity.router_serial = mnf.get("serial") or None
        static = dev.get("static") or {}
        identity.router_model = static.get("device_name") or None
        identity.router_firmware = static.get("fw_version") or None
        # Prefer the colon-formatted WAN MAC from ports[] (matches existing TOS
        # mac_address values); fall back to mnfinfo.macEth (hex, no colons).
        identity.router_mac = _extract_wan_mac(dev) or _format_mac(mnf.get("macEth"))
        # Teltonika is the manufacturer for all RUT* devices.
        if identity.router_model or identity.router_serial:
            identity.router_manufacturer = "Teltonika"

    # --- SIM / mobile: /api/modems/status --------------------------------
    modems = _get("/api/modems/status")
    if isinstance(modems, list) and modems and isinstance(modems[0], dict):
        m = modems[0]
        identity.sim_iccid = m.get("iccid") or None
        identity.provider = m.get("operator") or m.get("oper") or None
        identity.modem_subtype = _conntype_to_subtype(m.get("conntype"))
        identity.imsi = m.get("imsi") or None
        identity.imei = m.get("imei") or None

    # --- WAN IP (the SIM's ip_address): /api/interfaces/status -----------
    identity.sim_ip_address = _wan_ip(_get("/api/interfaces/status"))

    return identity


def _format_mac(hex_mac: Optional[str]) -> Optional[str]:
    """Format a colon-less hex MAC (``2097270A7727``) as ``20:97:27:0A:77:27``."""
    if not hex_mac:
        return None
    h = hex_mac.strip().replace(":", "")
    if len(h) != 12:
        return hex_mac  # unexpected shape — return as-is rather than mangle
    return ":".join(h[i : i + 2] for i in range(0, 12, 2)).upper()


def _extract_wan_mac(device_status: Dict[str, Any]) -> Optional[str]:
    """Return the WAN port's colon-formatted MAC from device/status ports[]."""
    for port in device_status.get("ports") or []:
        if isinstance(port, dict) and (port.get("name") or "").upper() == "WAN":
            mac = port.get("mac")
            if mac:
                return mac.upper()
    return None
