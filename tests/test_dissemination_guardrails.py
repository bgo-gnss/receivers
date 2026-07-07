"""Guardrails for the EPOS dissemination one-file-per-slot invariant.

Covers the three guardrails added after the ELEY R2-mistake cleanup:

* **G1** — the engine skips a decimated (``sample``) product when the source is
  RINEX2 (short naming can't encode the rate → a rate-less duplicate).
* **G2** — ``purge_stale_siblings_batch`` removes any OTHER indexed file in the
  same (station, obs-date, dir) slot, by its ACTUAL stored name/case, bounded to
  one slot (never a DOY glob).
* **G3** — ``published_name`` forces uppercase ``.D`` on RINEX2 Hatanaka shorts
  to match the archive (.d->.D) and the legacy EPOS portal.
"""

from __future__ import annotations

import contextlib
from datetime import date

import pytest

from receivers.dissemination import engine as eng_mod
from receivers.dissemination import rinex_index
from receivers.dissemination.config import ProductSpec, VersionPolicy
from receivers.dissemination.convert import published_name


# --------------------------------------------------------------------------- G3
class TestG3UppercaseHatanaka:
    def test_r2_short_forced_uppercase_D(self):
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        # lowercase obs .26o -> uppercase .26D.Z (not .26d.Z)
        assert published_name("ELEY0600.26o", r2) == "ELEY0600.26D.Z"

    def test_r2_short_already_uppercase_stays_D(self):
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        assert published_name("ELEY0600.26O", r2) == "ELEY0600.26D.Z"

    def test_r3_long_crx_unaffected(self):
        r3 = VersionPolicy(naming="long", hatanaka=True, compression="gz")
        out = published_name("RHOF00ISL_R_20261280000_01D_15S_MO.rnx", r3)
        assert out == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"

    def test_plain_obs_not_uppercased(self):
        plain = VersionPolicy(naming="short", hatanaka=False, compression="Z")
        # no Hatanaka -> name is the obs itself, must not be touched
        assert published_name("ELEY0600.26o", plain) == "ELEY0600.26o.Z"


# --------------------------------------------------------------------------- G1
class _Conv:
    def __init__(self, version, tmp):
        self.obs_name = "ELEY0600.26o"
        self.output_path = tmp / "obs.26o"
        self.output_path.write_text("     2.11           OBSERVATION DATA\n")
        self.rinex_version = version
        self.cached = False


def _min_target(tmp_path):
    from tests.test_dissemination import _target

    return _target(tmp_path)


class TestG1SkipDecimatedForR2:
    def test_decimated_product_skipped_when_source_is_r2(self, tmp_path, monkeypatch):
        eng = eng_mod.EposDisseminate(_min_target(tmp_path))
        src = tmp_path / "ELEY0600.26D.Z"
        src.write_bytes(b"x")
        monkeypatch.setattr(eng, "find_source", lambda s, d: src)
        monkeypatch.setattr(eng, "find_raw_source", lambda s, d: None)
        monkeypatch.setattr(
            eng_mod, "convert_for_dissemination", lambda *a, **k: _Conv(2, tmp_path)
        )
        r = eng.run_one("ELEY", date(2026, 3, 1), product=ProductSpec(sample=30))
        assert r.ok is False
        assert "decimated" in r.message and "30" in r.message
        assert r.rinex_version == 2

    def test_native_product_not_skipped_by_g1(self, tmp_path, monkeypatch):
        # sample=None must NOT hit the G1 skip (it proceeds past the guard; we
        # stop it at push by pointing at a no-host target so nothing is sent).
        eng = eng_mod.EposDisseminate(_min_target(tmp_path))
        src = tmp_path / "ELEY0600.26D.Z"
        src.write_bytes(b"x")
        monkeypatch.setattr(eng, "find_source", lambda s, d: src)
        monkeypatch.setattr(eng, "find_raw_source", lambda s, d: None)
        monkeypatch.setattr(
            eng_mod, "convert_for_dissemination", lambda *a, **k: _Conv(2, tmp_path)
        )
        r = eng.run_one("ELEY", date(2026, 3, 1), product=ProductSpec(sample=None))
        # It gets past G1 — the message is NOT the decimated-skip one.
        assert "decimated" not in r.message


# --------------------------------------------------------------------------- G2
class _FakeCursor:
    def __init__(self, select_rows, delete_ids):
        self._select = select_rows
        self._delete = delete_ids
        self._last = ""
        self.calls = []

    def execute(self, sql, params=None):
        self._last = sql
        self.calls.append((sql, params))

    def fetchall(self):
        return [(i,) for i in self._delete] if "DELETE" in self._last else self._select


class _FakeConn:
    closed = 0

    def commit(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """Patch epos_db.tx_cursor + remove_archive_files; return a recorder."""
    state = {"cursor": None, "removed": [], "rm_kwargs": None}

    @contextlib.contextmanager
    def _tx(_conn):
        yield state["cursor"]

    monkeypatch.setattr(rinex_index.epos_db, "tx_cursor", _tx)

    class _RM:
        def __init__(self, rels):
            self.deleted = [(r, 100) for r in rels]
            self.would_delete = []
            self.skipped_toobig = []
            self.missing = []
            self.invalid = []

    import receivers.archive.remove as remove_mod

    def _fake_remove(rels, *, ssh_target, dest_root, max_size, execute):
        state["removed"] = list(rels)
        state["rm_kwargs"] = dict(dest_root=dest_root, execute=execute)
        # would_delete on dry-run, deleted on execute
        rm = _RM(rels if execute else [])
        if not execute:
            rm.would_delete = [(r, 100) for r in rels]
        return rm

    monkeypatch.setattr(remove_mod, "remove_archive_files", _fake_remove)
    return state


class TestG2FindStaleSiblings:
    def test_full_path_and_legacy_dir_only_in_slot_deeper_excluded(self, fake_db):
        rel = "2026/mar/ELEY/15s_24hr/rinex"
        prefix = f"/files/{rel}/"
        rows = [
            # our full-path row
            ("ELEY0600.26d.Z", prefix + "ELEY0600.26d.Z"),
            # legacy container dir-only row (filename lives in `name`)
            ("ELEY0600.26X.Z", prefix),
            # deeper subdir -> different slot -> excluded
            ("ELEY0600.26Y.Z", prefix + "sub/ELEY0600.26Y.Z"),
        ]
        fake_db["cursor"] = _FakeCursor(rows, [])
        out = rinex_index.find_stale_siblings(
            _FakeConn(),
            marker="ELEY",
            obs_date=date(2026, 3, 1),
            relative_dir=rel,
            keep_name="ELEY00ISL_R_20260600000_01D_15S_MO.crx.gz",
        )
        # (name, stored_path, dest_rel); dest_rel is always <dir>/<name>
        assert out == [
            ("ELEY0600.26d.Z", prefix + "ELEY0600.26d.Z", f"{rel}/ELEY0600.26d.Z"),
            ("ELEY0600.26X.Z", prefix, f"{rel}/ELEY0600.26X.Z"),
        ]

    def test_query_pins_date_and_dir(self, fake_db):
        cur = _FakeCursor([], [])
        fake_db["cursor"] = cur
        rinex_index.find_stale_siblings(
            _FakeConn(),
            marker="ELEY",
            obs_date=date(2026, 3, 1),
            relative_dir="2026/mar/ELEY/15s_24hr/rinex",
            keep_name="keep",
        )
        sql, params = cur.calls[-1]
        assert "reference_date::date = %s" in sql
        assert "relative_path LIKE %s" in sql
        assert "lower(rf.name) <> lower(%s)" in sql
        # marker, date, dir-prefix, keep
        assert params[0] == "ELEY"
        assert params[2] == "/files/2026/mar/ELEY/15s_24hr/rinex/%"


class TestG2PurgeBatch:
    def _slot(self):
        return (
            "ELEY",
            date(2026, 3, 1),
            "2026/mar/ELEY/15s_24hr/rinex",
            "ELEY00ISL_R_20260600000_01D_15S_MO.crx.gz",
        )

    def test_removes_and_deindexes_actual_stored_name(self, fake_db):
        prefix = "/files/2026/mar/ELEY/15s_24hr/rinex/"
        # SELECT returns the stale lowercase straggler; DELETE returns its id.
        fake_db["cursor"] = _FakeCursor(
            [("ELEY0600.26d.Z", prefix + "ELEY0600.26d.Z")], [42]
        )
        out = rinex_index.purge_stale_siblings_batch(
            _FakeConn(),
            [self._slot()],
            ssh_target="epos@portal",
            dest_root="/mnt/epos_01/gps",
            dry_run=False,
        )
        # portal path is the stored path minus the /files/ virtual prefix
        assert fake_db["removed"] == ["2026/mar/ELEY/15s_24hr/rinex/ELEY0600.26d.Z"]
        assert out["removed"] == fake_db["removed"]
        assert out["deindexed"] == [42]

    def test_dry_run_reads_but_does_not_write(self, fake_db):
        prefix = "/files/2026/mar/ELEY/15s_24hr/rinex/"
        fake_db["cursor"] = _FakeCursor(
            [("ELEY0600.26d.Z", prefix + "ELEY0600.26d.Z")], [42]
        )
        out = rinex_index.purge_stale_siblings_batch(
            _FakeConn(),
            [self._slot()],
            ssh_target="epos@portal",
            dest_root="/mnt/epos_01/gps",
            dry_run=True,
        )
        assert fake_db["rm_kwargs"]["execute"] is False
        assert out["would_remove"]
        assert out["deindexed"] == []  # no DB writes on dry-run

    def test_empty_slots_noop(self, fake_db):
        out = rinex_index.purge_stale_siblings_batch(
            _FakeConn(), [], ssh_target="x", dest_root="y", dry_run=False
        )
        assert out == {
            "removed": [],
            "would_remove": [],
            "skipped": [],
            "deindexed": [],
        }
