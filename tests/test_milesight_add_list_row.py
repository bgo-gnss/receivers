"""`MilesightClient.add_list_row` — the LIST save format, and its trap.

A LIST config (port-forwards, firewall rules) does not take the singleton
save format, and getting it wrong is near-silent: the router answers
``status 0`` and stores nothing. That is why the format stayed unmapped for
three sessions and roughly ten blind attempts, and it is why the method
re-reads the collection instead of trusting the status.

Two parts of the payload are unguessable and were recovered by hooking
``XMLHttpRequest.prototype.send`` during one real Save in the router's own
UI:

* the OUTER ``index`` is a synthetic string — core name plus a random token.
  A plain integer is ignored; ``index: 1`` in particular is a no-op that
  reports success.
* the INNER ``value.index`` must be ``null``; the router assigns the real
  row index itself.

These tests drive `cgi` through a fake, so no router is contacted.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from receivers.cfg.milesight_api import MilesightClient, MilesightError

CORE = "yruo_firewall_port_mapping"


class _FakeRouter:
    """Records calls and models the store, so the re-read means something."""

    def __init__(
        self, rows: List[Dict[str, Any]] | None = None, *, store=True, status: int = 0
    ):
        self.rows = list(rows or [])
        self.store = store
        self.status = status
        self.calls: List[Dict[str, Any]] = []

    def cgi(self, core, function, values, idn=9):
        self.calls.append({"core": core, "function": function, "values": values})
        if function == "get":
            return {"status": 0, "result": [{"get": list(self.rows)}]}
        if function == "add":
            if self.status == 0 and self.store:
                payload = values[0]["value"]
                self.rows.append({"index": len(self.rows), "value": payload})
            return {"status": self.status}
        if function == "apply":
            return {"status": 0, "result": [{"reboot": False}]}
        raise AssertionError(f"unexpected function {function!r}")


def _client(router: _FakeRouter) -> MilesightClient:
    client = MilesightClient.__new__(MilesightClient)  # no network, no login
    client.host = "router.test"
    client.cgi = router.cgi  # type: ignore[method-assign]
    return client


def test_outer_index_is_a_synthetic_string_not_an_integer():
    """`index: 1` is the documented no-op: accepted, stored nothing."""
    router = _FakeRouter()
    _client(router).add_list_row(CORE, {"name": "rinex", "dport": "2101"})

    add = next(c for c in router.calls if c["function"] == "add")
    index = add["values"][0]["index"]
    assert isinstance(index, str), "an int index is silently ignored by the router"
    assert index.startswith(CORE), "the token is appended to the core name"
    assert index != CORE, "a bare core name is not a unique row index"


def test_inner_value_index_is_null():
    """The router assigns the row index; sending one makes the add a no-op."""
    router = _FakeRouter()
    _client(router).add_list_row(CORE, {"name": "rinex", "index": 7})

    add = next(c for c in router.calls if c["function"] == "add")
    assert add["values"][0]["value"]["index"] is None


def test_the_row_is_sent_otherwise_unchanged():
    router = _FakeRouter()
    _client(router).add_list_row(CORE, {"name": "rinex", "dport": "2101"})

    value = next(c for c in router.calls if c["function"] == "add")["values"][0][
        "value"
    ]
    assert value["name"] == "rinex" and value["dport"] == "2101"


def test_a_nonzero_status_raises():
    router = _FakeRouter(status=-1)
    with pytest.raises(MilesightError, match="status=-1"):
        _client(router).add_list_row(CORE, {"name": "rinex"})


def test_status_zero_that_stored_nothing_raises():
    """THE failure mode this method exists to catch.

    Ten blind attempts across three sessions all looked like success. A
    method that trusted the status would have reported the same.
    """
    router = _FakeRouter(store=False)
    with pytest.raises(MilesightError) as exc:
        _client(router).add_list_row(CORE, {"name": "rinex"})
    msg = str(exc.value)
    assert "reported success" in msg
    assert "accepted" in msg and "discarded" in msg
    assert "re-capture" in msg.lower(), "tell the next reader how to fix it"


def test_commit_is_on_by_default():
    """Unlike patch_singleton: a staged firewall rule that reverts on the next
    reboot is a trap, and there is no reason to add a row you will not keep."""
    router = _FakeRouter()
    _client(router).add_list_row(CORE, {"name": "rinex"})
    assert any(c["function"] == "apply" for c in router.calls)


def test_commit_can_be_turned_off():
    router = _FakeRouter()
    _client(router).add_list_row(CORE, {"name": "rinex"}, commit=False)
    assert not any(c["function"] == "apply" for c in router.calls)


def test_returns_the_collection_as_re_read():
    """So the caller sees the indexes the ROUTER assigned, not what we sent."""
    router = _FakeRouter(rows=[{"index": 0, "value": {"name": "existing"}}])
    out = _client(router).add_list_row(CORE, {"name": "rinex"})
    assert len(out) == 2
    assert out[0]["value"]["name"] == "existing"
    assert out[1]["index"] == 1


def test_each_add_gets_a_fresh_token():
    """Two rows must not collide on one synthetic index."""
    router = _FakeRouter()
    c = _client(router)
    c.add_list_row(CORE, {"name": "one"})
    c.add_list_row(CORE, {"name": "two"})
    indexes = [
        call["values"][0]["index"] for call in router.calls if call["function"] == "add"
    ]
    assert len(set(indexes)) == 2


# ---------------------------------------------------------------------------
# SMS — the core the SPA bundle does not contain
# ---------------------------------------------------------------------------
#
# `yruo_sms`, `query_inbox` and `query_outbox` appear in NO file the router
# serves: not index.html, not index-*.js, not the 1.5 MB routes-*.js. They are
# assembled at runtime, so grep cannot find them at any download size — an
# earlier attempt also drew a conclusion from a TRUNCATED routes.js (389 KB of
# 1,583 KB, a silent `curl --max-time` cut) and wrongly reported that the
# firmware had no SMS support.
#
# Captured by hooking XMLHttpRequest.send in the live UI and clicking the
# harmless Inbox/Outbox Search. `function` always equals `base`; the Send
# form's DOM ids are `1_destination` / `1_content`.


class _SmsRouter(_FakeRouter):
    """Models the router's REAL reply shape, which is a WRAPPER.

    This matters more than it looks. The first version of this fake returned
    ``{"result": [{"query_outbox": [...]}]}`` — invented, never observed —
    and the client returned that wrapper unchanged. An EMPTY mailbox
    therefore had ``len() == 1``, so the send verification compared 1 to 1
    and reported "accepted and discarded" for every attempt, including ones
    that might have worked. Every test passed throughout, because fake and
    code shared the same wrong assumption. Only the live router exposed it.

    What 10.6.1.211 actually returns:

        [{"timezone": "UTC Europe/London", "count": 0, "get": []}]
    """

    def __init__(self, outbox=None, *, status=0, stores=True):
        super().__init__(status=status)
        self.outbox = list(outbox or [])
        self.stores = stores

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
        if function == "query_outbox":
            return self._wrap(self.outbox)
        if function == "query_inbox":
            return self._wrap([])
        if function == "send":
            if self.status == 0 and self.stores:
                v = values[0]
                self.outbox.insert(
                    0,
                    {"recipient": v.get("destination"), "content": v.get("content")},
                )
            return {"status": self.status}
        raise AssertionError(f"unexpected function {function!r}")


def test_send_sms_uses_the_captured_core_and_field_names():
    router = _SmsRouter()
    _client(router).send_sms("+3548400754", "hello")

    send = next(c for c in router.calls if c["function"] == "send")
    assert send["core"] == "yruo_sms"
    v = send["values"][0]
    assert v["base"] == "send", "function and base are always the same token"
    # From the form's own DOM ids, not guessed.
    assert v["destination"] == "+3548400754"
    assert v["content"] == "hello"


def test_send_sms_verifies_against_the_outbox():
    """The Milesight advantage over Teltonika's gsmctl: delivery is checkable.

    This router family answers `status 0` to payloads it then discards, so a
    bare status is not evidence — the same trap that made the LIST-write
    format look unsolvable for three sessions.
    """
    router = _SmsRouter(stores=False)
    with pytest.raises(MilesightError) as exc:
        _client(router).send_sms("+3548400754", "hello")
    msg = str(exc.value)
    assert "outbox" in msg
    assert "accepted and" in msg and "discarded" in msg
    assert "function=" in msg, "name the token most likely to be wrong"


def test_send_sms_returns_the_outbox_row():
    router = _SmsRouter()
    row = _client(router).send_sms("+3548400754", "hello")
    assert row is not None


def test_send_sms_can_skip_verification():
    router = _SmsRouter(stores=False)
    _client(router).send_sms("+3548400754", "hello", verify=False)
    assert not any(c["function"] == "query_outbox" for c in router.calls)


def test_a_nonzero_status_on_send_raises():
    router = _SmsRouter(status=-1)
    with pytest.raises(MilesightError, match="status=-1"):
        _client(router).send_sms("+3548400754", "hello")


def test_query_sms_rejects_an_unknown_box():
    with pytest.raises(ValueError):
        _client(_SmsRouter()).query_sms("drafts")


def test_an_empty_mailbox_reads_as_zero_messages_not_one_wrapper():
    """The bug this file previously hid.

    The reply is a wrapper carrying `count` and `get`. Returning it unchanged
    made an EMPTY mailbox look like one message, which silently defeated the
    send verification against the live router — it compared 1 to 1 and blamed
    the payload every time.
    """
    assert _client(_SmsRouter()).query_sms("outbox") == []
    assert _client(_SmsRouter(outbox=[{"recipient": "x"}])).query_sms("outbox") != []
