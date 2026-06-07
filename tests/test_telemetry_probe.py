"""Tests for receivers.cfg.telemetry_probe — Teltonika RutOS REST probe.

HTTP is mocked with the real response shapes captured live from a RUT241
(fw RUT2M_R_00.07.22.3) on 2026-06-06, so these tests double as a regression
guard on the verified field map. No network required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.telemetry_probe import (
    ProbeAuthError,
    ProbeCredentialsError,
    ProbeUnreachableError,
    _conntype_to_subtype,
    _format_mac,
    _lan_ip,
    _wan_ip,
    probe_teltonika,
)

# --- Real captured payloads (trimmed to the fields the probe reads) ---------

_DEVICE_STATUS = {
    "success": True,
    "data": {
        "mnfinfo": {
            "serial": "6000544107",
            "mac": "2097270A7726",
            "macEth": "2097270A7727",
            "name": "RUT24103XXXX",
            "hwver": "0002",
        },
        "static": {
            "device_name": "RUT241",
            "model": "Teltonika RUT2M",
            "fw_version": "RUT2M_R_00.07.22.3",
        },
        "ports": [
            {"name": "WAN", "mac": "20:97:27:0A:77:27"},
            {"name": "LAN", "mac": "20:97:27:0A:77:26"},
        ],
    },
}

_MODEMS_STATUS = {
    "success": True,
    "data": [
        {
            "iccid": "89354010260102025676",
            "imsi": "274012011380378",
            "imei": "864677069907244",
            "operator": "Siminn",
            "conntype": "4G (LTE)",
            "simstate": "Inserted",
        }
    ],
}

_INTERFACES_STATUS = {
    "success": True,
    "data": [
        {"name": "lan", "ipv4-address": [{"address": "192.168.100.1"}], "route": []},
        {"name": "loopback", "ipv4-address": [{"address": "127.0.0.1"}], "route": []},
        {
            "name": "mob1s1a1_4",
            "ipv4-address": [{"address": "10.6.1.228"}],
            "route": [{"target": "0.0.0.0", "nexthop": "0.0.0.0"}],
        },
    ],
}


def _fake_session():
    """Return a MagicMock requests.Session wired with the captured responses."""

    def _resp(status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        return r

    sess = MagicMock()
    sess.post.return_value = _resp(
        200, {"success": True, "data": {"token": "tok123", "expires": 299}}
    )

    def _get(url, **_kw):
        if url.endswith("/api/system/device/status"):
            return _resp(200, _DEVICE_STATUS)
        if url.endswith("/api/modems/status"):
            return _resp(200, _MODEMS_STATUS)
        if url.endswith("/api/interfaces/status"):
            return _resp(200, _INTERFACES_STATUS)
        return _resp(404, {})

    sess.get.side_effect = _get
    return sess


def _patch(sess):
    """Patch requests.Session + credential resolution for a probe call."""
    import receivers.cfg.telemetry_probe as tp

    return (
        patch.multiple(
            "receivers.cfg.telemetry_probe",
            resolve_credentials=lambda **_k: ("admin", "secret"),
        ),
        patch("requests.Session", return_value=sess),
        patch.object(tp, "logger", MagicMock()),
    )


# --- pure helpers -----------------------------------------------------------


def test_conntype_to_subtype():
    assert _conntype_to_subtype("4G (LTE)") == "4G"
    assert _conntype_to_subtype("3G") == "3G"
    assert _conntype_to_subtype("5G-NSA") == "5G-NSA"
    assert _conntype_to_subtype(None) is None
    assert _conntype_to_subtype("") is None


def test_format_mac():
    assert _format_mac("2097270A7727") == "20:97:27:0A:77:27"
    assert _format_mac(None) is None
    assert _format_mac("bogus") == "bogus"  # unexpected length → unchanged


def test_wan_ip_picks_default_route():
    assert _wan_ip(_INTERFACES_STATUS["data"]) == "10.6.1.228"


def test_wan_ip_no_fallback_without_default_route():
    # Must NOT return a non-default address (would wrongly grab the LAN IP).
    data = [
        {"name": "lan", "ipv4-address": [{"address": "192.168.100.1"}], "route": []},
    ]
    assert _wan_ip(data) is None


def test_wan_ip_none_when_empty():
    assert _wan_ip([]) is None
    assert _wan_ip(None) is None


def test_lan_ip_picks_lan_interface():
    assert _lan_ip(_INTERFACES_STATUS["data"]) == "192.168.100.1"


def test_lan_ip_none_when_no_lan():
    data = [
        {
            "name": "mob1s1a1_4",
            "ipv4-address": [{"address": "10.6.1.228"}],
            "route": [{"target": "0.0.0.0"}],
        },
    ]
    assert _lan_ip(data) is None
    assert _lan_ip([]) is None
    assert _lan_ip(None) is None


# --- full probe (mocked HTTP) ----------------------------------------------


def test_probe_teltonika_full_identity():
    sess = _fake_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        ident = probe_teltonika("10.6.1.228")
    # Router identity
    assert ident.router_serial == "6000544107"
    assert ident.router_model == "RUT241"
    assert ident.router_manufacturer == "Teltonika"
    assert ident.router_mac == "20:97:27:0A:77:27"  # WAN port, colon-formatted
    assert ident.router_firmware == "RUT2M_R_00.07.22.3"
    assert ident.modem_subtype == "4G"
    assert ident.router_lan_ip == "192.168.100.1"  # LAN IP → modem, not WAN
    # SIM
    assert ident.sim_iccid == "89354010260102025676"
    assert ident.sim_ip_address == "10.6.1.228"  # mobile WAN IP → SIM
    assert ident.provider == "Siminn"
    assert ident.imsi == "274012011380378"
    assert ident.imei == "864677069907244"


def test_probe_login_sends_credentials():
    sess = _fake_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        probe_teltonika("10.6.1.228")
    # POST /api/login called with the resolved credentials as JSON
    login_call = sess.post.call_args
    assert login_call.args[0].endswith("/api/login")
    assert login_call.kwargs["json"] == {"username": "admin", "password": "secret"}


def test_probe_no_credentials_raises():
    with patch(
        "receivers.cfg.telemetry_probe.resolve_credentials",
        lambda **_k: (None, None),
    ):
        with pytest.raises(ProbeCredentialsError):
            probe_teltonika("10.6.1.228")


def test_probe_auth_rejected_raises():
    sess = _fake_session()
    rej = MagicMock()
    rej.status_code = 403
    sess.post.return_value = rej
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        with pytest.raises(ProbeAuthError):
            probe_teltonika("10.6.1.228")


def test_probe_unreachable_raises():
    import requests

    sess = _fake_session()
    sess.post.side_effect = requests.exceptions.ConnectionError("no route")
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        with pytest.raises(ProbeUnreachableError):
            probe_teltonika("10.6.1.228")


def test_probe_degrades_on_missing_endpoint():
    """A 404 on one status endpoint leaves those fields None, not a hard fail."""
    sess = _fake_session()

    def _get_no_modems(url, **_kw):
        r = MagicMock()
        if url.endswith("/api/modems/status"):
            r.status_code = 404
            r.json.return_value = {}
        elif url.endswith("/api/system/device/status"):
            r.status_code = 200
            r.json.return_value = _DEVICE_STATUS
        elif url.endswith("/api/interfaces/status"):
            r.status_code = 200
            r.json.return_value = _INTERFACES_STATUS
        else:
            r.status_code = 404
            r.json.return_value = {}
        return r

    sess.get.side_effect = _get_no_modems
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        ident = probe_teltonika("10.6.1.228")
    # Router still resolved; SIM fields degraded to None
    assert ident.router_serial == "6000544107"
    assert ident.sim_iccid is None
    assert ident.provider is None
    # WAN IP still works (separate endpoint)
    assert ident.sim_ip_address == "10.6.1.228"


# --- send_sms (MSISDN-discovery) -------------------------------------------


def test_send_sms_dry_run_sends_nothing():
    """Dry-run returns the planned payload and never opens a session."""
    from receivers.cfg.telemetry_probe import send_sms

    # No patching of Session/creds: if dry_run tried to connect it would fail.
    plan = send_sms("10.6.1.228", "8400754", "GSIG check", dry_run=True)
    assert plan["dry_run"] is True
    assert plan["sent"] is False
    assert plan["to"] == "8400754"
    assert plan["endpoint"] == "/api/messages/actions/send"


def test_send_sms_live_posts_expected_payload():
    from receivers.cfg.telemetry_probe import send_sms

    sess = _fake_session()
    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {"success": True}
    sess.post.return_value = MagicMock(  # login first, then send — use side_effect
        status_code=200, json=lambda: {"success": True, "data": {"token": "t"}}
    )

    posts = []

    def _post(url, **kw):
        posts.append((url, kw))
        r = MagicMock()
        r.status_code = 200
        if url.endswith("/api/login"):
            r.json.return_value = {"success": True, "data": {"token": "t"}}
        else:
            r.json.return_value = {"success": True}
        return r

    sess.post.side_effect = _post
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        res = send_sms("10.6.1.228", "8400754", "GSIG check", dry_run=False)
    assert res["sent"] is True
    # Find the send POST (not the login)
    send_call = next(c for c in posts if c[0].endswith("/api/messages/actions/send"))
    body = send_call[1]["json"]
    assert body["data"]["number"] == "8400754"
    assert body["data"]["message"] == "GSIG check"


def test_send_sms_rejected_raises():
    from receivers.cfg.telemetry_probe import ProbeError, send_sms

    sess = _fake_session()

    def _post(url, **kw):
        r = MagicMock()
        if url.endswith("/api/login"):
            r.status_code = 200
            r.json.return_value = {"success": True, "data": {"token": "t"}}
        else:
            r.status_code = 500
            r.text = "modem busy"
        return r

    sess.post.side_effect = _post
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        with pytest.raises(ProbeError):
            send_sms("10.6.1.228", "8400754", "x", dry_run=False)


# --- port forwards (ensure_port_forwards / list_port_forwards) --------------

_PORT_FORWARDS = {
    "success": True,
    "data": [
        {
            "name": "GPS_http",
            "src": "wan",
            "src_dport": "8060",
            "dest": "lan",
            "dest_ip": "192.168.100.60",
            "dest_port": "80",
            "proto": ["tcp"],
            "enabled": "1",
            ".type": "redirect",
        }
    ],
}


def _pf_session():
    """Session mock: login + port_forwards GET/POST + changes apply."""
    sess = MagicMock()
    posts = []

    def _post(url, **kw):
        posts.append((url, kw.get("json")))
        r = MagicMock()
        r.status_code = 200
        if url.endswith("/api/login"):
            r.json.return_value = {"success": True, "data": {"token": "t"}}
        else:
            r.json.return_value = {"success": True}
        return r

    def _get(url, **kw):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = _PORT_FORWARDS
        return r

    sess.post.side_effect = _post
    sess.get.side_effect = _get
    sess._posts = posts
    return sess


def test_list_port_forwards():
    from receivers.cfg.telemetry_probe import list_port_forwards

    sess = _pf_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        fws = list_port_forwards("10.6.1.228")
    assert [f["name"] for f in fws] == ["GPS_http"]


def test_ensure_port_forwards_dry_run_no_write():
    from receivers.cfg.telemetry_probe import ensure_port_forwards

    sess = _pf_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        res = ensure_port_forwards(
            "10.6.1.228",
            "192.168.100.60",
            [
                {"name": "GPS_control", "src_dport": "28784"},
                {"name": "GPS_ftp", "src_dport": "2160"},
                {"name": "GPS_http", "src_dport": "8060", "dest_port": "80"},
            ],
            dry_run=True,
        )
    assert res["dry_run"] is True
    assert res["applied"] is False
    assert "GPS_http" in res["skipped"]  # already present (same dport+dest_ip)
    created_names = [w["name"] for w in res["would_create"]]
    assert created_names == ["GPS_control", "GPS_ftp"]
    # dry-run must not POST anything
    assert not [u for u, _ in sess._posts if u.endswith("/port_forwards/config")]


def test_ensure_port_forwards_live_creates_and_applies():
    from receivers.cfg.telemetry_probe import ensure_port_forwards

    sess = _pf_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        res = ensure_port_forwards(
            "10.6.1.228",
            "192.168.100.60",
            [
                {"name": "GPS_control", "src_dport": "28784"},
                {"name": "GPS_http", "src_dport": "8060", "dest_port": "80"},
            ],
            dry_run=False,
        )
    assert res["created"] == ["GPS_control"]  # http already present, skipped
    assert res["applied"] is True
    # exactly one create POST + one apply POST
    create_posts = [b for u, b in sess._posts if u.endswith("/port_forwards/config")]
    apply_posts = [u for u, _ in sess._posts if u.endswith("/port_forwards/changes")]
    assert len(create_posts) == 1
    assert create_posts[0]["data"]["src_dport"] == "28784"
    assert create_posts[0]["data"]["dest_ip"] == "192.168.100.60"
    assert len(apply_posts) == 1


def test_ensure_port_forwards_all_present_no_apply():
    from receivers.cfg.telemetry_probe import ensure_port_forwards

    sess = _pf_session()
    cm_creds, cm_sess, cm_log = _patch(sess)
    with cm_creds, cm_sess, cm_log:
        res = ensure_port_forwards(
            "10.6.1.228",
            "192.168.100.60",
            [{"name": "GPS_http", "src_dport": "8060", "dest_port": "80"}],
            dry_run=False,
        )
    # nothing to create → no apply call
    assert res["created"] == []
    assert res["applied"] is False
    assert not [u for u, _ in sess._posts if u.endswith("/port_forwards/changes")]
