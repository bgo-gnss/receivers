"""Contract tests for the pinned ``tostools`` dependency.

The cfg orchestration in :mod:`receivers.cfg.operations` calls a set of
:class:`tostools.api.tos_writer.TOSWriter` methods that were added across
several tostools releases. The test suite for operations.py uses
:class:`MagicMock` which auto-vivifies any attribute access — so a
missing method on the pinned tostools version goes undetected until a
real run hits an ``AttributeError`` on the first writer call.

These tests assert the symbols exist at import time so CI catches a
stale tostools pin before operators run into it in production.
"""

from __future__ import annotations

import pytest


def _writer_methods():
    """Return the importable TOSWriter class — skips if tostools missing."""
    pytest.importorskip("tostools")
    from tostools.api.tos_writer import TOSWriter

    return TOSWriter


REQUIRED_WRITER_METHODS = (
    # Pattern 2 join move + helpers
    "move_device",
    "get_open_parent_join",
    "patch_entity_connection",
    "create_entity_connection",
    "delete_entity_connection",
    # Vitjun (maintenance visit)
    "add_maintenance_visit",
    "get_maintenance_visit",
    "list_maintenance_visits",
    "update_maintenance_visit",
    # Entity lookup + attribute transitions
    "find_station_by_marker",
    "find_location_by_name",
    "find_device_by_serial",
    "get_entity_history",
    "transition_attribute_value",
    # Device intake (warehouse step in replace_receiver)
    "create_device",
    "connect_device_to_location",
)


@pytest.mark.parametrize("method", REQUIRED_WRITER_METHODS)
def test_tos_writer_has_method(method: str) -> None:
    """Each required method must exist on the pinned tostools TOSWriter."""
    TOSWriter = _writer_methods()
    assert hasattr(TOSWriter, method), (
        f"TOSWriter is missing required method {method!r}. "
        f"The receivers cfg orchestration calls this method; bump the "
        f"tostools pin in pyproject.toml to a release containing it."
    )


def test_build_required_attributes_importable() -> None:
    """replace_receiver's warehouse-intake step imports this helper."""
    pytest.importorskip("tostools")
    from tostools.device import build_required_attributes  # noqa: F401


def test_to_igs_receiver_importable() -> None:
    """replace_receiver maps model names through this helper."""
    pytest.importorskip("tostools")
    from tostools.standards.igs_equipment import to_igs_receiver  # noqa: F401
