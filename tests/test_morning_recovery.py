"""Tests for receivers.scheduling.morning_recovery."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from receivers.scheduling.morning_recovery import (
    _confirm_recovered,
    _query_stations_missing_yesterday,
    _retry_station,
    _run_morning_recovery_job,
)


def _mock_db(rows):
    """Build a mock DatabaseConnectionFactory.connection() context manager.

    `rows` is the list of tuples to return from cur.fetchall() / fetchone().
    """
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    return conn, cur


# ─── _query_stations_missing_yesterday ─────────────────────────────────────


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_excludes_known_missing_by_default(mock_conn):
    conn, cur = _mock_db([("AFST",), ("ENTC",)])
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )

    assert result == ["AFST", "ENTC"]
    sql_arg = cur.execute.call_args[0][0]
    # The where_known_missing clause should be present
    assert "ft2.status = 'missing'" in sql_arg
    assert "NOT EXISTS" in sql_arg


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_bypass_known_missing_drops_clause(mock_conn):
    conn, cur = _mock_db([])
    mock_conn.return_value = conn

    _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=True
    )

    sql_arg = cur.execute.call_args[0][0]
    # When bypassing, the clause is excluded
    assert "ft2.status = 'missing'" not in sql_arg


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_passes_session_and_date_params(mock_conn):
    conn, cur = _mock_db([])
    mock_conn.return_value = conn

    _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )

    params = cur.execute.call_args[0][1]
    assert params == {"sess": "15s_24hr", "date": date(2026, 5, 7)}


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_returns_empty_list_when_no_missing(mock_conn):
    conn, _ = _mock_db([])
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )
    assert result == []


# ─── _confirm_recovered ────────────────────────────────────────────────────


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_confirm_recovered_true_when_archived(mock_conn):
    conn, _ = _mock_db([(1,)])
    mock_conn.return_value = conn

    assert _confirm_recovered("AFST", "15s_24hr", date(2026, 5, 7)) is True


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_confirm_recovered_false_when_no_row(mock_conn):
    conn, _ = _mock_db([])
    mock_conn.return_value = conn

    assert _confirm_recovered("AFST", "15s_24hr", date(2026, 5, 7)) is False


# ─── _retry_station ────────────────────────────────────────────────────────


@patch("receivers.scheduling.morning_recovery._confirm_recovered")
@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_returns_recovered_tuple_on_success(mock_dl, mock_confirm):
    mock_confirm.return_value = True
    sid, recovered = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )
    assert (sid, recovered) == ("AFST", True)
    mock_dl.assert_called_once_with(
        "AFST",
        "15s_24hr",
        production_mode=True,
        timeout_minutes=8,
        run_rinex=True,
    )


@patch("receivers.scheduling.morning_recovery._confirm_recovered")
@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_returns_failed_tuple_when_not_recovered(mock_dl, mock_confirm):
    mock_confirm.return_value = False
    sid, recovered = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )
    assert (sid, recovered) == ("AFST", False)


@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_swallows_exceptions(mock_dl):
    mock_dl.side_effect = RuntimeError("boom")
    sid, recovered = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )
    assert (sid, recovered) == ("AFST", False)


# ─── _run_morning_recovery_job (smoke) ─────────────────────────────────────


@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_noop_when_no_missing(mock_query):
    mock_query.return_value = []
    # Should complete without raising and not invoke any download
    _run_morning_recovery_job(
        sessions=["15s_24hr"],
        days_back=1,
        max_workers=2,
        station_timeout_minutes=8,
        bypass_known_missing=False,
    )
    mock_query.assert_called_once()


@patch("receivers.scheduling.morning_recovery._retry_station")
@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_invokes_retry_per_station(mock_query, mock_retry):
    mock_query.return_value = ["AFST", "ENTC", "FAGD"]
    mock_retry.side_effect = lambda sid, *a, **kw: (sid, True)

    _run_morning_recovery_job(
        sessions=["15s_24hr"],
        days_back=1,
        max_workers=2,
        station_timeout_minutes=8,
        bypass_known_missing=False,
    )

    assert mock_retry.call_count == 3
    called_sids = sorted(call.args[0] for call in mock_retry.call_args_list)
    assert called_sids == ["AFST", "ENTC", "FAGD"]


@patch("receivers.scheduling.morning_recovery._retry_station")
@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_handles_mixed_outcomes(mock_query, mock_retry):
    mock_query.return_value = ["AFST", "ENTC"]
    # AFST fails, ENTC succeeds
    mock_retry.side_effect = lambda sid, *a, **kw: (sid, sid == "ENTC")

    # Should complete cleanly even when some stations fail
    _run_morning_recovery_job(
        sessions=["15s_24hr"],
        days_back=1,
        max_workers=2,
        station_timeout_minutes=8,
        bypass_known_missing=False,
    )

    assert mock_retry.call_count == 2
