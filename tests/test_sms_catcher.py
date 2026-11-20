"""Tests for ``cfg discover-phone --via``: a fleet router as the SMS catcher.

No network: inbox reads, SMS sends and TOS writes are all stubbed.
"""

from __future__ import annotations

import argparse
from typing import List
from unittest.mock import patch

import pytest

from receivers.cfg import sms_catcher
from receivers.cfg.sms_catcher import (
    CatcherError,
    InboxMessage,
    find_sender,
    parse_gsmctl_inbox,
    resolve_catcher,
    wait_for_sender,
)

GSMCTL_LISTING = """\
Index:          0
Date:           Wed Nov 18 23:56:58 2026
Sender:         +3548000001
Status:         read
Text:           SKOG 10.4.1.231 GPS SIM MSISDN discovery 2026-11-18
------------------------------
Index:          1
Date:           Fri Nov 20 11:02:10 2026
Sender:         +3548000002
Status:         unread
Missing:        0
Total parts:    2
Text:           MJSK 10.6.2.14 GPS SIM MSISDN discovery
2026-11-20 #a1b2c3
------------------------------
"""


# --- parse_gsmctl_inbox ---------------------------------------------------


def test_parses_gsmctl_blocks_including_multiline_text():
    msgs = parse_gsmctl_inbox(GSMCTL_LISTING)
    assert [m.sender for m in msgs] == ["+3548000001", "+3548000002"]
    assert msgs[0].content == "SKOG 10.4.1.231 GPS SIM MSISDN discovery 2026-11-18"
    # the body's second line is kept, the dashed separator is not
    assert msgs[1].content.endswith("2026-11-20 #a1b2c3")
    assert "---" not in msgs[1].content


def test_parses_empty_listing():
    assert parse_gsmctl_inbox("") == []


# --- find_sender ----------------------------------------------------------


def test_find_sender_matches_token_not_newest():
    msgs = [
        InboxMessage("+3548000001", "MJSK discovery #ffffff"),  # newest, stale run
        InboxMessage("+3548000002", "MJSK discovery #a1b2c3"),
    ]
    assert find_sender(msgs, "#a1b2c3") == "+3548000002"


def test_find_sender_none_when_absent():
    assert find_sender([InboxMessage("+354800", "other")], "#a1b2c3") is None


def test_find_sender_refuses_conflicting_senders():
    msgs = [
        InboxMessage("+3548000001", "x #a1b2c3"),
        InboxMessage("+3548000002", "y #a1b2c3"),
    ]
    with pytest.raises(CatcherError, match="several senders"):
        find_sender(msgs, "#a1b2c3")


# --- wait_for_sender ------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_wait_polls_until_token_arrives():
    clock = _Clock()
    inboxes: List[List[InboxMessage]] = [
        [InboxMessage("+354800", "old #000000")],
        [InboxMessage("+354800", "old #000000"), InboxMessage("+3548000002", "x #abc")],
    ]

    def reader(_host: str) -> List[InboxMessage]:
        return inboxes.pop(0)

    got = wait_for_sender(
        "h",
        "#abc",
        timeout=60,
        poll=10,
        reader=reader,
        sleep=clock.sleep,
        clock=clock.now,
    )
    assert got == "+3548000002"
    assert clock.t == 10


def test_wait_times_out_with_none():
    clock = _Clock()
    got = wait_for_sender(
        "h",
        "#abc",
        timeout=30,
        poll=10,
        reader=lambda _h: [],
        sleep=clock.sleep,
        clock=clock.now,
    )
    assert got is None
    assert clock.t == 30


def test_wait_retries_a_failed_read():
    clock = _Clock()
    calls = {"n": 0}

    def reader(_h: str) -> List[InboxMessage]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CatcherError("ssh hiccup")
        return [InboxMessage("+3548000002", "x #abc")]

    assert (
        wait_for_sender(
            "h",
            "#abc",
            timeout=60,
            poll=5,
            reader=reader,
            sleep=clock.sleep,
            clock=clock.now,
        )
        == "+3548000002"
    )


# --- resolve_catcher ------------------------------------------------------


def test_marker_resolves_router_and_tos_number():
    c = resolve_catcher(
        "vfls",
        lookup_router_ip=lambda m: {"VFLS": "10.6.1.211"}.get(m),
        lookup_sim_phone=lambda m: {"VFLS": "+3548000099"}.get(m),
    )
    assert (c.host, c.number, c.label) == ("10.6.1.211", "+3548000099", "VFLS")


def test_explicit_number_overrides_tos():
    c = resolve_catcher(
        "VFLS",
        number="+3540000000",
        lookup_router_ip=lambda m: "10.6.1.211",
        lookup_sim_phone=lambda m: pytest.fail("TOS must not be asked"),
    )
    assert c.number == "+3540000000"


def test_marker_without_tos_number_is_refused():
    with pytest.raises(CatcherError, match="no phone_number in TOS"):
        resolve_catcher(
            "VFLS",
            lookup_router_ip=lambda m: "10.6.1.211",
            lookup_sim_phone=lambda m: None,
        )


def test_host_needs_explicit_number():
    with pytest.raises(CatcherError, match="pass the catcher SIM's number"):
        resolve_catcher("10.6.1.211")
    assert resolve_catcher("10.6.1.211", number="+3548000099").host == "10.6.1.211"


def test_marker_without_router_is_refused():
    with pytest.raises(CatcherError, match="no router_ip"):
        resolve_catcher("XXXX", lookup_router_ip=lambda m: None)


# --- CLI wiring: cmd_cfg_discover_phone --via --record ---------------------


def _disc_args(**kw) -> argparse.Namespace:
    base = dict(
        host="10.6.2.14",
        station="MJSK",
        to=None,
        via="VFLS",
        wait=30.0,
        record=True,
        message=None,
        ussd=None,
        password=None,
        no_dry_run=True,
        router_type="teltonika",
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _catcher():
    return sms_catcher.Catcher(host="10.6.1.211", number="+3548000099", label="VFLS")


def test_cli_via_sends_to_catcher_and_records_sender():
    from receivers.cli import cfg as cli

    sent = {}

    def fake_send(host, to, message, **kw):
        sent.update(host=host, to=to, message=message)
        return {"cmd": "gsmctl", "sent": True}

    with (
        patch("receivers.cfg.sms_catcher.resolve_catcher", return_value=_catcher()),
        patch("receivers.cfg.sms_catcher.make_token", return_value="#a1b2c3"),
        patch("receivers.cfg.telemetry_probe.send_sms_ssh", side_effect=fake_send),
        patch(
            "receivers.cfg.sms_catcher.wait_for_sender", return_value="+3548000002"
        ) as wait,
        patch("receivers.cfg.operations.set_device_attribute") as setattr_,
        patch.object(cli, "_print_result_summary"),
    ):
        rc = cli.cmd_cfg_discover_phone(_disc_args())
    assert rc == 0
    assert sent["to"] == "+3548000099"
    assert sent["message"].endswith("#a1b2c3")
    assert wait.call_args.args[:2] == ("10.6.1.211", "#a1b2c3")
    setattr_.assert_called_once()
    assert setattr_.call_args.args == ("MJSK",)
    assert setattr_.call_args.kwargs["code"] == "phone_number"
    assert setattr_.call_args.kwargs["value"] == "+3548000002"
    assert setattr_.call_args.kwargs["dry_run"] is False


def test_cli_via_dry_run_sends_nothing_and_writes_nothing():
    from receivers.cli import cfg as cli

    with (
        patch("receivers.cfg.sms_catcher.resolve_catcher", return_value=_catcher()),
        patch(
            "receivers.cfg.telemetry_probe.send_sms_ssh",
            return_value={"cmd": "gsmctl", "sent": False},
        ) as send,
        patch("receivers.cfg.sms_catcher.wait_for_sender") as wait,
        patch("receivers.cfg.operations.set_device_attribute") as setattr_,
    ):
        rc = cli.cmd_cfg_discover_phone(_disc_args(no_dry_run=False))
    assert rc == 0
    assert send.call_args.kwargs["dry_run"] is True
    wait.assert_not_called()
    setattr_.assert_not_called()


def test_cli_via_timeout_records_nothing_and_fails():
    from receivers.cli import cfg as cli

    with (
        patch("receivers.cfg.sms_catcher.resolve_catcher", return_value=_catcher()),
        patch(
            "receivers.cfg.telemetry_probe.send_sms_ssh",
            return_value={"cmd": "gsmctl", "sent": True},
        ),
        patch("receivers.cfg.sms_catcher.wait_for_sender", return_value=None),
        patch("receivers.cfg.operations.set_device_attribute") as setattr_,
    ):
        rc = cli.cmd_cfg_discover_phone(_disc_args())
    assert rc == 1
    setattr_.assert_not_called()


def test_cli_record_without_via_is_refused():
    from receivers.cli import cfg as cli

    with patch("receivers.cfg.telemetry_probe.send_sms_ssh") as send:
        rc = cli.cmd_cfg_discover_phone(_disc_args(via=None, to="+3548000000"))
    assert rc == 2
    send.assert_not_called()


def test_parser_registers_via_flags():
    from receivers.cli import cfg as cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    cli._add_discover_phone_parser(sub)
    cli._add_replace_sim_parser(sub)
    a = parser.parse_args(
        ["discover-phone", "--station", "MJSK", "--via", "VFLS", "--record"]
    )
    assert (a.via, a.record, a.wait) == ("VFLS", True, 180.0)
    b = parser.parse_args(
        [
            "replace-sim",
            "--station",
            "MJSK",
            "--discover-phone",
            "--discover-phone-via",
            "VFLS",
        ]
    )
    assert b.discover_phone_via == "VFLS"


# --- replace-sim --discover-phone-via ---------------------------------------


def test_replace_sim_discover_via_returns_caught_sender_without_prompt():
    from receivers.cli import cfg as cli

    args = argparse.Namespace(
        station="MJSK",
        date=None,
        probe="10.6.2.14",
        discover_phone_from=None,
        discover_phone_to=None,
        discover_phone_via="VFLS",
        username=None,
        password=None,
    )
    with (
        patch("receivers.cfg.sms_catcher.resolve_catcher", return_value=_catcher()),
        patch("receivers.cfg.sms_catcher.make_token", return_value="#a1b2c3"),
        patch("receivers.cfg.telemetry_probe.send_sms") as send,
        patch("receivers.cfg.sms_catcher.wait_for_sender", return_value="+3548000002"),
        patch("builtins.input", side_effect=AssertionError("must not prompt")),
    ):
        got = cli._discover_phone(args, dry_run=False)
    assert got == "+3548000002"
    assert send.call_args.args[1] == "+3548000099"
    assert send.call_args.args[2].endswith("#a1b2c3")


# --- operations.station_device_attribute ------------------------------------


class _FakeWriter:
    def __init__(self, rows):
        self.rows = rows

    def get_attribute_values(self, device_id, code):
        assert (device_id, code) == (21837, "phone_number")
        return self.rows


def test_station_device_attribute_reads_open_value():
    from receivers.cfg import operations as ops

    w = _FakeWriter(
        [
            {
                "value": "+3540000000",
                "date_from": "2025-01-01",
                "date_to": "2026-01-01",
            },
            {"value": "+3548000099", "date_from": "2026-09-30", "date_to": None},
        ]
    )
    with (
        patch.object(ops, "_resolve_station", return_value=4413),
        patch.object(ops, "_find_open_child", return_value=21837),
    ):
        got = ops.station_device_attribute(
            "VFLS", subtype="sim_card", code="phone_number", writer=w
        )
    assert got == "+3548000099"


def test_station_device_attribute_none_without_device():
    from receivers.cfg import operations as ops

    with (
        patch.object(ops, "_resolve_station", return_value=4413),
        patch.object(ops, "_find_open_child", return_value=None),
    ):
        assert (
            ops.station_device_attribute(
                "VFLS", subtype="sim_card", code="phone_number", writer=_FakeWriter([])
            )
            is None
        )
