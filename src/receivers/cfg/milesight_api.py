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
    base = os.environ.get("GPS_CONFIG_PATH") or os.path.expanduser(
        "~/.config/gpsconfig"
    )
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
            cfg_user = (
                load_password_from_pass(u_pp.strip())
                if u_pp
                else cp.get(section, "username", fallback=None)
            )
            p_pp = cp.get(section, "password_pass_path", fallback=None)
            cfg_pass = (
                load_password_from_pass(p_pp.strip())
                if p_pp
                else cp.get(section, "password", fallback=None)
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

    def __enter__(self) -> MilesightClient:
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
        raise MilesightUnreachableError(
            f"{self.host}: /islogin gave no JSON on any scheme"
        )

    def connect(self) -> MilesightClient:
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
            "values": [
                {"username": self._user, "password": _encrypt_password(self._pass)}
            ],
        }
        try:
            r = self._session.post(
                f"{self._base()}/cgi", json=payload, timeout=self.timeout
            )
        except requests.exceptions.RequestException as exc:
            raise MilesightUnreachableError(
                f"{self.host}: login failed: {exc}"
            ) from exc
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

    def cgi(self, core: str, function: str, values: list, idn: int = 9) -> dict:
        """Raw ``POST /cgi`` JSON-RPC call; returns the parsed response."""
        if not self._td:
            self.login()
        payload = {
            "id": idn,
            "execute": 1,
            "core": core,
            "function": function,
            "values": values,
        }
        r = self._session.post(
            f"{self._base()}/cgi", json=payload, timeout=self.timeout
        )
        return r.json()

    def get_config(self, core: str, base: Optional[str] = None) -> list:
        """GET a config domain; returns ``result[0].get`` (list of {type,index,value}).

        The SPA addresses each page by a ``yruo_*`` core (e.g. ``yruo_cell``,
        ``yruo_bridge``, ``yruo_dhcpserver``, ``yruo_firewall_security``). Discover
        new ones by capturing the page's own ``get`` XHR.
        """
        j = self.cgi(core, "get", [{"base": base or core}])
        try:
            return j["result"][0]["get"]
        except (KeyError, IndexError, TypeError):
            raise MilesightError(f"{self.host}: get {core} unexpected: {str(j)[:120]}")

    def set_singleton(
        self, core: str, index, value: dict, base: Optional[str] = None
    ) -> None:
        """SET one SINGLETON config object (cell/bridge/dhcp/security/…).

        Read-modify-write: fetch with :meth:`get_config`, patch the value, pass
        it here with the same ``index`` the get returned. For a LIST config
        (e.g. ``yruo_firewall_port_mapping``) use :meth:`add_list_row`, which
        has its own index convention.
        """
        j = self.cgi(
            core, "set", [{"base": base or core, "index": index, "value": value}]
        )
        if j.get("status") != 0:
            raise MilesightError(
                f"{self.host}: set {core} failed status={j.get('status')}"
            )

    def apply(self) -> bool:
        """COMMIT staged config to the running config (the UI's "Apply" button).

        **A ``set`` only stages.** It updates the config store — a subsequent
        ``get`` reads the new value back, which makes it look applied — but the
        RUNNING config does not take it until this commit, and a reboot REVERTS
        anything uncommitted. Measured 2026-09-29: cellular ``pri_dns1``/
        ``sec_dns1`` were set, read back correctly, and were empty after a
        reboot, because only the UI-saved values had been committed.

        The call is ``function="apply"`` — NOT ``"set"``. Guessing ``set``/``add``/
        ``get`` against ``yruo_apply`` returns ``status=-1``, which is why this
        was recorded as unmapped for two sessions. The true form was recovered
        from the router's own SPA bundle (``/assets/routes-*.js``)::

            {execute:1, core:"yruo_apply", function:"apply", values:[]}

        Returns:
            ``True`` when the router signals it needs a reboot to finish
            applying (``result[0].reboot``), else ``False``.

        Note:
            This commits SINGLETON writes. It does NOT rescue a LIST config
            (``yruo_firewall_port_mapping``) — those never reach the store in
            the first place, so there is nothing for apply to commit. See
            :meth:`set_singleton`.
        """
        j = self.cgi("yruo_apply", "apply", [])
        if j.get("status") != 0:
            raise MilesightError(f"{self.host}: apply failed status={j.get('status')}")
        try:
            return bool(j["result"][0].get("reboot"))
        except (KeyError, IndexError, TypeError):
            return False

    def patch_singleton(
        self,
        core: str,
        patch: dict,
        base: Optional[str] = None,
        commit: bool = False,
    ) -> dict:
        """Read-modify-write a singleton: GET, apply ``patch``, SET; return new value.

        Args:
            commit: Also call :meth:`apply`, committing the change to the running
                config. Defaults to ``False`` so existing callers keep their
                stage-only behaviour; pass ``True`` for a write that must survive
                a reboot.
        """
        entry = self.get_config(core, base)[0]
        value = dict(entry["value"])
        value.update(patch)
        self.set_singleton(core, entry["index"], value, base)
        if commit:
            self.apply()
        return value

    def add_list_row(
        self,
        core: str,
        value: dict,
        base: Optional[str] = None,
        commit: bool = True,
    ) -> list:
        """ADD one row to a LIST config (port-forwards, firewall rules, …).

        LIST configs do not take the singleton save format, and getting this
        wrong is near-silent: a payload the router does not like returns
        ``status 0`` and stores NOTHING. That is why this stayed unmapped for
        three sessions and ~10 blind attempts, and it is why this method
        RE-READS the collection and raises unless the row actually appeared.

        The format was recovered by hooking ``XMLHttpRequest.prototype.send``
        and performing one real Save in the router's own UI. Two parts of it
        are unguessable:

        * the OUTER ``index`` is a **synthetic string**: the core name plus a
          random token. A plain integer is silently ignored — ``index: 1`` in
          particular is a no-op that reports success.
        * the INNER ``value.index`` must be **``null``**. The router assigns
          the real row index itself.

        Args:
            core: the LIST core, e.g. ``yruo_firewall_port_mapping``.
            value: the row. ``index`` is forced to ``None``; anything else you
                pass is sent as given.
            base: config base, defaulting to ``core``.
            commit: call :meth:`apply` afterwards. Defaults to **True** here,
                unlike :meth:`patch_singleton` — a staged firewall rule that
                reverts on the next reboot is a trap, and there is no reason
                to add a row you do not mean to keep.

        Returns:
            The collection as re-read after the write, so the caller sees the
            row indexes the router assigned.

        Raises:
            MilesightError: the router reported failure, or accepted the call
                and stored nothing.
        """
        import random
        import string

        before = self.get_config(core, base)
        row = dict(value)
        row["index"] = None
        token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        j = self.cgi(
            core,
            "add",
            [{"base": base or core, "index": f"{core}{token}", "value": row}],
        )
        if j.get("status") != 0:
            raise MilesightError(
                f"{self.host}: add {core} failed status={j.get('status')}"
            )
        if commit:
            self.apply()
        after = self.get_config(core, base)
        if len(after) <= len(before):
            # The documented failure mode: status 0, nothing stored.
            raise MilesightError(
                f"{self.host}: add {core} reported success but the collection "
                f"still holds {len(after)} row(s) — the payload was accepted "
                f"and discarded. Re-capture the UI's save XHR for this core."
            )
        return after

    # ------------------------------------------------------------------
    # SMS
    # ------------------------------------------------------------------
    #
    # The `yruo_sms` core is NOT discoverable from the SPA bundle. Unlike
    # `yruo_apply` (MJ-570), the strings "yruo_sms", "query_inbox" and
    # "query_outbox" appear in NO file the router serves — not
    # `index.html`, not `index-*.js`, not the 1.5 MB `routes-*.js` — so they
    # are assembled at runtime from minified fragments. Grepping cannot find
    # them at any download size, and an earlier attempt also drew a
    # conclusion from a TRUNCATED `routes.js` (389 KB of 1,583 KB, a silent
    # `curl --max-time` cut) and wrongly reported that the firmware had no
    # SMS support at all.
    #
    # Captured instead by hooking XMLHttpRequest.send in the live UI
    # (10.6.1.211, fw 32.3.0.14, 2026-10-02) and clicking Inbox/Outbox
    # Search — both harmless reads:
    #
    #   {"id":10,"execute":1,"core":"yruo_sms","function":"query_inbox",
    #    "values":[{"base":"query_inbox","limit":10,"start":0,
    #               "language":"en","key":"time","order":0,
    #               "start_date":"","end_date":"","from":""}]}
    #
    # Two invariants came out of it: `function` always equals `base`, and
    # READS are flat — `values:[{base, ...params}]`.
    #
    # The SEND is NOT flat. It is value-wrapped, like the LIST writes:
    #
    #   {"core":"yruo_sms","function":"send",
    #    "values":[{"base":"send","value":{"destination":..., "content":...}}]}
    #
    # Confirmed live against 10.6.1.211 on 2026-10-02 — the message reached
    # the outbox with status "success".
    #
    # The field IS `destination`, and the Send form's DOM ids
    # (`1_destination` / `1_content`) named it correctly all along. An earlier
    # revision of this comment asserted the opposite — that the field was
    # `number` and the DOM ids were a red herring — on the strength of a
    # status-0 response. That was wrong, and it cost a session: this core
    # returns status 0 for a send whose recipient field it does not recognise,
    # discards it, and writes nothing anywhere. See `send_sms` for the
    # three-trial matrix that settled it.

    SMS_CORE = "yruo_sms"

    def query_sms(self, box: str = "outbox", limit: int = 10) -> list:
        """Read the SMS ``outbox`` or ``inbox``.

        Both are plain reads and cost nothing. The outbox is what makes
        :meth:`send_sms` verifiable, which Teltonika's ``gsmctl`` path
        cannot offer.
        """
        if box not in ("inbox", "outbox"):
            raise ValueError("box must be 'inbox' or 'outbox'")
        base = f"query_{box}"
        j = self.cgi(
            self.SMS_CORE,
            base,
            [
                {
                    "base": base,
                    "limit": limit,
                    "start": 0,
                    "language": "en",
                    "key": "time",
                    "order": 0,
                    "start_date": "",
                    "end_date": "",
                    "from": "",
                }
            ],
        )
        if j.get("status") != 0:
            raise MilesightError(
                f"{self.host}: {base} failed status={j.get('status')} "
                f"{str(j.get('result'))[:80]}"
            )
        # The reply is a WRAPPER, not the messages:
        #   [{"timezone": "UTC Europe/London", "count": 0, "get": []}]
        # Returning it unchanged makes len() == 1 for an EMPTY mailbox, which
        # silently defeated the send verification — it compared 1 to 1 and
        # reported "the payload was accepted and discarded" for every attempt,
        # including ones that might have worked. Unwrap to the message list.
        try:
            return j["result"][0]["get"] or []
        except (KeyError, IndexError, TypeError):
            return []

    def sms_sent_count(self) -> int:
        """The modem's own count of SMS sent this month.

        The confirmation source for :meth:`send_sms`, because it comes from
        the modem rather than the UI. It agrees with the outbox: 7 sends, 7
        rows on 10.6.1.211 on 2026-10-02. An earlier docstring here claimed
        the outbox "drops messages" on the strength of a single 4-vs-3
        disagreement; that is **not reproduced**, and no cause for it has been
        established — the obvious candidate, the pre-``bfa5a9f``
        :meth:`query_sms` returning the reply WRAPPER, does not fit (it gives
        a length of 1 for any mailbox, not 3). Treat both signals as sound.
        This one is used because a count comparison needs no row matching.
        """
        v = (self.get_config("yruo_status", base="summary")[0] or {}).get("value") or {}
        return int(((v.get("cell") or {}).get("sim_monthly_sms")) or 0)

    def send_sms(
        self,
        destination: str,
        content: str,
        verify: bool = True,
        verify_timeout: float = 20.0,
    ) -> dict:
        """Send one SMS. Costs a message.

        Args:
            destination: recipient number, as typed into the UI's "Phone
                Number" field. The wire field is ``destination`` — see the
                Note; the form's ``1_destination`` DOM id was the clue.
            content: message body.
            verify: poll for CONFIRMATION of transmission. Advisory — an
                unconfirmed send is reported, never raised. See below.
            verify_timeout: how long to poll for confirmation, in seconds.

        Returns:
            ``{"confirmed": bool, "sent_count": int, "outbox": row|None,
            "response": raw}``. ``confirmed`` means the modem's counter rose
            within ``verify_timeout``.

        Raises:
            MilesightError: only when the router itself reports failure
                (``status != 0``).

        Note:
            **The wire field is ``destination``. ``number`` is ACCEPTED and
            silently DISCARDED.** Measured on 10.6.1.211 (VFLS), 2026-10-02,
            three trials each, every outcome identified by the CONTENT of the
            new outbox row rather than by the counter alone::

                {"destination": N, "content": C}            -> sent, 3/3
                {"number": N, "destination": N, "content": C} -> sent
                {"number": N, "content": C}                 -> NOTHING, 3/3

            Every one of those returned ``status 0`` with ``result [{}]``. The
            no-op case never reaches the outbox and never moves the modem's
            monthly counter, even after polling 60 s. So on this core
            ``status 0`` says only "the request parsed" — it is not an
            acknowledgement that a message exists.

            This is what made the field name expensive to find. ``f01e5c7``
            (09:31 UTC) sent ``destination`` (at the item's top level) and
            messages went out; ``fda6e36`` (10:05 UTC) "fixed" it to a
            value-wrapped ``number`` and sends stopped, but because the response was unchanged the symptom
            looked like a flaky modem rather than a regression. Three further
            hypotheses were chased and each disproved — an anti-flood limit
            (``statistics.sim1_sms_overflow`` reads ``0``), a wedged queue
            (``modem_status`` ``Ready``, registered, RSSI -62 dBm), and a
            missing ``apply()`` commit (no effect). The form's own
            ``1_destination`` DOM id had named the field all along.

            **Mailbox row timestamps are rendered +1 h — but the router's
            clock is CORRECT.** Two fields disagree, sampled in the same
            second on both units, 2026-10-02:

                laptop UTC                              12:53:04
                yruo_system  base=time   current_time   12:53:05   <- true
                yruo_status  base=summary system.local_time 13:53:07  <- +1 h

            The clock is the first one. Proof independent of any config read:
            `summary.system.uptime` wound back from laptop UTC gives boot
            2026-09-30 18:45:27 (VFLS) and 17:45:52 (VFLN), matching the known
            install instants 18:45:29 / 17:46:18 to within seconds, whereas
            winding back from ``local_time`` puts boot an hour late. Timezone
            is ``Atlantic/Iceland`` (UTC+0, no DST), ``ntp_enable 1`` against
            10.170.255.210/.220 — all correct, nothing to fix on the device.
            The +1 h is a firmware RENDERING fault in that status field and in
            the SMS mailbox rows, most likely falling back to the zone's DST
            sibling (Europe/London, BST until 2026-10-25).

            So: **subtract an hour from an outbox/inbox row before comparing
            it to a server log, and never "correct" the router clock on the
            strength of `local_time`** — that would push the real clock an
            hour wrong. The four deliveries read 10:56-11:01 in the outbox,
            i.e. 09:56-10:01 UTC: after ``f01e5c7``, before ``fda6e36``.

            ``verify`` remains advisory: an unconfirmed send is reported, not
            raised, because for ``discover-phone`` the operator reads the
            sender number off the catcher phone and a slow confirmation is
            worth reporting rather than failing. With the correct field every
            observed send confirmed within 25 s.
        """
        import time as _time

        before = self.sms_sent_count() if verify else 0

        j = self.cgi(
            self.SMS_CORE,
            "send",
            # ``destination``, NOT ``number`` — see the Note. A wrong or
            # absent field name here is a SILENT discard: status 0, nothing
            # sent, nothing logged by the router.
            [
                {
                    "base": "send",
                    "value": {"destination": destination, "content": content},
                }
            ],
        )
        if j.get("status") != 0:
            raise MilesightError(
                f"{self.host}: send_sms failed status={j.get('status')} "
                f"{str(j.get('result'))[:120]}"
            )
        if not verify:
            return {
                "confirmed": None,
                "sent_count": None,
                "outbox": None,
                "response": j,
            }
        deadline = _time.monotonic() + verify_timeout
        sent = self.sms_sent_count()
        while sent <= before and _time.monotonic() < deadline:
            _time.sleep(2.0)
            sent = self.sms_sent_count()
        rows = self.query_sms("outbox")
        return {
            "confirmed": sent > before,
            "sent_count": sent,
            "outbox": rows[0] if rows else None,
            "response": j,
        }

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
            f"{self._base()}/cgi-bin/file-export",
            data=form,
            timeout=max(self.timeout, 60),
        )
        body = r.content
        if r.status_code != 200 or body.startswith(b"Export"):
            raise MilesightError(f"{self.host}: export failed: {body[:80]!r}")
        with open(out_path, "wb") as fh:
            fh.write(body)
        logger.info(
            "milesight %s: exported %d bytes -> %s", self.host, len(body), out_path
        )
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
            logger.info(
                "milesight %s: connection reset — router applying + rebooting",
                self.host,
            )
            return
        if r.status_code != 200:
            raise MilesightError(
                f"{self.host}: import HTTP {r.status_code}: {r.content[:80]!r}"
            )
        logger.info("milesight %s: import accepted", self.host)
