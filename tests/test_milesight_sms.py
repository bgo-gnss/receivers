"""`MilesightClient.send_sms` / `query_sms` — the recipient field, and its trap.

`yruo_sms`, `query_inbox` and `query_outbox` appear in NO file the router
serves: not index.html, not index-*.js, not the 1.5 MB routes-*.js. They are
assembled at runtime, so grep cannot find them at any download size — an
earlier attempt also drew a conclusion from a TRUNCATED routes.js (389 KB of
1,583 KB, a silent `curl --max-time` cut) and wrongly reported that the
firmware had no SMS support. The core was captured by hooking
`XMLHttpRequest.send` in the live UI and clicking the harmless Inbox/Outbox
Search. `function` always equals `base`.

**The recipient field is `destination`** — the Send form's DOM ids
(`1_destination` / `1_content`) named it correctly. A send whose value carries
no `destination` is ACCEPTED and SILENTLY DISCARDED. Measured on 10.6.1.211
(VFLS) 2026-10-02, three trials per variant, each outcome identified by the
CONTENT of the new outbox row rather than by the counter alone::

    {"destination": N, "content": C}              -> sent, 3/3
    {"number": N, "destination": N, "content": C} -> sent
    {"number": N, "content": C}                   -> NOTHING, 3/3

All three return `status 0` with `result [{}]`, and the no-op never reaches the
outbox nor moves the modem's monthly counter, even polled for 60 s. So no
assertion on the RETURN value can tell the fix from the regression: **the
payload is the only level at which this bug class is visible.** `f01e5c7` sent
`destination` and messages went out; `fda6e36` changed it to a value-wrapped
`number`, sends stopped, and because the response was unchanged it looked like
a flaky modem for a session. A test here asserted `number` and passed
throughout.

These tests drive `cgi` through a fake, so no router is contacted.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from receivers.cfg.milesight_api import MilesightClient, MilesightError


class _SmsRouter:
    """Models the router's REAL reply shape — a WRAPPER — and its discard.

    Two deliberate pieces of hostility, each from a bug a cooperative fake hid:

    * **The wrapper.** The first version of this fake returned
      ``{"result": [{"query_outbox": [...]}]}`` — invented, never observed —
      and the client returned that wrapper unchanged. An EMPTY mailbox
      therefore had ``len() == 1``, so send verification compared 1 to 1 and
      reported "accepted and discarded" for every attempt. Every test passed,
      because fake and code shared the same wrong assumption. What 10.6.1.211
      actually returns (the zone was ``Europe/London`` when this was first
      captured; it now reads ``Atlantic/Iceland``, and either way the row
      timestamps are router-local and run +1 h from UTC)::

          [{"timezone": "UTC Europe/London", "count": 0, "get": []}]

    * **The discard.** This fake answers ``status 0`` to ANY send but only
      transmits when ``value.destination`` is present. An earlier version
      counted every send regardless of field, which is why the test asserting
      ``number`` passed against code that sent nothing.
    """

    def __init__(self, outbox=None, *, status: int = 0, stores: bool = True):
        self.outbox: List[Dict[str, Any]] = list(outbox or [])
        self.status = status
        self.stores = stores
        self.calls: List[Dict[str, Any]] = []
        # The modem's own counter — the oracle for a send.
        self.sent_count = 0

    @staticmethod
    def _wrap(messages):
        return {
            "status": 0,
            "result": [
                {
                    "timezone": "UTC Atlantic/Iceland",
                    "count": len(messages),
                    "get": list(messages),
                }
            ],
        }

    def cgi(self, core, function, values, idn=9):
        self.calls.append({"core": core, "function": function, "values": values})
        if core == "yruo_status" and function == "get":
            return {
                "status": 0,
                "result": [
                    {"get": [{"value": {"cell": {"sim_monthly_sms": self.sent_count}}}]}
                ],
            }
        if function == "query_outbox":
            return self._wrap(self.outbox)
        if function == "query_inbox":
            return self._wrap([])
        if function == "send":
            value = (values[0] or {}).get("value") or {}
            # The router's real behaviour: parse it, then drop it on the floor
            # unless the recipient arrived under the name it recognises.
            if self.status == 0 and self.stores and value.get("destination"):
                self.sent_count += 1
                self.outbox.insert(
                    0,
                    {
                        "type": "sms_outbox",
                        "value": {
                            "from": value.get("destination"),
                            "content": value.get("content"),
                            "status": "success",
                        },
                    },
                )
            return {"status": self.status}
        raise AssertionError(f"unexpected function {function!r}")


def _client(router: _SmsRouter) -> MilesightClient:
    client = MilesightClient.__new__(MilesightClient)  # no network, no login
    client.host = "router.test"
    client.cgi = router.cgi  # type: ignore[method-assign]
    return client


# --- the payload -----------------------------------------------------------


def test_the_wire_field_is_destination_not_number():
    """The single assertion that distinguishes the fix from the regression."""
    router = _SmsRouter()
    _client(router).send_sms("+3548400754", "hello")

    send = next(c for c in router.calls if c["function"] == "send")
    assert send["core"] == "yruo_sms"
    value = send["values"][0]["value"]
    assert value["destination"] == "+3548400754", (
        "the recipient goes in `destination`; `number` is accepted and "
        "silently discarded (status 0, nothing sent)"
    )
    assert "number" not in value, "`number` is not a field this core reads"
    assert value["content"] == "hello"


def test_the_send_is_value_wrapped_under_base_send():
    """A flat payload is accepted (status 0) and discarded, same as a bad field."""
    router = _SmsRouter()
    _client(router).send_sms("+3548400754", "hello")

    item = next(c for c in router.calls if c["function"] == "send")["values"][0]
    assert item["base"] == "send", "function and base are always the same token"
    assert "value" in item, "a flat payload is accepted (status 0) and discarded"
    assert set(item["value"]) == {"destination", "content"}
    assert "destination" not in item, "the recipient is nested, not top-level"


def test_a_send_with_the_right_field_actually_transmits():
    """End-to-end against the discard model: the payload moves the counter."""
    router = _SmsRouter()
    out = _client(router).send_sms("+3548400754", "hello")

    assert router.sent_count == 1
    assert out["confirmed"] is True
    assert out["sent_count"] == 1
    assert out["outbox"] is not None


# --- verification semantics ------------------------------------------------


def test_an_unconfirmed_send_is_reported_not_raised():
    """`confirmed=False` must not be an exception.

    It now means something definite — the recipient field reached the router
    but nothing was transmitted — rather than "queued, will arrive". It stays
    advisory because `discover-phone`'s operator reads the sender number off
    the catcher phone, so a slow modem should not fail the verb. Treating "not
    yet confirmed" as failure produced two successive wrong diagnoses before
    the field name was found, which is the other reason not to raise here.
    """
    router = _SmsRouter(stores=False)
    out = _client(router).send_sms("+3548400754", "hello", verify_timeout=0.1)
    assert out["confirmed"] is False
    assert out["response"]["status"] == 0
    assert router.sent_count == 0


def test_send_sms_can_skip_verification():
    router = _SmsRouter(stores=False)
    _client(router).send_sms("+3548400754", "hello", verify=False)
    assert not any(c["function"] == "query_outbox" for c in router.calls)


def test_a_nonzero_status_on_send_raises():
    """`status != 0` is the one failure the router itself admits to."""
    router = _SmsRouter(status=-1)
    with pytest.raises(MilesightError, match="status=-1"):
        _client(router).send_sms("+3548400754", "hello")


# --- mailbox reads ---------------------------------------------------------


def test_query_sms_rejects_an_unknown_box():
    with pytest.raises(ValueError):
        _client(_SmsRouter()).query_sms("drafts")


def test_an_empty_mailbox_reads_as_zero_messages_not_one_wrapper():
    """The bug the fake previously hid.

    The reply is a wrapper carrying `count` and `get`. Returning it unchanged
    made an EMPTY mailbox look like one message, which silently defeated the
    send verification against the live router — it compared 1 to 1 and blamed
    the payload every time.
    """
    assert _client(_SmsRouter()).query_sms("outbox") == []


# --- the CALL SITE ---------------------------------------------------------
# Covering `send_sms` alone is not enough: the verb operators actually run is
# `cfg discover-phone`, which reaches the payload through
# `_discover_phone_milesight`. A helper-only test passes while the wiring is
# reverted, which is how an abstraction ends up better tested than the code.


class _FakeClientFactory:
    """Stands in for `MilesightClient` inside the CLI's local import."""

    def __init__(self, router: _SmsRouter) -> None:
        self.router = router
        self.connected = False

    def __call__(self, host, *a, **k):
        self.host = host
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self):
        self.connected = True

    def send_sms(self, destination, content, **kw):
        return MilesightClient.send_sms(
            _client(self.router), destination, content, **kw
        )


def test_discover_phone_milesight_sends_destination(monkeypatch, capsys):
    """The verb operators run must put the recipient in `destination`."""
    from receivers.cfg import milesight_api
    from receivers.cli.cfg import _discover_phone_milesight

    router = _SmsRouter()
    monkeypatch.setattr(milesight_api, "MilesightClient", _FakeClientFactory(router))

    rc = _discover_phone_milesight(
        "router.test", "+3548400754", "GPS SIM MSISDN discovery", dry_run=False
    )

    assert rc == 0
    send = next(c for c in router.calls if c["function"] == "send")
    assert send["values"][0]["value"]["destination"] == "+3548400754"
    assert router.sent_count == 1, "the CLI path must transmit, not just return 0"
    assert "CONFIRMED" in capsys.readouterr().out


def test_discover_phone_milesight_no_longer_blames_the_firmware(monkeypatch, capsys):
    """The unconfirmed hint must not send the operator to the router UI.

    That advice came from the misdiagnosis: the API looked unreliable only
    because its recipient field was wrong. Keeping it would teach operators to
    route around a bug that is fixed.
    """
    from receivers.cfg import milesight_api
    from receivers.cli.cfg import _discover_phone_milesight

    router = _SmsRouter(stores=False)
    factory = _FakeClientFactory(router)
    monkeypatch.setattr(milesight_api, "MilesightClient", factory)

    _discover_phone_milesight("router.test", "+3548400754", "hi", dry_run=False)

    out = capsys.readouterr().out
    assert "NOT confirmed" in out
    assert "which still works" not in out, "the stale 'use the UI' advice is back"
    assert (
        "unreliable" not in out.lower()
    ), "delivery is not unreliable; the field name was wrong"
