"""Tests for cfg.operations.correct_date — Pattern 4 bulk date correction.

A fake TOSWriter serves a synthetic modem-swap topology with boundaries at the
'wrong' instant (2026-06-08T12:00:00); correct_date must find exactly those —
joins, attributes (incl. a datetime `value`), warehouse-return join, and the
swap vitjun — and shift them to 2026-06-04T12:00:00, leaving unrelated same-day
boundaries untouched.
"""

from __future__ import annotations

import re

import pytest

from receivers.cfg.operations import CfgOperationError, correct_date

FROM = "2026-06-08T12:00:00"
TO = "2026-06-04T12:00:00"
OTHER = "2026-06-08T08:00:00"  # same day, different instant — must NOT match


def _tos_date(dt):
    if dt is None:
        return None
    dt = re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", dt)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
        dt = f"{dt}T00:00:00"
    return dt


# Synthetic topology: station 100, old modem 300, new modem 301, antenna 400.
_HISTORY = {
    100: {
        "code_entity_subtype": "gps_station",
        "attributes": [
            {
                "id_attribute_value": 1,
                "code": "lat",
                "value": "64.1",
                "date_from": "2014-01-01T00:00:00",
                "date_to": None,
            },
        ],
        "children_connections": [
            {
                "id_entity_connection": 200,
                "id_entity_child": 300,
                "time_from": "2024-01-01T00:00:00",
                "time_to": FROM,
            },
            {
                "id_entity_connection": 201,
                "id_entity_child": 301,
                "time_from": FROM,
                "time_to": "",
            },
            {
                "id_entity_connection": 202,
                "id_entity_child": 400,
                "time_from": "2020-01-01T00:00:00",
                "time_to": "",
            },
        ],
    },
    300: {  # old modem — status close + bilað open at FROM
        "code_entity_subtype": "modem_gsm",
        "attributes": [
            {
                "id_attribute_value": 10,
                "code": "status",
                "value": "virkt",
                "date_from": "2024-01-01T00:00:00",
                "date_to": FROM,
            },
            {
                "id_attribute_value": 11,
                "code": "status",
                "value": "bilað",
                "date_from": FROM,
                "date_to": None,
            },
        ],
        "children_connections": [],
    },
    301: {  # new modem — serial + date_start (value AND date_from) at FROM
        "code_entity_subtype": "modem_gsm",
        "attributes": [
            {
                "id_attribute_value": 20,
                "code": "serial_number",
                "value": "X1",
                "date_from": FROM,
                "date_to": None,
            },
            {
                "id_attribute_value": 21,
                "code": "date_start",
                "value": FROM,
                "date_from": FROM,
                "date_to": None,
            },
            {
                "id_attribute_value": 22,
                "code": "ip_address",
                "value": "10.0.0.1",
                "date_from": OTHER,
                "date_to": None,
            },  # different instant — skip
        ],
        "children_connections": [],
    },
    400: {  # antenna — no FROM boundaries
        "code_entity_subtype": "antenna",
        "attributes": [
            {
                "id_attribute_value": 30,
                "code": "serial_number",
                "value": "A1",
                "date_from": "2020-01-01T00:00:00",
                "date_to": None,
            },
        ],
        "children_connections": [],
    },
}

# parent_history per entity (catches warehouse-return join 203 on old modem 300)
_PARENT_HISTORY = {
    300: [
        {
            "id": 200,
            "id_entity_parent": 100,
            "time_from": "2024-01-01T00:00:00",
            "time_to": FROM,
        },
        {
            "id": 203,
            "id_entity_parent": 4,
            "time_from": FROM,
            "time_to": "",
        },  # → warehouse
    ],
    301: [{"id": 201, "id_entity_parent": 100, "time_from": FROM, "time_to": ""}],
    400: [
        {
            "id": 202,
            "id_entity_parent": 100,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "",
        }
    ],
}

_VITJUNS = [
    {"id": 500, "start_time": FROM, "end_time": FROM, "participants": "bgo@vedur.is"},
    {"id": 501, "start_time": "2025-01-01T12:00:00", "end_time": "2025-01-01T12:00:00"},
]


class FakeWriter:
    def __init__(self):
        self.patched_conns = []
        self.patched_attrs = []
        self.patched_visits = []

    @staticmethod
    def _tos_date(dt):
        return _tos_date(dt)

    def find_station_by_marker(self, marker, type_filter="stöð"):
        return 100

    def get_entity_history(self, eid):
        return _HISTORY.get(eid, {})

    def _request(self, method, path):
        m = re.search(r"/parent_history/(\d+)", path)
        return _PARENT_HISTORY.get(int(m.group(1)), []) if m else []

    def list_maintenance_visits(self, eid):
        return _VITJUNS

    def patch_entity_connection(self, cid, **kw):
        self.patched_conns.append((cid, kw))

    def patch_attribute_value(self, aid, **kw):
        self.patched_attrs.append((aid, kw))

    def update_maintenance_visit(self, vid, **kw):
        self.patched_visits.append((vid, kw))


def test_correct_date_finds_all_boundaries_and_plans_shift():
    w = FakeWriter()
    res = correct_date("ROTH", "2026-06-08", "2026-06-04", writer=w, dry_run=True)
    assert res.tos_changes["from"] == FROM
    assert res.tos_changes["to"] == TO

    conn_ids = {cid for cid, _ in w.patched_conns}
    attr_ids = {aid for aid, _ in w.patched_attrs}
    visit_ids = {vid for vid, _ in w.patched_visits}

    # 3 joins: station-close 200, station-open 201, warehouse-open 203
    assert conn_ids == {200, 201, 203}
    # 4 attrs: old status close(10), old bilað open(11), new serial(20), new date_start(21)
    assert attr_ids == {10, 11, 20, 21}
    # 1 vitjun (500); the old 2025 vitjun 501 untouched
    assert visit_ids == {500}


def test_unrelated_same_day_instant_not_touched():
    w = FakeWriter()
    correct_date("ROTH", "2026-06-08", "2026-06-04", writer=w, dry_run=True)
    # attr 22 (ip_address at 08:00, not noon) and antenna attr 30 must be absent
    assert 22 not in {aid for aid, _ in w.patched_attrs}
    assert 30 not in {aid for aid, _ in w.patched_attrs}
    assert 202 not in {cid for cid, _ in w.patched_conns}  # antenna join


def test_date_start_value_and_date_from_both_shifted():
    w = FakeWriter()
    correct_date("ROTH", "2026-06-08", "2026-06-04", writer=w, dry_run=True)
    ds = dict(w.patched_attrs)[21]
    assert ds.get("value") == TO
    assert ds.get("date_from") == TO


def test_close_open_use_right_field():
    w = FakeWriter()
    correct_date("ROTH", "2026-06-08", "2026-06-04", writer=w, dry_run=True)
    conns = dict(w.patched_conns)
    assert conns[200] == {"time_to": TO}  # old modem leaves station
    assert conns[201] == {"time_from": TO}  # new modem joins station
    assert conns[203] == {"time_from": TO}  # old modem enters warehouse


def test_same_from_to_rejected():
    w = FakeWriter()
    with pytest.raises(CfgOperationError):
        correct_date("ROTH", "2026-06-08", "2026-06-08", writer=w, dry_run=True)
