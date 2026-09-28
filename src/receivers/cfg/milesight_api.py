"""Milesight UR-series router HTTP-API client (config backup/restore + identity).

Milesight UR32/UR32L/UR35 cellular routers expose a private JSON-RPC endpoint
(``POST /cgi``) plus ``/cgi-bin/file-export`` / ``file-import`` for config
backup and restore. This module wraps that surface so the fleet's Milesight
routers can be driven from :mod:`receivers.cfg` the same way Teltonika RutOS
routers are driven by :mod:`receivers.cfg.telemetry_probe` — one client, creds
from ``receivers.cfg``, self-signed TLS tolerated.

Verified live 2026-09-28 against THOC (fw ``32.3.0.8-r1``) and a bench UR32L-L04EU
(fw ``32.3.0.14``). Contract discovered by live capture; see the
``milesight_ur_api`` memory for the reverse-engineering notes.

AUTH (the one wrinkle vs. Teltonika's clean bearer token). ``POST /cgi`` with
``core=user function=login``; the password is **AES-128-CBC encrypted
client-side with a fixed key** (``1111111111111111`` / IV ``2222222222222222``,
PKCS7, base64) — this is the vendor's own (weak, CVE-2023-43261) login scheme,
reproduced here only to authenticate to the operator's own routers. The session
token ``td`` comes back **in the response body** (fw >= 3.0.14) OR **as a
cookie** (fw 3.0.8) — both handled.

TLS. The routers ship an ancient LEDE/OpenSSL stack whose ciphers modern Python
OpenSSL refuses by default (``SSLV3_ALERT_HANDSHAKE_FAILURE``). :class:`_LegacyAdapter`
lowers the security level and allows legacy renegotiation. ``curl -k`` works out
of the box because system OpenSSL is more permissive; requests is not, hence the
adapter. fw 3.0.14 is **HTTPS-only** (HTTP port open but serves nothing); fw
3.0.8 serves plain HTTP — pass ``scheme`` accordingly or use :meth:`connect`.

CONFIG BLOB. Export/import move an opaque OpenSSL ``Salted__`` blob
(``type=backup file=cfgbackup``); the router encrypts it with a device-derived
key, so it is NOT decryptable off-box and NOT portable across major firmware
versions (a 3.0.8 blob is rejected by 3.0.14 — clone only within a version).

Credentials come from ``receivers.cfg`` ``[milesight]`` (cleartext
``username``/``password`` OR ``*_pass_path`` via pass(1)), falling back to
``[teltonika]`` since the fleet shares one router login. Same convention as the
TOS ``[tos]`` section. No secret is ever logged.
"""

from __future__ import annotations

import base64
import configparser
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
# Vendor's fixed login-crypto key/IV (CryptoJS AES-CBC in the router's SPA).
_LOGIN_KEY = b"1111111111111111"
_LOGIN_IV = b"2222222222222222"
# OP_LEGACY_SERVER_CONNECT — allow unsafe legacy renegotiation for the old stack.
_OP_LEGACY_SERVER_CONNECT = 0x4


class MilesightError(Exception):
    """Base class for Milesight client failures."""


class MilesightAuthError(MilesightError):
    """Login rejected (bad credentials, or the 5-try/10-min lockout)."""


class MilesightUnreachableError(MilesightError):
    """Router did not respond."""


@dataclass
class MilesightIdentity:
    """Unauthenticated identity from ``POST /islogin``."""

    host: str
    model: Optional[str] = None
    part_number: Optional[str] = None
    firmware: Optional[str] = None


def _encrypt_password(plaintext: str) -> str:
    """Reproduce the router SPA's login encryption: base64(AES-128-CBC(pkcs7(pw)))."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(_LOGIN_KEY), modes.CBC(_LOGIN_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _find_receivers_cfg(cfg_path: Optional[str] = None) -> Optional[str]:
    """Locate receivers.cfg (explicit → GPS_CONFIG_PATH → default)."""
    if cfg_path:
        return cfg_path
    base = os.environ.get("GPS_CONFIG_PATH") or os.path.expanduser("~/.config/gpsconfig")
    candidate = os.path.join(base, "receivers.cfg")
    return candidate if os.path.isfile(candidate) else None


def resolve_credentials(
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve router creds. Precedence: explicit → [milesight] → [teltonika].

    Within a section: ``*_pass_path`` (via pass(1)) → cleartext. The fleet shares
    one router login, so ``[milesight]`` is optional and ``[teltonika]`` is the
    fallback. Either return value may be ``None`` if unresolved.
    """
    try:
        from tostools.api.tos_writer import load_password_from_pass
    except Exception:  # pragma: no cover - optional dep

        def load_password_from_pass(spec: str) -> Optional[str]:  # type: ignore[misc]
            return None

    cfg_user = cfg_pass = None
    path = _find_receivers_cfg(cfg_path)
    if path:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        for section in ("milesight", "teltonika"):
            if not cp.has_section(section):
                continue
            u_pp = cp.get(section, "username_pass_path", fallback=None)
            cfg_user = load_password_from_pass(u_pp.strip()) if u_pp else cp.get(
                section, "username", fallback=None
            )
            p_pp = cp.get(section, "password_pass_path", fallback=None)
            cfg_pass = load_password_from_pass(p_pp.strip()) if p_pp else cp.get(
                section, "password", fallback=None
            )
            if cfg_user or cfg_pass:
                break
    return (username or cfg_user, password or cfg_pass)


class _LegacyAdapter:
    """requests HTTPAdapter that tolerates the routers' legacy TLS stack."""

    def __new__(cls):
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        class _Adapter(HTTPAdapter):
            def init_poolmanager(self, *a, **k):
                ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=0")
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    ctx.options |= _OP_LEGACY_SERVER_CONNECT
                except Exception:
                    pass
                k["ssl_context"] = ctx
                return super().init_poolmanager(*a, **k)

        return _Adapter()


class MilesightClient:
    """Authenticated session against one Milesight UR-series router.

    Usage::

        with MilesightClient("192.168.1.1") as c:   # auto-detects http/https
            c.login()
            c.export_config("/tmp/thoc.bin")

    ``scheme`` may be forced ("http" for fw 3.0.8, "https" for 3.0.14); by
    default :meth:`connect` probes ``/islogin`` on both.
    """

    def __init__(
        self,
        host: str,
        scheme: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        cfg_path: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        import requests

        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        self.host = host
        self.scheme = scheme
        self.timeout = timeout
        self._user, self._pass = resolve_credentials(username, password, cfg_path)
        self._session = requests.Session()
        self._session.verify = False
        self._session.mount("https://", _LegacyAdapter())
        self._td: Optional[str] = None

    def __enter__(self) -> "MilesightClient":
        return self

    def __exit__(self, *exc) -> None:
        self._session.close()

    def _base(self) -> str:
        return f"{self.scheme}://{self.host}"

    def get_identity(self) -> MilesightIdentity:
        """Unauthenticated ``POST /islogin``; also sets ``scheme`` if unset."""
        import requests

        schemes = [self.scheme] if self.scheme else ["https", "http"]
        for sch in schemes:
            try:
                r = self._session.post(
                    f"{sch}://{self.host}/islogin", timeout=self.timeout
                )
                j = r.json()
                self.scheme = sch
                return MilesightIdentity(
                    host=self.host,
                    model=j.get("model"),
                    part_number=j.get("pn"),
                    firmware=j.get("rtver"),
                )
            except requests.exceptions.RequestException:
                continue
            except ValueError:
                continue
        raise MilesightUnreachableError(f"{self.host}: /islogin gave no JSON on any scheme")

    def connect(self) -> "MilesightClient":
        """Probe identity (fixing scheme) then :meth:`login`."""
        self.get_identity()
        self.login()
        return self

    def login(self) -> None:
        """Authenticate; capture the ``td`` token from body or cookie."""
        import requests

        if not (self._user and self._pass):
            raise MilesightAuthError(
                f"{self.host}: no credentials — set [milesight]/[teltonika] in "
                "receivers.cfg or pass username/password."
            )
        if not self.scheme:
            self.get_identity()
        payload = {
            "id": "1",
            "execute": 1,
            "core": "user",
            "function": "login",
            "values": [{"username": self._user, "password": _encrypt_password(self._pass)}],
        }
        try:
            r = self._session.post(f"{self._base()}/cgi", json=payload, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise MilesightUnreachableError(f"{self.host}: login failed: {exc}") from exc
        try:
            j = r.json()
        except ValueError as exc:
            raise MilesightAuthError(f"{self.host}: login gave non-JSON") from exc
        if j.get("status") != 0:
            raise MilesightAuthError(
                f"{self.host}: login rejected (status={j.get('status')}); "
                "check credentials / lockout (5 tries = 10 min)."
            )
        td = None
        result = j.get("result") or []
        if result and isinstance(result[0], dict):
            td = result[0].get("td")
        td = td or self._session.cookies.get("td")
        if not td:
            raise MilesightAuthError(f"{self.host}: login ok but no session token")
        self._td = td
        self._session.cookies.set("td", td, domain=self.host)
        logger.info("milesight %s: authenticated", self.host)

    def export_config(self, out_path: str) -> int:
        """Download the config backup blob to ``out_path``; return byte count."""
        if not self._td:
            self.login()
        form = {
            "sessionid": self._td,
            "type": "backup",
            "file": "cfgbackup",
            "filename": "type=backup&file=cfgbackup",
        }
        r = self._session.post(
            f"{self._base()}/cgi-bin/file-export", data=form, timeout=max(self.timeout, 60)
        )
        body = r.content
        if r.status_code != 200 or body.startswith(b"Export"):
            raise MilesightError(f"{self.host}: export failed: {body[:80]!r}")
        with open(out_path, "wb") as fh:
            fh.write(body)
        logger.info("milesight %s: exported %d bytes -> %s", self.host, len(body), out_path)
        return len(body)

    def import_config(self, in_path: str) -> None:
        """Upload a config backup blob and apply it (router reboots).

        NOTE: only portable within a firmware major version — a 3.0.8 blob is
        rejected by 3.0.14. The router resets the connection as it reboots; that
        is success, not an error.
        """
        import requests

        if not self._td:
            self.login()
        blob = open(in_path, "rb").read()
        data = {
            "sessionid": self._td,
            "filename": "type=backup&file=cfgbackup",
            "size": str(len(blob)),
        }
        files = {"file": ("cfgbackup", blob, "application/octet-stream")}
        try:
            r = self._session.post(
                f"{self._base()}/cgi-bin/file-import",
                data=data,
                files=files,
                timeout=max(self.timeout, 90),
            )
        except requests.exceptions.ConnectionError:
            logger.info("milesight %s: connection reset — router applying + rebooting", self.host)
            return
        if r.status_code != 200:
            raise MilesightError(f"{self.host}: import HTTP {r.status_code}: {r.content[:80]!r}")
        logger.info("milesight %s: import accepted", self.host)
