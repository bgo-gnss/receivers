"""Tests for receivers.cfg.discrepancy_log.

The writer module is mocked at the ``DatabaseConnectionFactory.connection``
boundary so these tests don't need a live PostgreSQL. End-to-end behaviour
against a real database is covered by the smoke check that ships alongside
migration 038.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.discrepancy_log import (
    ACTION_AUTO_RESOLVED,
    ACTION_CFG_UPDATED,
    ACTION_SUPERSEDED,
    DETECTED_BY_HEALTH,
    DiscrepancyRecord,
    auto_resolve_if_open,
    get_history,
    list_open,
    record_detection,
    record_resolution,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_connection_yielding(cursor: MagicMock) -> MagicMock:
    """Build a ``DatabaseConnectionFactory.connection`` mock.

    The factory returns a context manager that yields a connection. The
    connection's ``cursor()`` is itself a context manager yielding the
    cursor we control.
    """
    conn = MagicMock(name="conn")
    cursor_cm = MagicMock(name="cursor_cm")
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False
    conn.cursor.return_value = cursor_cm

    factory_cm = MagicMock(name="factory_cm")
    factory_cm.__enter__.return_value = conn
    factory_cm.__exit__.return_value = False
    return factory_cm


@pytest.fixture
def mocked_db():
    """Patch DatabaseConnectionFactory.connection with a fresh cursor mock."""
    cursor = MagicMock(name="cursor")
    factory_cm = _mock_connection_yielding(cursor)
    with patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.connection",
        return_value=factory_cm,
    ):
        yield cursor


# ---------------------------------------------------------------------------
# record_detection
# ---------------------------------------------------------------------------


class TestRecordDetection:
    def test_inserts_when_no_open_row_exists(self, mocked_db):
        cursor = mocked_db
        cursor.fetchone.side_effect = [None, (42,)]

        rid = record_detection(
            "ELDC",
            "receiver_serial",
            cfg_value="AAA",
            receiver_value="BBB",
            tos_value=None,
            verdict="conflict",
            detected_by=DETECTED_BY_HEALTH,
        )

        assert rid == 42
        # Calls: pg_advisory_xact_lock, SELECT, INSERT
        assert cursor.execute.call_count == 3
        lock_sql, _ = cursor.execute.call_args_list[0].args
        assert "pg_advisory_xact_lock" in lock_sql
        select_sql, select_params = cursor.execute.call_args_list[1].args
        assert "SELECT id" in select_sql
        assert select_params == ("ELDC", "receiver_serial")
        insert_sql, insert_params = cursor.execute.call_args_list[2].args
        assert "INSERT INTO cfg_discrepancy" in insert_sql
        assert insert_params == (
            "ELDC",
            "receiver_serial",
            "AAA",
            "BBB",
            None,
            "conflict",
            DETECTED_BY_HEALTH,
        )

    def test_idempotent_when_open_row_matches(self, mocked_db):
        cursor = mocked_db
        cursor.fetchone.return_value = (7, "AAA", "BBB", None, "conflict")

        rid = record_detection(
            "ELDC",
            "receiver_serial",
            cfg_value="AAA",
            receiver_value="BBB",
            tos_value=None,
            verdict="conflict",
            detected_by=DETECTED_BY_HEALTH,
        )

        assert rid == 7
        # Calls: pg_advisory_xact_lock, SELECT — no UPDATE, no INSERT.
        assert cursor.execute.call_count == 2

    def test_supersedes_existing_when_values_changed(self, mocked_db):
        cursor = mocked_db
        cursor.fetchone.side_effect = [
            (7, "AAA", "BBB", None, "conflict"),  # existing open row
            (8,),  # new INSERT id
        ]

        rid = record_detection(
            "ELDC",
            "receiver_serial",
            cfg_value="AAA",
            receiver_value="CCC",
            tos_value=None,
            verdict="conflict",
            detected_by=DETECTED_BY_HEALTH,
        )

        assert rid == 8
        # Calls: pg_advisory_xact_lock, SELECT, UPDATE, INSERT
        assert cursor.execute.call_count == 4
        update_sql, update_params = cursor.execute.call_args_list[2].args
        assert "UPDATE cfg_discrepancy" in update_sql
        assert ACTION_SUPERSEDED in update_params

    def test_swallows_db_error_and_returns_none(self):
        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            side_effect=RuntimeError("db down"),
        ):
            rid = record_detection(
                "ELDC",
                "receiver_serial",
                cfg_value="AAA",
                receiver_value="BBB",
                tos_value=None,
                verdict="conflict",
                detected_by=DETECTED_BY_HEALTH,
            )
        assert rid is None


# ---------------------------------------------------------------------------
# record_resolution
# ---------------------------------------------------------------------------


class TestRecordResolution:
    def test_updates_open_row(self, mocked_db):
        cursor = mocked_db
        cursor.rowcount = 1

        ok = record_resolution(
            "ELDC",
            "receiver_serial",
            action=ACTION_CFG_UPDATED,
            resolved_value="BBB",
            resolved_by="bgo",
            note="manual",
        )

        assert ok is True
        sql, params = cursor.execute.call_args.args
        assert "UPDATE cfg_discrepancy" in sql
        assert params == (
            "bgo",
            ACTION_CFG_UPDATED,
            "BBB",
            "manual",
            "ELDC",
            "receiver_serial",
        )

    def test_returns_false_when_nothing_open(self, mocked_db):
        cursor = mocked_db
        cursor.rowcount = 0

        ok = record_resolution(
            "ELDC",
            "receiver_serial",
            action=ACTION_CFG_UPDATED,
            resolved_value="BBB",
            resolved_by="bgo",
        )

        assert ok is False

    def test_swallows_db_error(self):
        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            side_effect=RuntimeError("db down"),
        ):
            ok = record_resolution(
                "ELDC",
                "receiver_serial",
                action=ACTION_CFG_UPDATED,
                resolved_value="BBB",
                resolved_by="bgo",
            )
        assert ok is False


# ---------------------------------------------------------------------------
# auto_resolve_if_open
# ---------------------------------------------------------------------------


class TestAutoResolve:
    def test_uses_auto_resolved_action_and_auto_user(self, mocked_db):
        cursor = mocked_db
        cursor.rowcount = 1

        ok = auto_resolve_if_open("ELDC", "receiver_serial")

        assert ok is True
        sql, params = cursor.execute.call_args.args
        assert "UPDATE cfg_discrepancy" in sql
        assert params[0] == "auto"
        assert params[1] == ACTION_AUTO_RESOLVED


# ---------------------------------------------------------------------------
# list_open
# ---------------------------------------------------------------------------


def _make_row(rid: int, station_id: str, cfg_key: str, **overrides):
    """Build a tuple matching _ALL_COLS order in discrepancy_log."""
    base = {
        "cfg_value": None,
        "receiver_value": None,
        "tos_value": None,
        "verdict": "conflict",
        "detected_at": datetime(2026, 5, 3, tzinfo=timezone.utc),
        "detected_by": DETECTED_BY_HEALTH,
        "resolved_at": None,
        "resolved_by": None,
        "resolved_action": None,
        "resolved_value": None,
        "resolution_note": None,
    }
    base.update(overrides)
    return (
        rid,
        station_id,
        cfg_key,
        base["cfg_value"],
        base["receiver_value"],
        base["tos_value"],
        base["verdict"],
        base["detected_at"],
        base["detected_by"],
        base["resolved_at"],
        base["resolved_by"],
        base["resolved_action"],
        base["resolved_value"],
        base["resolution_note"],
    )


class TestListOpen:
    def test_returns_records_with_no_filters(self, mocked_db):
        cursor = mocked_db
        cursor.fetchall.return_value = [
            _make_row(1, "ELDC", "receiver_serial"),
            _make_row(2, "THOB", "receiver_firmware_version", verdict="missing"),
        ]

        rows = list_open()

        assert len(rows) == 2
        assert all(isinstance(r, DiscrepancyRecord) for r in rows)
        assert rows[0].station_id == "ELDC"
        assert rows[1].verdict == "missing"
        sql, params = cursor.execute.call_args.args
        assert "resolved_at IS NULL" in sql
        assert params == []

    def test_filters_combine(self, mocked_db):
        cursor = mocked_db
        cursor.fetchall.return_value = []

        list_open(
            station_ids=["ELDC", "THOB"],
            cfg_keys=["receiver_serial"],
            verdicts=["conflict"],
        )

        sql, params = cursor.execute.call_args.args
        assert "station_id = ANY(%s)" in sql
        assert "cfg_key = ANY(%s)" in sql
        assert "verdict = ANY(%s)" in sql
        assert params == [["ELDC", "THOB"], ["receiver_serial"], ["conflict"]]


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def test_requires_station_or_field(self):
        with pytest.raises(ValueError):
            get_history()

    def test_filters_by_station(self, mocked_db):
        cursor = mocked_db
        cursor.fetchall.return_value = [_make_row(1, "ELDC", "receiver_serial")]

        rows = get_history(station_id="ELDC", limit=50)

        assert len(rows) == 1
        sql, params = cursor.execute.call_args.args
        assert "station_id = %s" in sql
        assert "ORDER BY detected_at DESC" in sql
        assert params == ["ELDC", 50]

    def test_filters_by_field_and_since(self, mocked_db):
        cursor = mocked_db
        cursor.fetchall.return_value = []
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        get_history(cfg_key="receiver_serial", since=since, limit=10)

        sql, params = cursor.execute.call_args.args
        assert "cfg_key = %s" in sql
        assert "detected_at >= %s" in sql
        assert params == ["receiver_serial", since, 10]


# ---------------------------------------------------------------------------
# DiscrepancyRecord
# ---------------------------------------------------------------------------


class TestDiscrepancyRecord:
    def test_is_open_true_when_unresolved(self):
        rec = DiscrepancyRecord(
            id=1,
            station_id="ELDC",
            cfg_key="receiver_serial",
            cfg_value=None,
            receiver_value=None,
            tos_value=None,
            verdict="conflict",
            detected_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            detected_by=DETECTED_BY_HEALTH,
            resolved_at=None,
            resolved_by=None,
            resolved_action=None,
            resolved_value=None,
            resolution_note=None,
        )
        assert rec.is_open is True

    def test_as_dict_includes_iso_timestamps(self):
        ts = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
        rec = DiscrepancyRecord(
            id=1,
            station_id="ELDC",
            cfg_key="receiver_serial",
            cfg_value="AAA",
            receiver_value="BBB",
            tos_value=None,
            verdict="conflict",
            detected_at=ts,
            detected_by=DETECTED_BY_HEALTH,
            resolved_at=ts,
            resolved_by="bgo",
            resolved_action=ACTION_CFG_UPDATED,
            resolved_value="BBB",
            resolution_note=None,
        )
        d = rec.as_dict()
        assert d["detected_at"].startswith("2026-05-03T12:00:00")
        assert d["resolved_at"].startswith("2026-05-03T12:00:00")
        assert d["resolved_action"] == ACTION_CFG_UPDATED
