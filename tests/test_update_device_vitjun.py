"""Tests for cfg update-device's auto-vitjun helper (_create_update_vitjun).

A firmware/marker --change is a maintenance event, so update-device drops a
Fjarvitjun (remote, by default) on the device's station — mirroring the vitjun
replace-modem/replace-receiver write for a hardware swap.
"""

from __future__ import annotations

from types import SimpleNamespace

from receivers.cli.cfg import _create_update_vitjun


def _args(visit_type="remote", reason="change", work=None, participants="bgo@vedur.is"):
    return SimpleNamespace(
        visit_type=visit_type, reason=reason, work=work, participants=participants
    )


class FakeW:
    def __init__(self, parent=16090, sub="GPS stöð", fail_times=0):
        self.parent = parent
        self.sub = sub
        self.fail_times = fail_times
        self.calls = []

    def get_open_parent_join(self, eid):
        return {"id_entity_parent": self.parent}

    def get_entity_history(self, eid):
        return {"code_entity_subtype": self.sub}

    def add_maintenance_visit(self, eid, **kw):
        self.calls.append((eid, kw))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("401 invalid token")
        return {"id_maintenance": 9999}


_FV = {"firmware_version": "5.7.0"}


def test_creates_remote_vitjun_on_station():
    w = FakeW()
    _create_update_vitjun(
        w,
        20423,
        ["firmware_version"],
        _FV,
        "2026-06-08T00:00:00",
        "Fjarvitjun",
        _args(),
    )
    eid, kw = w.calls[-1]
    assert eid == 16090  # the station, not the device
    assert kw["maintenance_type"] == "remote"
    assert kw["reasons"] == ["change"]
    assert "5.7.0" in kw["work"]
    assert kw["participants"] == "bgo@vedur.is"


def test_onsite_maps_to_on_site():
    w = FakeW()
    _create_update_vitjun(
        w,
        20423,
        ["firmware_version"],
        _FV,
        "d",
        "Staðarvitjun",
        _args(visit_type="onsite"),
    )
    assert w.calls[-1][1]["maintenance_type"] == "on_site"


def test_skips_when_warehoused(capsys):
    w = FakeW(sub="Lager")
    _create_update_vitjun(
        w, 20423, ["firmware_version"], _FV, "d", "Fjarvitjun", _args()
    )
    assert w.calls == []  # no vitjun on a warehouse
    out = capsys.readouterr()
    assert "warehouse" in (out.out + out.err).lower()


def test_skips_when_no_open_parent(capsys):
    w = FakeW(parent=None)
    _create_update_vitjun(
        w, 20423, ["firmware_version"], _FV, "d", "Fjarvitjun", _args()
    )
    assert w.calls == []


def test_retries_once_on_intermittent_401():
    w = FakeW(fail_times=1)
    _create_update_vitjun(
        w, 20423, ["firmware_version"], _FV, "d", "Fjarvitjun", _args()
    )
    assert len(w.calls) == 2  # failed once, retried, succeeded


def test_custom_work_text_overrides():
    w = FakeW()
    _create_update_vitjun(
        w, 20423, ["firmware_version"], _FV, "d", "Fjarvitjun", _args(work="Endurræsti")
    )
    assert w.calls[-1][1]["work"] == "Endurræsti"
