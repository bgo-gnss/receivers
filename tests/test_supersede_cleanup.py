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
