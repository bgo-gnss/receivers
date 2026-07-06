"""Supersede-cleanup: after a durable push+index of the new long-name product,
remove the legacy short-name file it replaces (portal + DB)."""

from unittest.mock import patch

from receivers.archive.remove import RemoveResult
from receivers.dissemination.rinex_index import deindex_rinex_file, supersede_legacy


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._cur = _Cur(rows)
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


def test_deindex_deletes_by_name_and_returns_ids():
    conn = _Conn([(228136,)])
    ids = deindex_rinex_file(conn, "RHOF1770.26D.Z")
    assert ids == [228136]
    assert conn.committed
    assert "DELETE FROM rinex_file WHERE name = %s" in conn._cur.sql
    assert conn._cur.params == ("RHOF1770.26D.Z",)


def test_deindex_none_matched():
    conn = _Conn([])
    assert deindex_rinex_file(conn, "RHOF1770.26D.Z") == []


def _remove_result(**kw):
    r = RemoveResult()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_supersede_dry_run_shows_intent_no_writes():
    """dry_run: portal delete is a preview (would_remove), DB untouched."""
    conn = _Conn([(1,)])  # would raise if de-index ran & committed unexpectedly
    fake_rm = _remove_result(
        would_delete=[("2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z", 3500000)]
    )
    with patch(
        "receivers.archive.remove.remove_archive_files", return_value=fake_rm
    ) as m:
        out = supersede_legacy(
            conn,
            superseded_name="RHOF1770.26D.Z",
            relative_dir="2026/jun/RHOF/15s_24hr/rinex",
            ssh_target="epos@epos-portal.vedur.is",
            dest_root="/mnt/epos_01/gps",
            dry_run=True,
        )
    # portal delete invoked with execute=False
    assert m.call_args.kwargs["execute"] is False
    assert out["would_remove"] == ["2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z"]
    assert out["removed"] == []
    assert out["deindexed"] == []
    assert conn.committed is False  # DB not touched in dry-run


def test_supersede_real_removes_and_deindexes():
    conn = _Conn([(228136,)])
    fake_rm = _remove_result(
        deleted=[("2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z", 3500000)]
    )
    with patch(
        "receivers.archive.remove.remove_archive_files", return_value=fake_rm
    ) as m:
        out = supersede_legacy(
            conn,
            superseded_name="RHOF1770.26D.Z",
            relative_dir="2026/jun/RHOF/15s_24hr/rinex",
            ssh_target="epos@epos-portal.vedur.is",
            dest_root="/mnt/epos_01/gps",
            dry_run=False,
        )
    assert m.call_args.kwargs["execute"] is True
    assert m.call_args.kwargs["ssh_target"] == "epos@epos-portal.vedur.is"
    assert m.call_args.kwargs["dest_root"] == "/mnt/epos_01/gps"
    assert out["removed"] == ["2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z"]
    assert out["deindexed"] == [228136]
    assert conn.committed is True


def test_supersede_missing_legacy_is_noop_not_error():
    conn = _Conn([])
    fake_rm = _remove_result(missing=["2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z"])
    with patch("receivers.archive.remove.remove_archive_files", return_value=fake_rm):
        out = supersede_legacy(
            conn,
            superseded_name="RHOF1770.26D.Z",
            relative_dir="2026/jun/RHOF/15s_24hr/rinex",
            ssh_target="epos@epos-portal.vedur.is",
            dest_root="/mnt/epos_01/gps",
            dry_run=False,
        )
    assert out["removed"] == []
    assert "2026/jun/RHOF/15s_24hr/rinex/RHOF1770.26D.Z" in out["skipped"]


# ── pgbouncer transaction-pooling hardening ──────────────────────────────────
#
# psql.vedur.is:6432 is pgbouncer in transaction pooling: the session-level
# SET search_path from epos_db.connect() only sticks to the backend that ran
# that one transaction, so any later transaction can land on a fresh backend
# and fail with 'relation "rinex_file" does not exist' (observed live on the
# 2026-07-06 RHOF full-history run — the first miss then poisoned every
# subsequent de-index in the flush). Each transaction must carry SET LOCAL.


class _RecordingCur(_Cur):
    """Cursor that records every executed statement."""

    def __init__(self, rows):
        super().__init__(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        super().execute(sql, params if params is not None else ())


class _RecordingConn(_Conn):
    def __init__(self, rows):
        super().__init__(rows)
        self._cur = _RecordingCur(rows)


def test_tx_cursor_plants_set_local_for_registered_conn():
    from receivers.dissemination import epos_db

    conn = _RecordingConn([(1,)])
    epos_db._CONN_SCHEMAS[conn] = "gnss-europe-v0-2-9"
    with epos_db.tx_cursor(conn) as cur:
        cur.execute("SELECT 1", ())
    assert cur.executed[0] == 'SET LOCAL search_path TO "gnss-europe-v0-2-9"'
    assert cur.executed[1] == "SELECT 1"


def test_tx_cursor_plain_for_unregistered_conn():
    from receivers.dissemination import epos_db

    conn = _RecordingConn([(1,)])
    with epos_db.tx_cursor(conn) as cur:
        cur.execute("SELECT 1", ())
    assert cur.executed == ["SELECT 1"]


def test_deindex_carries_set_local_in_same_transaction():
    from receivers.dissemination import epos_db

    conn = _RecordingConn([(5,)])
    epos_db._CONN_SCHEMAS[conn] = "gnss-europe-v0-2-9"
    assert deindex_rinex_file(conn, "RHOF1770.26D.Z") == [5]
    assert conn._cur.executed[0].startswith("SET LOCAL search_path")
    assert "DELETE FROM rinex_file" in conn._cur.executed[1]


def test_batch_deindexes_names_already_missing_from_portal():
    """A prior partially-failed flush removed the file but left the DB row —
    the re-run must still de-index it (missing-from-portal is 'clean')."""
    from receivers.dissemination.rinex_index import supersede_legacy_batch

    conn = _Conn([(7,)])
    fake_rm = _remove_result(
        deleted=[("2012/aug/RHOF/15s_24hr/rinex/RHOF2410.12D.Z", 1000)],
        missing=["2012/aug/RHOF/15s_24hr/rinex/RHOF2420.12D.Z"],
    )
    with patch("receivers.archive.remove.remove_archive_files", return_value=fake_rm):
        out = supersede_legacy_batch(
            conn,
            [
                ("RHOF2410.12D.Z", "2012/aug/RHOF/15s_24hr/rinex"),
                ("RHOF2420.12D.Z", "2012/aug/RHOF/15s_24hr/rinex"),
            ],
            ssh_target="epos@epos-portal.vedur.is",
            dest_root="/mnt/epos_01/gps",
            dry_run=False,
        )
    # both the just-removed AND the already-missing name were de-indexed
    assert out["deindexed"] == [7, 7]


def test_batch_deindex_failure_recovers_and_retries_without_poisoning():
    """First DELETE hits a backend without the search_path → recover + retry;
    later names in the same flush must not be poisoned."""
    from receivers.dissemination import rinex_index

    calls = {"n": 0, "recovered": 0}

    def flaky_deindex(conn, name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError('relation "rinex_file" does not exist')
        return [calls["n"]]

    def fake_recover(conn):
        calls["recovered"] += 1
        return True

    fake_rm = _remove_result(
        deleted=[
            ("2012/aug/RHOF/15s_24hr/rinex/RHOF2410.12D.Z", 1000),
            ("2012/aug/RHOF/15s_24hr/rinex/RHOF2420.12D.Z", 1000),
        ]
    )
    with (
        patch("receivers.archive.remove.remove_archive_files", return_value=fake_rm),
        patch.object(rinex_index, "deindex_rinex_file", side_effect=flaky_deindex),
        patch.object(rinex_index.epos_db, "recover", side_effect=fake_recover),
    ):
        out = rinex_index.supersede_legacy_batch(
            object(),
            [
                ("RHOF2410.12D.Z", "2012/aug/RHOF/15s_24hr/rinex"),
                ("RHOF2420.12D.Z", "2012/aug/RHOF/15s_24hr/rinex"),
            ],
            ssh_target="epos@epos-portal.vedur.is",
            dest_root="/mnt/epos_01/gps",
            dry_run=False,
        )
    assert calls["recovered"] == 1  # first failure recovered
    assert out["deindexed"] == [2, 3]  # retry of name1 + clean name2
