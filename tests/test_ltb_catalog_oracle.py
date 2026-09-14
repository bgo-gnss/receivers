"""The long-term-backfill presence oracle is ``archive_catalog``, not the disk.

Regression cover for #174 / S1. ``query_long_term_gaps`` used to ask the local
filesystem whether a file existed. ``local_prune`` empties the local rolling
window after 21 days for 1Hz while ``lookback_days`` is 30-90, so a perfectly
healthy station classified its own archived hours as gaps forever — measured on
THOB 2026-09-13 as ``queued=206, already_ok=0`` with zero real gaps.

Four traps these tests exist to pin:

* the lookup MUST filter ``storage_location='imo_archive'`` — ``local_raw`` /
  ``local_rinex`` are the same rolling window in DB form, so widening the
  filter silently restores the bug;
* the lookup MUST NOT carry a ``file_date`` predicate — that column holds
  known-bad values (the imo_archive tier spans ``0012-08-18`` to
  ``2027-01-01``), which is the whole reason the key is ``canonical_key``;
* an unreadable catalog MUST yield an EMPTY report, never a full one, and must
  never fall back to the filesystem oracle it replaced — classifying a whole
  lookback window as gaps is the stampede LTB was disabled for; and
* the raw/rinex intersection MUST be per ``(file_date, file_hour)`` slot. It
  used to be a set of bare dates, so for an hourly session one missing rinex
  hour made every raw-missing hour that day a download candidate.
"""

from datetime import date

import pytest

from receivers.scheduling import long_term_backfill as ltb


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _slot(d, h, raw, rnx):
    """rnx may be a single key or a tuple of the d/o encoding pair."""
    keys = (rnx,) if isinstance(rnx, str) else tuple(rnx)
    return ltb._Slot(
        file_date=d, file_hour=h, raw_key=raw, rinex_keys=keys, raw_path=f"/a/{raw}"
    )


@pytest.fixture
def patched(monkeypatch):
    """Neutralise every DB read except the catalog oracle under test."""
    monkeypatch.setattr(ltb, "_last_archived_date", lambda *a, **k: None)
    monkeypatch.setattr(ltb, "_receiver_horizon", lambda *a, **k: None)
    monkeypatch.setattr(ltb, "_absence_counts", lambda *a, **k: (0, 0))
    monkeypatch.setattr(ltb, "_known_missing_slots", lambda *a, **k: set())


# --------------------------------------------------------------------------
# the SQL contract
# --------------------------------------------------------------------------
class _Cur:
    """Capture the SQL and params a lookup issues, return a canned key set."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return [(k,) for k in self.rows]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def capture_sql(monkeypatch):
    """Run ``_catalog_present_keys`` against a fake connection."""

    def _run(rows, session="1Hz_1hr", category="raw", keys=("k1", "k2")):
        cur = _Cur(rows)
        import receivers.health.database_factory as dbf

        monkeypatch.setattr(
            dbf.DatabaseConnectionFactory,
            "connection",
            staticmethod(lambda **kw: _Conn(cur)),
        )
        got = ltb._catalog_present_keys(session, category, list(keys))
        return got, cur

    return _run


def test_lookup_filters_to_the_long_term_archive_tier(capture_sql):
    """``imo_archive`` only. local_raw/local_rinex ARE the pruned window."""
    assert ltb.ARCHIVE_STORAGE_LOCATION == "imo_archive"
    _, cur = capture_sql(["k1"])
    assert "storage_location = %s" in cur.sql
    assert cur.params[0] == "imo_archive"
    for pruned in ("local_raw", "local_rinex"):
        assert pruned not in cur.params, (
            f"{pruned} is the rolling local window in DB form — reading it "
            "reintroduces the very bug this oracle replaces"
        )


def test_lookup_keys_on_canonical_key_and_never_on_file_date(capture_sql):
    """file_date carries mis-dated rows (#170); the key must not touch it."""
    _, cur = capture_sql(["k1"])
    assert "canonical_key = ANY(%s)" in cur.sql
    assert "file_date" not in cur.sql, (
        "archive_catalog.file_date spans 0012-08-18..2027-01-01 on the "
        "imo_archive tier; a date predicate would re-import that corruption"
    )


def test_lookup_returns_only_the_present_subset(capture_sql):
    got, _ = capture_sql(["k1"], keys=("k1", "k2"))
    assert got == {"k1"}


def test_lookup_short_circuits_on_no_keys(monkeypatch):
    """An empty window must not open a connection at all."""

    def _boom(**kw):  # pragma: no cover - must never run
        raise AssertionError("opened a connection for zero keys")

    import receivers.health.database_factory as dbf

    monkeypatch.setattr(
        dbf.DatabaseConnectionFactory, "connection", staticmethod(_boom)
    )
    assert ltb._catalog_present_keys("1Hz_1hr", "raw", []) == set()


def test_lookup_returns_none_not_empty_on_failure(monkeypatch):
    """None means 'no oracle'. An empty set would mean 'nothing is archived'."""
    import receivers.health.database_factory as dbf

    def _boom(**kw):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(
        dbf.DatabaseConnectionFactory, "connection", staticmethod(_boom)
    )
    assert ltb._catalog_present_keys("1Hz_1hr", "raw", ["k1"]) is None


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def test_catalogued_slots_are_already_ok_not_queued(monkeypatch, patched):
    """The THOB case: everything archived, nothing on local disk."""
    slots = [_slot(date(2026, 9, 1), h, f"raw{h}", f"rnx{h}") for h in range(24)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: set(keys) if cat == "raw" else set(),
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert rep.queued == []
    assert (
        rep.already_ok == 24
    ), "already_ok is the observable that proves the oracle ran"


def test_rinex_alone_satisfies_a_slot(monkeypatch, patched):
    """Raw pruned but the RINEX product exists — that is not a gap."""
    slots = [_slot(date(2026, 9, 1), 0, "raw0", "rnx0")]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: {"rnx0"} if cat == "rinex" else set(),
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert rep.queued == []
    assert rep.already_ok == 1


def test_a_real_gap_is_still_queued(monkeypatch, patched):
    """The check that stops 'queued=0' from just meaning a blind oracle."""
    slots = [_slot(date(2026, 9, 1), h, f"raw{h}", f"rnx{h}") for h in range(24)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    # hours 5 and 6 are in neither tier
    present = {f"raw{h}" for h in range(24) if h not in (5, 6)}
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: present if cat == "raw" else set(),
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert sorted(g.file_hour for g in rep.queued) == [5, 6]
    assert rep.already_ok == 22
    assert {g.reason for g in rep.queued} == {"not_in_archive_catalog"}


def test_intersection_is_per_slot_not_per_date(monkeypatch, patched):
    """One missing rinex hour must not queue every raw-missing hour that day.

    The old code did ``rinex_missing = {g.file_date for g in rinex_gaps}``,
    collapsing 24 hourly slots onto one date. Here hour 3 has raw but no rinex
    and hour 9 has rinex but no raw: both are satisfied, and ONLY hour 17 —
    absent from both tiers — is a gap. Under the date-granular form all three
    would queue.
    """
    slots = [_slot(date(2026, 9, 1), h, f"raw{h}", f"rnx{h}") for h in range(24)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    raw_present = {f"raw{h}" for h in range(24) if h not in (9, 17)}
    rinex_present = {f"rnx{h}" for h in range(24) if h not in (3, 17)}
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: raw_present if cat == "raw" else rinex_present,
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert [g.file_hour for g in rep.queued] == [17]
    assert rep.already_ok == 23


def test_known_missing_slots_are_not_queued(monkeypatch, patched):
    """Receiver-confirmed absences stay out of the worklist."""
    slots = [_slot(date(2026, 9, 1), h, f"raw{h}", f"rnx{h}") for h in range(4)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: set())
    monkeypatch.setattr(
        ltb, "_known_missing_slots", lambda *a, **k: {(date(2026, 9, 1), 2)}
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert [g.file_hour for g in rep.queued] == [0, 1, 3]


def test_horizon_floor_still_bounds_the_worklist(monkeypatch, patched):
    """Days the receiver has aged out are unrecoverable, so not candidates."""
    slots = [_slot(date(2026, 9, d), None, f"raw{d}", f"rnx{d}") for d in (1, 2, 3)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: set())
    monkeypatch.setattr(ltb, "_receiver_horizon", lambda *a, **k: date(2026, 9, 3))
    rep = ltb.query_long_term_gaps("THOB", "15s_24hr", lookback_days=3)
    assert [g.file_date for g in rep.queued] == [date(2026, 9, 3)]


# --------------------------------------------------------------------------
# the failure direction
# --------------------------------------------------------------------------
@pytest.mark.parametrize("dead", ["raw", "rinex"])
def test_unreadable_catalog_yields_an_empty_report_not_a_full_one(
    monkeypatch, patched, dead
):
    """No oracle => no work. Queueing the window would be the stampede."""
    slots = [_slot(date(2026, 9, 1), h, f"raw{h}", f"rnx{h}") for h in range(24)]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: None if cat == dead else set(),
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert rep.queued == []
    assert rep.already_ok == 0


def test_no_filesystem_fallback_when_the_catalog_is_down(monkeypatch, patched):
    """A silent fallback to the oracle being replaced is the worst outcome."""
    slots = [_slot(date(2026, 9, 1), 0, "raw0", "rnx0")]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: None)

    import receivers.health.file_tracker as ft

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("fell back to the filesystem gap detector")

    monkeypatch.setattr(ft.GapDetector, "find_gaps", _boom)
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert rep.queued == []


def test_classifier_never_calls_find_gaps_at_all(monkeypatch, patched):
    """Even on the happy path. find_gaps is the filesystem oracle."""
    slots = [_slot(date(2026, 9, 1), 0, "raw0", "rnx0")]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: {"raw0"})

    import receivers.health.file_tracker as ft

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("classifier still probes the filesystem")

    monkeypatch.setattr(ft.GapDetector, "find_gaps", _boom)
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=1)
    assert rep.already_ok == 1


# --------------------------------------------------------------------------
# the anchors this module leans on
# --------------------------------------------------------------------------
def test_slot_generation_reuses_the_gap_detector_definitions():
    """``_expected_slots`` borrows two GapDetector members by name.

    ``_generate_expected_files`` is private but is the SINGLE definition of
    "daily vs hourly" that ``find_gaps`` also uses — re-deriving it here would
    let the classifier and gap detection drift about what a station is even
    supposed to produce. If either name moves, fix ``_expected_slots``; do not
    delete this test.
    """
    from receivers.health.file_tracker import ArchiveFileChecker, GapDetector

    assert hasattr(GapDetector, "_generate_expected_files")
    assert hasattr(ArchiveFileChecker, "build_archive_path")


def test_hourly_and_daily_sessions_produce_the_expected_slot_counts():
    """Pins the rule ``_expected_slots`` inherits, without touching a DB."""
    from receivers.health.file_tracker import GapDetector

    gen = GapDetector._generate_expected_files
    start = end = date(2026, 9, 1)
    daily = gen(None, "THOB", "15s_24hr", start, end)
    hourly = gen(None, "THOB", "1Hz_1hr", start, end)
    assert [h for _, h in daily] == [None]
    assert [h for _, h in hourly] == list(range(24))


# --------------------------------------------------------------------------
# the rinex-name defect found in live validation
# --------------------------------------------------------------------------
def test_rinex_keys_are_igs_short_names_not_the_raw_filename():
    """``build_archive_path`` returns the RAW name for a ``_rinex`` session.

    Found on rek-d01 2026-09-13: the expected "rinex" key for THOB came back as
    ``thob202609100000b.sbf`` while the archive holds ``THOB253a.26D.Z``. The
    rinex half of the oracle could therefore never match, silently reducing the
    raw/rinex union to raw alone. Invisible on a healthy PolaRX5 station;
    catastrophic on a stream-acquired one — GONH (mosaic-X5) has almost no 1Hz
    raw and scored 684 false gaps out of 720 with 3,026 RINEX hours catalogued.
    """
    from datetime import datetime

    hourly = ltb._rinex_keys("THOB", datetime(2026, 9, 10, 0), 0)
    assert "thob253a.26d" in hourly
    assert not any(
        k.endswith(".sbf") for k in hourly
    ), "a raw filename here means build_archive_path leaked back in"
    # hour letter advances a..x
    assert "thob253b.26d" in ltb._rinex_keys("THOB", datetime(2026, 9, 10, 1), 1)
    assert "thob253x.26d" in ltb._rinex_keys("THOB", datetime(2026, 9, 10, 23), 23)
    # daily form ends in 0, not an hour letter
    assert "thob2530.26d" in ltb._rinex_keys("THOB", datetime(2026, 9, 10), None)


def test_rinex_keys_cover_both_hatanaka_and_plain_observation():
    """canonical_key does not fold ``d``/``o``; both answer "product exists"."""
    from datetime import datetime

    keys = ltb._rinex_keys("THOB", datetime(2026, 9, 10, 0), 0)
    assert {"thob253a.26d", "thob253a.26o"} <= set(keys)


def test_a_rinex_only_station_is_not_all_gaps(monkeypatch, patched):
    """The GONH shape: stream-acquired, so raw is absent by design."""
    from datetime import datetime

    slots = [
        ltb._Slot(
            file_date=date(2026, 9, 10),
            file_hour=h,
            raw_key=f"gonh2026091{h:02d}b.sbf",
            rinex_keys=ltb._rinex_keys("GONH", datetime(2026, 9, 10, h), h),
            raw_path="/a/x",
        )
        for h in range(24)
    ]
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    catalogued = {k for sl in slots for k in sl.rinex_keys if k.endswith(".26d")}
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: catalogued if cat == "rinex" else set(),
    )
    rep = ltb.query_long_term_gaps("GONH", "1Hz_1hr", lookback_days=1)
    assert rep.queued == []
    assert rep.already_ok == 24


# --------------------------------------------------------------------------
# the settling horizon (the trailing edge)
# --------------------------------------------------------------------------
def _recent_slots(hours_ago):
    """One slot per entry, each ENDING that many hours before now."""
    from datetime import datetime, timedelta

    now = datetime.now(ltb.UTC)
    out = []
    for h in hours_ago:
        end = now - timedelta(hours=h)
        # _slot_end(d, hour) == midnight(d) + (hour+1)h, so invert it
        slot_start = end - timedelta(hours=1)
        out.append(
            ltb._Slot(
                file_date=slot_start.date(),
                file_hour=slot_start.hour,
                raw_key=f"raw{h}",
                rinex_keys=(f"rnx{h}",),
                raw_path=f"/a/raw{h}",
            )
        )
    return out


def test_slot_end_is_when_recording_finished():
    """Absence before this instant is meaningless — the file does not exist."""
    from datetime import datetime

    assert ltb._slot_end(date(2026, 9, 10), 0) == datetime(
        2026, 9, 10, 1, tzinfo=ltb.UTC
    )
    assert ltb._slot_end(date(2026, 9, 10), 23) == datetime(
        2026, 9, 11, 0, tzinfo=ltb.UTC
    )
    # a daily slot covers the whole day, so it ends at the next midnight
    assert ltb._slot_end(date(2026, 9, 10), None) == datetime(
        2026, 9, 11, 0, tzinfo=ltb.UTC
    )


def test_recent_absence_is_not_settled_rather_than_queued(monkeypatch, patched):
    """The trailing edge: THOB/OLKE/GONH each queued (2026-09-13, 23) at 00:36.

    That hour was collected ~50 min earlier and was sitting on local disk; it
    was simply not yet in archive_catalog, because the hourly archive-sync
    sweep runs at :45. Absent the guard, EVERY healthy station reports its own
    most recent hour as a gap — ~180 futile downloads per fleet run.
    """
    slots = _recent_slots([1, 2, 30])  # 1h and 2h ago are unsettled at 6h
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: set())
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=2, settle_hours=6)
    assert rep.not_settled == 2
    assert len(rep.queued) == 1, "the 30h-old slot is settled and IS a real gap"
    assert rep.queued[0].expected_path == "/a/raw30", "the wrong slot survived"


def test_settled_absence_is_still_a_gap(monkeypatch, patched):
    """The guard must not swallow real gaps just outside the horizon."""
    slots = _recent_slots([7, 8, 40])
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: set())
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=3, settle_hours=6)
    assert rep.not_settled == 0
    assert len(rep.queued) == 3


def test_presence_wins_over_recency(monkeypatch, patched):
    """A recent slot ALREADY in the catalog is already_ok, not not_settled.

    Order matters: checking recency first would under-report already_ok and
    make the oracle look less effective than it is.
    """
    slots = _recent_slots([1])
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(
        ltb,
        "_catalog_present_keys",
        lambda s, cat, keys: {"raw1"} if cat == "raw" else set(),
    )
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=2, settle_hours=6)
    assert rep.already_ok == 1
    assert rep.not_settled == 0
    assert rep.queued == []


def test_settle_hours_zero_restores_the_old_behaviour(monkeypatch, patched):
    """Explicit escape hatch, so the guard is provably the thing doing the work."""
    slots = _recent_slots([1])
    monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
    monkeypatch.setattr(ltb, "_catalog_present_keys", lambda s, cat, keys: set())
    rep = ltb.query_long_term_gaps("THOB", "1Hz_1hr", lookback_days=2, settle_hours=0)
    assert rep.not_settled == 0
    assert len(rep.queued) == 1


def test_default_settle_hours_covers_the_measured_archive_lag():
    """Measured 2026-09-14: 168 of 169 online stations within 4.4 h; one at 8.4.

    Lowering this below ~4 without re-measuring brings the trailing-edge false
    gap straight back.
    """
    assert ltb.DEFAULT_SETTLE_HOURS >= 4
