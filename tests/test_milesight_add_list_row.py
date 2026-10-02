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
