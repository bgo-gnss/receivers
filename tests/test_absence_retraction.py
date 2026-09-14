"""Having a file and recording it absent must never both be true.

``file_absence`` was write-only: ``record_file_absence`` inserts and promotes to
terminal, and nothing ever retracted a row — not even a later successful
download of that exact file. Measured on rek-d01 2026-09-14, right after
recovering RIFC/15s_24hr/2026-09-05 through the unfinalised-variant fetch:

    file_tracking  -> downloaded, RIFC202609050000a.sbf.gz, 75485 bytes
    file_absence   -> terminal = t          <-- still

Latent only because ``use_terminal_absence`` is false. The moment that is
enabled, a stale terminal row makes ``is_file_missing()`` return TRUE for a file
already in the archive, and gap detection plus the backfill skip a slot we hold.
"""

from datetime import date

from receivers.health.file_tracker import FileTracker


class _Cur:
    def __init__(self):
        self.sql = []
        self.params = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_retraction_targets_the_exact_slot():
    cur = _Cur()
    n = FileTracker._retract_absence(cur, "RIFC", "15s_24hr", date(2026, 9, 5), None)
    assert n == 1
    sql = cur.sql[0]
    assert sql.startswith("DELETE FROM file_absence")
    assert "sid = %s" in sql and "session_type = %s" in sql and "file_date = %s" in sql
    assert cur.params[0] == ("RIFC", "15s_24hr", date(2026, 9, 5), None)


def test_daily_slot_with_a_null_hour_still_matches():
    """``file_hour = NULL`` never equals NULL — a plain ``=`` retracts nothing.

    The table's unique constraint is NULLS NOT DISTINCT, so the delete has to
    use IS NOT DISTINCT FROM or every DAILY slot silently fails to retract —
    which is exactly the population (15s_24hr) this was found in.
    """
    cur = _Cur()
    FileTracker._retract_absence(cur, "RIFC", "15s_24hr", date(2026, 9, 5), None)
    assert "file_hour IS NOT DISTINCT FROM %s" in cur.sql[0]
    assert "file_hour = %s" not in cur.sql[0]


def test_retraction_is_scoped_to_receiver_absences():
    """Only receiver-sourced absences; other source_locations are not ours."""
    cur = _Cur()
    FileTracker._retract_absence(cur, "RIFC", "15s_24hr", date(2026, 9, 5), 3)
    assert "source_location = 'receiver'" in cur.sql[0]


def test_hourly_slot_passes_its_hour_through():
    cur = _Cur()
    FileTracker._retract_absence(cur, "THOB", "1Hz_1hr", date(2026, 9, 5), 7)
    assert cur.params[0][-1] == 7


def test_zero_rowcount_is_reported_as_zero():
    """Nothing to retract is the normal case and must not look like a retraction."""
    cur = _Cur()
    cur.rowcount = 0
    assert (
        FileTracker._retract_absence(cur, "X", "15s_24hr", date(2026, 9, 5), None) == 0
    )
