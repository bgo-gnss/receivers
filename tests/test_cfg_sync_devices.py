"""Regression tests for `cfg reconcile --sync-devices` device creation.

Why this file exists
--------------------
`_sync_devices_to_tos` used to call::

    build_required_attributes(serial=…, model=…, owner=…, location=station_id, …)

but `build_required_attributes` is declared as ``(serial, model, owner,
date_start)`` — there is **no `location` parameter**. Every call therefore
raised ``TypeError``, which the function's own broad ``except Exception``
swallowed and reported as "device creation failed". The net effect was that
``receivers cfg reconcile --sync-devices`` **never created a single device**,
printing a failure the operator could easily read past.

The bug survived because the only existing coverage
(``test_reconcile_output_golden.py``) monkeypatches ``_sync_devices_to_tos``
ITSELF, in order to test the ``--yes``/``--dry-run`` consent gate. That never
executes the function body.

So these tests deliberately do the opposite: they run the REAL body, and they
keep ``build_required_attributes`` / ``validate_model`` REAL (unpatched), so a
signature mismatch cannot hide. Only the network boundary (TOSWriter) and the
"does this entity already exist" lookup are faked.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional, Tuple

import pytest

from receivers.cli.cfg import _sync_devices_to_tos

# ---------------------------------------------------------------------------
# Fakes — only the network boundary
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Stands in for TOSWriter. Records create_device calls."""

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.created: List[Tuple[str, List[Dict[str, Any]]]] = []

    def find_device_by_serial(self, subtype, serial):  # noqa: ANN001
        return None  # nothing pre-exists → proceed to create

    def create_device(self, entity_subtype, attributes):  # noqa: ANN001
        self.created.append((entity_subtype, attributes))
        return {"id_entity": 9001}


@pytest.fixture
def patched(monkeypatch):
    """Patch TOSWriter + the existence lookup. Keep tostools helpers REAL."""
    import tostools.api.tos_writer as tw

    import receivers.cfg.tos_push as tp

    writer = _FakeWriter()

    def _factory(*_a, **_k):
        return writer

    monkeypatch.setattr(tw, "TOSWriter", _factory)
    # resolve_entity_id returns the id of an EXISTING child, or None.
    monkeypatch.setattr(tp, "resolve_entity_id", lambda *a, **k: None)
    return writer


def _args(*, dry_run: bool = True) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run, effective_date="2026-10-11")


_STATION_CFG: Dict[str, Any] = {
    "receiver_type": "PolaRX5",
    "receiver_serial": "3093129",
}
_IDENTITY: Dict[str, Any] = {
    "receiver_type": "PolaRX5",
    "receiver_serial": "3093129",
}
_TOS_DATA: Dict[str, Any] = {"id_entity": 4253}


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


class TestSyncDevicesCreates:
    def test_creates_receiver_with_real_attribute_builder(self, patched):
        """REGRESSION (the `location=` TypeError).

        Real `build_required_attributes` is on the path here, so the old
        `location=station_id` argument makes this raise inside the body — the
        function swallows it and returns 0, failing the assertion below.
        """
        created = _sync_devices_to_tos(
            station_id="HAMR",
            station_config=_STATION_CFG,
            receiver_identity=_IDENTITY,
            tos_data=_TOS_DATA,
            args=_args(),
            silent=True,
        )
        assert created == 1, "expected one device created"
        subtype, attributes = patched.created[0]
        assert subtype == "gnss_receiver"
        # The attribute list is the real canonical set, not a stub.
        codes = {a["code"] for a in attributes}
        assert {"serial_number", "model", "owner", "date_start"} <= codes

    def test_no_location_attribute_is_invented(self, patched):
        """The fix must not smuggle `location` in as an attribute instead.

        Location is expressed by the device's JOIN, never by an attribute, so
        the builder's output must not contain a location-ish code.
        """
        _sync_devices_to_tos(
            station_id="HAMR",
            station_config=_STATION_CFG,
            receiver_identity=_IDENTITY,
            tos_data=_TOS_DATA,
            args=_args(),
            silent=True,
        )
        _subtype, attributes = patched.created[0]
        assert not any("location" in a["code"].lower() for a in attributes)


class TestSyncDevicesSkips:
    def test_no_tos_data_returns_zero(self, patched):
        assert (
            _sync_devices_to_tos(
                station_id="HAMR",
                station_config=_STATION_CFG,
                receiver_identity=_IDENTITY,
                tos_data=None,
                args=_args(),
                silent=True,
            )
            == 0
        )
        assert patched.created == []

    def test_no_receiver_identity_returns_zero(self, patched):
        assert (
            _sync_devices_to_tos(
                station_id="HAMR",
                station_config=_STATION_CFG,
                receiver_identity=None,
                tos_data=_TOS_DATA,
                args=_args(),
                silent=True,
            )
            == 0
        )
        assert patched.created == []

    def test_missing_station_entity_returns_zero(self, patched):
        assert (
            _sync_devices_to_tos(
                station_id="HAMR",
                station_config=_STATION_CFG,
                receiver_identity=_IDENTITY,
                tos_data={"no_id": "here"},
                args=_args(),
                silent=True,
            )
            == 0
        )

    def test_existing_device_is_not_recreated(self, patched, monkeypatch):
        """When the child already exists in TOS, nothing is created."""
        import receivers.cfg.tos_push as tp

        monkeypatch.setattr(tp, "resolve_entity_id", lambda *a, **k: 12345)
        created = _sync_devices_to_tos(
            station_id="HAMR",
            station_config=_STATION_CFG,
            receiver_identity=_IDENTITY,
            tos_data=_TOS_DATA,
            args=_args(),
            silent=True,
        )
        assert created == 0
        assert patched.created == []

    def test_receiver_without_serial_is_skipped(self, patched):
        """receiver/antenna require a serial; without one the device can't be
        identified, so it must be skipped rather than created badly."""
        created = _sync_devices_to_tos(
            station_id="HAMR",
            station_config={"receiver_type": "PolaRX5"},
            receiver_identity={"receiver_type": "PolaRX5"},
            tos_data=_TOS_DATA,
            args=_args(),
            silent=True,
        )
        assert created == 0
        assert patched.created == []


class TestNoSilentSwallow:
    def test_builder_error_is_surfaced_not_silently_counted(self, patched, monkeypatch):
        """The original bug was invisible because a TypeError was swallowed and
        reported as a generic failure. Pin that a builder failure yields 0 (so
        it is never miscounted as success) — the fix removed the error, this
        guards the counting contract."""
        import tostools.device as dev

        import receivers.cli.cfg as cfgmod

        def boom(**_kw):
            raise TypeError("simulated builder failure")

        monkeypatch.setattr(dev, "build_required_attributes", boom)
        created = _sync_devices_to_tos(
            station_id="HAMR",
            station_config=_STATION_CFG,
            receiver_identity=_IDENTITY,
            tos_data=_TOS_DATA,
            args=_args(),
            silent=True,
        )
        assert created == 0
        assert patched.created == []
