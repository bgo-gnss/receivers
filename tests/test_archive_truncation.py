"""archive-truncation: detection, the decision ladder, salvage, and apply.

The 2026-11-16 incident: four raw hours (SVIN, ENTC, VONC, SLEC) were archived
with broken gzip streams. Every downstream check looks only at size and magic
bytes, so they counted as present and were never fetched again.

These tests drive the real chain — ``build_plan`` (filesystem + catalog facts
→ decisions) then ``apply_plan`` (re-check → local write → the REAL remote swap
script under bash → read-back → reindex/ledger/RINEX hooks). Only the edges are
faked: the database cursor, the ssh hop (the script runs locally), the
downloader, the reindexer and the RINEX converter.
"""

from __future__ import annotations

import binascii
import gzip
import hashlib
import os
import struct
import subprocess
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from receivers.archive import truncation_apply as ta
from receivers.archive.truncation import (
    Action,
    GzipStatus,
    ReceiverState,
    decide,
    probe_gzip,
    salvage,
    sbf_complete_prefix,
)
from receivers.archive.truncation_scan import Plan, build_plan, receiver_state

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
REL_H = "2026/oct/VONC/1Hz_1hr/raw/VONC202610221700b.sbf.gz"


# ---------------------------------------------------------------------------
# Synthetic SBF
# ---------------------------------------------------------------------------


def sbf_block(block_id: int, payload: bytes) -> bytes:
    body_len = 8 + len(payload)
    pad = (-body_len) % 4
    payload += b"\0" * pad
    length = 8 + len(payload)
    body = struct.pack("<HH", block_id, length) + payload
    crc = binascii.crc_hqx(body, 0)
    return b"$@" + struct.pack("<H", crc) + body


def sbf_stream(n: int, seed: int = 0) -> bytes:
    return b"".join(
        sbf_block(4027, hashlib.sha256(f"{seed}-{i}".encode()).digest() * 3)
        for i in range(n)
    )


def write_gz(path: Path, raw: bytes, *, cut: float | None = None) -> bytes:
    data = gzip.compress(raw, mtime=0)
    if cut is not None:
        data = data[: int(len(data) * cut)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# probe_gzip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_classifies_every_state(tmp_path):
    raw = os.urandom(300_000)
    ok = tmp_path / "ok.gz"
    write_gz(ok, raw)
    tr = tmp_path / "tr.gz"
    write_gz(tr, raw, cut=0.6)
    bad = tmp_path / "bad.gz"
    b = bytearray(gzip.compress(raw, mtime=0))
    b[-6] ^= 0xFF  # break the CRC32 trailer
    bad.write_bytes(bytes(b))
    plain = tmp_path / "plain.gz"
    plain.write_bytes(b"not gzip at all")
    empty = tmp_path / "empty.gz"
    empty.write_bytes(b"")

    p = probe_gzip(ok)
    assert p.status == GzipStatus.COMPLETE
    assert p.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert p.readable_bytes == len(raw)
    t = probe_gzip(tr)
    assert t.status == GzipStatus.TRUNCATED and 0 < t.readable_bytes < len(raw)
    assert t.content_sha256 is None
    assert probe_gzip(bad).status == GzipStatus.CORRUPT
    assert probe_gzip(plain).status == GzipStatus.NOT_GZIP
    assert probe_gzip(empty).status == GzipStatus.EMPTY
    assert probe_gzip(tmp_path / "nope.gz").status == GzipStatus.MISSING


@pytest.mark.unit
def test_probe_agrees_with_the_catalog_hash(tmp_path):
    """The catalog's content_sha256 is the identity the ladder compares."""
    from receivers.utils.content_hash import content_sha256

    f = tmp_path / "a.sbf.gz"
    write_gz(f, sbf_stream(50))
    assert probe_gzip(f).content_sha256 == content_sha256(f)


@pytest.mark.unit
def test_probe_follows_multi_member_streams(tmp_path):
    f = tmp_path / "m.gz"
    f.write_bytes(gzip.compress(b"abc" * 1000) + gzip.compress(b"xyz" * 1000))
    p = probe_gzip(f)
    assert p.status == GzipStatus.COMPLETE and p.readable_bytes == 6000


# ---------------------------------------------------------------------------
# SBF walk + salvage
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crc_matches_the_tostools_walker():
    from tostools.ntrip_sourcetable import count_sbf_blocks, crc16_ccitt

    data = os.urandom(200)
    assert binascii.crc_hqx(data, 0) == crc16_ccitt(data)
    buf = sbf_stream(20)
    assert sbf_complete_prefix(buf).blocks == count_sbf_blocks(buf) == 20


@pytest.mark.unit
def test_walk_reaches_the_exact_end_of_a_complete_stream():
    buf = sbf_stream(100)
    w = sbf_complete_prefix(buf)
    assert (w.end, w.blocks, w.stop, w.tail) == (len(buf), 100, "end", 0)


@pytest.mark.unit
def test_walk_stops_at_the_partial_last_block():
    buf = sbf_stream(100)
    blk = len(sbf_stream(1))
    w = sbf_complete_prefix(buf[: 60 * blk + 17])
    assert (w.end, w.blocks, w.stop) == (60 * blk, 60, "partial_block")


@pytest.mark.unit
def test_walk_never_resynchronises_past_a_bad_block():
    good = sbf_stream(10)
    blk = len(sbf_stream(1))
    damaged = bytearray(good)
    damaged[3 * blk + 12] ^= 0x01  # payload byte of block 4 -> CRC fails
    w = sbf_complete_prefix(bytes(damaged))
    assert (w.blocks, w.stop, w.end) == (3, "bad_crc", 3 * blk)


@pytest.mark.unit
def test_salvage_is_deterministic_and_keeps_only_whole_blocks(tmp_path):
    raw = sbf_stream(2000)
    f = tmp_path / "VONC202610221700b.sbf.gz"
    write_gz(f, raw, cut=0.55)
    a, b = salvage(f), salvage(f)
    assert a is not None and a.data == b.data and a.content_sha256 == b.content_sha256
    kept = gzip.decompress(a.data)
    assert raw.startswith(kept)
    assert sbf_complete_prefix(kept).stop == "end"
    assert a.kept_bytes == len(kept) and a.blocks * len(sbf_stream(1)) == len(kept)
    # The salvaged file is a COMPLETE gzip stream whose content hash is the identity.
    g = tmp_path / "out.sbf.gz"
    g.write_bytes(a.data)
    assert probe_gzip(g).content_sha256 == a.content_sha256


@pytest.mark.unit
def test_salvage_refuses_complete_and_non_sbf_files(tmp_path):
    ok = tmp_path / "X.sbf.gz"
    write_gz(ok, sbf_stream(10))
    assert salvage(ok) is None
    t02 = tmp_path / "ALHV0410.26.T02.gz"
    write_gz(t02, sbf_stream(100), cut=0.5)
    assert salvage(t02) is None


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def _p(status, sha=None, n=0):
    from receivers.archive.truncation import GzipProbe

    return GzipProbe(GzipStatus(status), size=1, content_sha256=sha, readable_bytes=n)


SBF = "VONC202610221700b.sbf.gz"


@pytest.mark.unit
@pytest.mark.parametrize(
    "copies, cat, recv, blocks, action, source",
    [
        # (a) a complete copy wins over everything, archive preferred
        (
            {"archive": _p("truncated"), "local": _p("complete", "S")},
            None,
            "gone",
            5,
            Action.REPLACE,
            "local",
        ),
        (
            {"archive": _p("complete", "S"), "local": _p("truncated")},
            "S",
            "holds",
            5,
            Action.REPLACE,
            "archive",
        ),
        # a complete copy that contradicts the catalog is a human's call
        (
            {"archive": _p("truncated"), "local": _p("complete", "S")},
            "OTHER",
            "gone",
            5,
            Action.CONFLICT,
            "local",
        ),
        # (b) receiver still holds it
        (
            {"archive": _p("truncated"), "local": None},
            None,
            "holds",
            5,
            Action.REFETCH,
            None,
        ),
        # unknown retention defers — even with salvageable blocks
        (
            {"archive": _p("truncated"), "local": _p("truncated")},
            None,
            "unknown",
            5,
            Action.DEFERRED,
            None,
        ),
        # (c) gone: salvage
        (
            {"archive": _p("truncated"), "local": None},
            None,
            "gone",
            5,
            Action.SALVAGE,
            "archive",
        ),
        # (d) gone and nothing decodes
        (
            {"archive": _p("corrupt"), "local": None},
            None,
            "gone",
            0,
            Action.UNRECOVERABLE,
            None,
        ),
        # nothing broken
        (
            {"archive": _p("complete", "S"), "local": None},
            None,
            "gone",
            0,
            Action.CONFLICT,
            None,
        ),
    ],
)
def test_ladder(copies, cat, recv, blocks, action, source):
    d = decide(
        name=SBF,
        copies=copies,
        catalog_content_sha256=cat,
        receiver=ReceiverState(recv),
        salvage_source="archive" if blocks else None,
        salvage_blocks=blocks,
    )
    assert d.action == action
    assert d.source == source


@pytest.mark.unit
def test_ladder_reports_unwalkable_formats_unsupported():
    d = decide(
        name="ALHV0410.26.T02.gz",
        copies={"archive": _p("truncated"), "local": None},
        catalog_content_sha256=None,
        receiver=ReceiverState.GONE,
        salvage_source=None,
        salvage_blocks=0,
    )
    assert d.action == Action.UNSUPPORTED


@pytest.mark.unit
def test_salvage_and_refetch_target_both_copies_replace_only_the_broken():
    gone = decide(name=SBF, copies={"archive": _p("truncated"), "local": _p("truncated")},
                  catalog_content_sha256=None, receiver=ReceiverState.GONE,
                  salvage_source="local", salvage_blocks=3)  # fmt: skip
    assert gone.targets == ("archive", "local")
    rep = decide(name=SBF, copies={"archive": _p("truncated"), "local": _p("complete", "S")},
                 catalog_content_sha256=None, receiver=ReceiverState.GONE,
                 salvage_source=None, salvage_blocks=0)  # fmt: skip
    assert rep.targets == ("archive",)


# ---------------------------------------------------------------------------
# Receiver facts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_receiver_state_from_horizon_ledger_and_staleness():
    h = {("VONC", "1Hz_1hr"): (date(2026, 10, 26), 1, NOW - timedelta(hours=1))}
    kw = dict(rel=REL_H, station="VONC", session="1Hz_1hr", horizons=h,
              not_on_receiver=set(), now=NOW, max_age=timedelta(hours=48))  # fmt: skip
    assert (
        receiver_state(file_date=date(2026, 10, 22), file_hour=17, **kw)["state"]
        == "gone"
    )
    assert (
        receiver_state(file_date=date(2026, 10, 26), file_hour=0, **kw)["state"]
        == "gone"
    )
    assert (
        receiver_state(file_date=date(2026, 10, 26), file_hour=1, **kw)["state"]
        == "holds"
    )
    stale = {("VONC", "1Hz_1hr"): (date(2026, 10, 1), 1, NOW - timedelta(days=5))}
    assert receiver_state(file_date=date(2026, 10, 26), file_hour=5,
                          **{**kw, "horizons": stale})["state"] == "unknown"  # fmt: skip
    assert receiver_state(file_date=date(2026, 10, 26), file_hour=5,
                          **{**kw, "horizons": {}})["state"] == "unknown"  # fmt: skip
    # the ledger is keyed by path: another path in it does not affect this one
    assert receiver_state(file_date=date(2026, 10, 30), file_hour=5,
                          **{**kw, "not_on_receiver": {"other/path"}})["state"] == "holds"  # fmt: skip
    # A not-found answer NEVER makes a file GONE (receivers answer 550 for
    # files they hold): with the horizon still covering it, it is UNKNOWN...
    assert receiver_state(file_date=date(2026, 10, 30), file_hour=5,
                          **{**kw, "not_on_receiver": {REL_H}})["state"] == "unknown"  # fmt: skip
    # ...without a horizon it stays UNKNOWN...
    assert receiver_state(file_date=date(2026, 10, 22), file_hour=17,
                          **{**kw, "horizons": {}, "not_on_receiver": {REL_H}})["state"] == "unknown"  # fmt: skip
    # ...and only the horizon passing the period makes it GONE.
    assert receiver_state(file_date=date(2026, 10, 22), file_hour=17,
                          **{**kw, "not_on_receiver": {REL_H}})["state"] == "gone"  # fmt: skip


# ---------------------------------------------------------------------------
# build_plan end to end (fake DB cursor, real filesystem)
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self, catalog=(), horizons=(), not_on_receiver=()):
        self.catalog, self.horizons, self.gone = (
            list(catalog),
            list(horizons),
            list(not_on_receiver),
        )

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                if "archive_catalog" in sql:
                    self.rows = conn.catalog
                elif "receiver_horizon" in sql:
                    self.rows = conn.horizons
                else:
                    self.rows = [(r,) for r in conn.gone]

            def fetchall(self):
                return self.rows

        return Cur()

    def rollback(self):
        pass


def _age(path: Path, hours: float = 24) -> None:
    t = (datetime.now() - timedelta(hours=hours)).timestamp()
    os.utime(path, (t, t))


def _roots(tmp_path):
    a, loc = tmp_path / "archive", tmp_path / "local"
    a.mkdir()
    loc.mkdir()
    return a, loc


def _plan(conn, a, loc, **kw):
    return build_plan(conn, local_root=str(loc), archive_root=str(a),
                      start=date(2026, 10, 1), end=date(2026, 10, 31), now=NOW, **kw)  # fmt: skip


@pytest.mark.unit
def test_plan_salvages_a_gone_hour_and_skips_cataloged_files(tmp_path):
    a, loc = _roots(tmp_path)
    raw = sbf_stream(3000)
    for root in (a, loc):
        write_gz(root / REL_H, raw, cut=0.6)
        _age(root / REL_H)
    # A cataloged file whose size matches its row is NOT probed: make it
    # corrupt to prove the cheap candidate set is what selects work.
    rel_ok = "2026/oct/VONC/1Hz_1hr/raw/VONC202610221600b.sbf.gz"
    write_gz(a / rel_ok, raw, cut=0.5)
    _age(a / rel_ok)
    cat = [
        ("VONC", "1Hz_1hr", "vonc202610221600b.sbf", (a / rel_ok).stat().st_size, "X")
    ]
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn(cat, hz), a, loc)

    assert [i.rel for i in plan.items] == [REL_H]
    it = plan.items[0]
    assert it.action == "salvage" and it.receiver["state"] == "gone"
    assert it.decision["targets"] == ["archive", "local"]
    assert plan.stats["probed"] == 1


@pytest.mark.unit
def test_plan_defers_without_a_horizon_and_skips_settling_files(tmp_path):
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(500), cut=0.6)
    _age(a / REL_H)
    rel_new = "2026/oct/VONC/1Hz_1hr/raw/VONC202610231700b.sbf.gz"
    write_gz(a / rel_new, sbf_stream(500), cut=0.6)  # fresh mtime: in flight
    plan = _plan(FakeConn(), a, loc)
    assert [(i.rel, i.action) for i in plan.items] == [(REL_H, "deferred")]
    assert plan.stats["skipped_settling"] == 1


@pytest.mark.unit
def test_plan_round_trips_through_json(tmp_path):
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(500), cut=0.6)
    _age(a / REL_H)
    plan = _plan(FakeConn(), a, loc)
    f = tmp_path / "plan.json"
    f.write_text(plan.to_json())
    again = Plan.load(str(f))
    assert [i.__dict__ for i in again.items] == [i.__dict__ for i in plan.items]


# ---------------------------------------------------------------------------
# apply_plan — the real remote script, run locally instead of over ssh
# ---------------------------------------------------------------------------


def local_swapper(remote_root: Path):
    """``push_and_swap`` with rsync+ssh replaced by a local copy + local bash."""

    def swap(swaps, cfg, work_dir):
        args = []
        for s in swaps:
            dst = remote_root / (s.item.rel + ta.TMP_SUFFIX)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(Path(s.staged).read_bytes())
            args += [s.item.rel, s.old_csha or "-", s.new_csha]
        q = str(remote_root.parent / "quarantine" / cfg.run_id)
        proc = subprocess.run(["bash", "-s", "--", str(remote_root), q, *args],
                              input=ta._REMOTE_SWAP, capture_output=True, text=True)  # fmt: skip
        out = {}
        for line in proc.stdout.splitlines():
            st, rel, detail = line.split("|", 2)
            out[rel] = (st, detail)
        return out

    return swap


class Recorder:
    def __init__(self):
        self.reindexed, self.rinexed, self.ledger, self.downloads = [], [], [], []
        self.tracked = []


def _apply(plan, tmp_path, rec, *, downloader=None, swapper=None, salvaged=frozenset()):
    cfg = ta.ApplyConfig(
        ssh_target="gpsops@rawdata", dest_root=plan.archive_root, catalog_hosts=["h1", "h2"],
        local_quarantine=str(tmp_path / "lq"), readback_tries=1, readback_wait_s=0,
    )  # fmt: skip
    return (
        ta.apply_plan(
            plan,
            cfg,
            execute=True,
            downloader=downloader
            or (lambda it: rec.downloads.append(it.rel) or "unreachable (ping failed)"),
            swapper=swapper or local_swapper(Path(plan.archive_root)),
            reindexer=lambda c, files, root: rec.reindexed.extend(files)
            or {"h1": object(), "h2": object()},
            rinexer=lambda it: rec.rinexed.append(
                (it.station, it.session_type, it.file_date)
            ),
            ledger=lambda hosts, rows: rec.ledger.extend(rows) or {},
            already_recorded=set(),
            salvaged=salvaged,
            ledger_check=lambda hosts: [],
            tracker=lambda it, size: rec.tracked.append((it.rel, size)) or True,
        ),
        cfg,
    )


@pytest.mark.unit
def test_apply_salvage_rewrites_both_copies_without_a_hole(tmp_path):
    a, loc = _roots(tmp_path)
    raw = sbf_stream(3000)
    for root in (a, loc):
        write_gz(root / REL_H, raw, cut=0.6)
        _age(root / REL_H)
    old_archive = (a / REL_H).read_bytes()
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    rec = Recorder()
    res, cfg = _apply(plan, tmp_path, rec)

    assert [(r.rel, r.outcome) for r in res.results] == [(REL_H, "repaired")]
    for root in (a, loc):
        p = probe_gzip(root / REL_H)
        assert p.status == GzipStatus.COMPLETE
        assert p.content_sha256 == plan.items[0].salvage["content_sha256"]
    # old copies are quarantined, not lost
    assert (tmp_path / "quarantine" / cfg.run_id / REL_H).read_bytes() == old_archive
    assert (tmp_path / "lq" / cfg.run_id / REL_H).exists()
    assert len(rec.reindexed) == 1 and rec.reindexed[0].endswith(REL_H)
    assert rec.rinexed == [("VONC", "1Hz_1hr", "2026-10-22")]
    (row,) = rec.ledger
    assert row[6:9] == ("salvage", "repaired", True)  # action, outcome, is_partial
    assert not list(a.rglob("*.repair-tmp"))


@pytest.mark.unit
def test_apply_refuses_when_a_copy_changed_after_the_plan(tmp_path):
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.7)  # someone touched it
    before = (a / REL_H).read_bytes()
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec)
    assert [r.outcome for r in res.results] == ["refused"]
    assert (a / REL_H).read_bytes() == before
    assert not rec.reindexed and not rec.rinexed


@pytest.mark.unit
def test_apply_replace_from_the_complete_local_copy(tmp_path):
    a, loc = _roots(tmp_path)
    raw = sbf_stream(3000)
    write_gz(a / REL_H, raw, cut=0.4)
    good = write_gz(loc / REL_H, raw)
    for root in (a, loc):
        _age(root / REL_H)
    plan = _plan(FakeConn(), a, loc)
    assert plan.items[0].action == "replace"
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec)
    assert [r.outcome for r in res.results] == ["repaired"]
    assert (a / REL_H).read_bytes() == good
    (row,) = rec.ledger
    assert row[8] is False  # not partial


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer, first",
    [
        ("Station VONC is unreachable (ping failed)", "unreachable"),
        ("Remote file X not found on server", "not_on_receiver"),
        ("Login failed: 530 Login incorrect", "failed"),
    ],
)
def test_refetch_tries_each_station_once_and_restores_on_failure(
    tmp_path, answer, first
):
    a, loc = _roots(tmp_path)
    rels = [f"2026/oct/VONC/1Hz_1hr/raw/VONC2026102{d}1700b.sbf.gz" for d in (7, 8)]
    for rel in rels:
        for root in (a, loc):
            write_gz(root / rel, sbf_stream(500), cut=0.6)
            _age(root / rel)
    local_before = {rel: (loc / rel).read_bytes() for rel in rels}
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    assert {i.action for i in plan.items} == {"refetch"}
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec,
                    downloader=lambda it: rec.downloads.append(it.rel) or answer)  # fmt: skip
    assert len(rec.downloads) == 1, "after any failed contact the station is left alone"
    assert sorted(r.outcome for r in res.results) == sorted(["deferred", first])
    for rel in rels:
        assert (loc / rel).read_bytes() == local_before[rel]


@pytest.mark.unit
def test_refetch_success_replaces_the_archive_from_the_new_download(tmp_path):
    a, loc = _roots(tmp_path)
    raw = sbf_stream(800)
    rel = "2026/oct/VONC/1Hz_1hr/raw/VONC202610271700b.sbf.gz"
    for root in (a, loc):
        write_gz(root / rel, raw, cut=0.6)
        _age(root / rel)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)

    def download(it):
        assert not (
            loc / it.rel
        ).exists(), "the downloader must see the period as missing"
        write_gz(loc / it.rel, raw)
        return "Archived to ..."

    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec, downloader=download)
    assert [r.outcome for r in res.results] == ["repaired"]
    assert gzip.decompress((a / rel).read_bytes()) == raw


@pytest.mark.unit
def test_not_found_on_receiver_is_recorded_for_the_next_plan(tmp_path):
    a, loc = _roots(tmp_path)
    rel = "2026/oct/VONC/1Hz_1hr/raw/VONC202610271700b.sbf.gz"
    write_gz(a / rel, sbf_stream(800), cut=0.6)
    _age(a / rel)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec,
                    downloader=lambda it: "Remote file X not found on server")  # fmt: skip
    assert [r.outcome for r in res.results] == ["not_on_receiver"]
    assert rec.ledger[0][7] == "not_on_receiver"
    # The next plan DEFERS (no second login, no salvage) while the horizon
    # still covers the hour...
    nxt = _plan(FakeConn([], hz, [rel]), a, loc)
    assert nxt.items[0].action == "deferred"
    # ...and salvages only once the receiver's horizon has passed it.
    later = [("VONC", "1Hz_1hr", date(2026, 10, 29), 1, NOW - timedelta(hours=2))]
    assert _plan(FakeConn([], later, [rel]), a, loc).items[0].action == "salvage"


@pytest.mark.unit
def test_dry_run_writes_nothing(tmp_path):
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    before = (a / REL_H).read_bytes()
    cfg = ta.ApplyConfig(ssh_target="x", dest_root=str(a), catalog_hosts=[],
                         local_quarantine=str(tmp_path / "lq"))  # fmt: skip
    res = ta.apply_plan(plan, cfg, execute=False,
                        swapper=lambda *a: pytest.fail("dry-run swapped"))  # fmt: skip
    assert [r.outcome for r in res.results] == ["would_apply"]
    assert (a / REL_H).read_bytes() == before


# ---------------------------------------------------------------------------
# The remote swap script on its own
# ---------------------------------------------------------------------------


def _run_swap(root: Path, q: Path, *triples):
    proc = subprocess.run(["bash", "-s", "--", str(root), str(q), *triples],
                          input=ta._REMOTE_SWAP, capture_output=True, text=True)  # fmt: skip
    return [line.split("|")[0] for line in proc.stdout.splitlines()]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.mark.unit
def test_remote_swap_refuses_changed_files_and_is_idempotent(tmp_path):
    root, q = tmp_path / "r", tmp_path / "q"
    f = root / "x.gz"
    f.parent.mkdir()
    f.write_bytes(b"OLD")
    Path(str(f) + ".repair-tmp").write_bytes(b"NEW")
    # the plan saw different bytes -> refused, tmp removed, file untouched
    assert _run_swap(root, q, "x.gz", _sha(b"SOMETHING ELSE"), _sha(b"NEW")) == [
        "REFUSED"
    ]
    assert f.read_bytes() == b"OLD" and not Path(str(f) + ".repair-tmp").exists()
    # a corrupted upload is caught by its sha
    Path(str(f) + ".repair-tmp").write_bytes(b"NEW-but-mangled")
    assert _run_swap(root, q, "x.gz", _sha(b"OLD"), _sha(b"NEW")) == ["FAIL"]
    # the real swap
    Path(str(f) + ".repair-tmp").write_bytes(b"NEW")
    assert _run_swap(root, q, "x.gz", _sha(b"OLD"), _sha(b"NEW")) == ["REPLACED"]
    assert f.read_bytes() == b"NEW" and (q / "x.gz").read_bytes() == b"OLD"
    # rerun after success: already the new file
    Path(str(f) + ".repair-tmp").write_bytes(b"NEW")
    assert _run_swap(root, q, "x.gz", _sha(b"OLD"), _sha(b"NEW")) == ["REPLACED"]
    # a missing archive copy is inserted, but not over a file that appeared
    g = root / "y.gz"
    Path(str(g) + ".repair-tmp").write_bytes(b"NEW")
    assert _run_swap(root, q, "y.gz", "-", _sha(b"NEW")) == ["REPLACED"]
    assert g.read_bytes() == b"NEW"


@pytest.mark.unit
def test_apply_refuses_a_salvage_that_does_not_reproduce_the_plan(
    tmp_path, monkeypatch
):
    """Determinism guard: if salvage gives different content than at plan time
    (other code version, other walker), nothing is written."""
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    before = (a / REL_H).read_bytes()
    real = ta.salvage

    def drifted(path):
        s = real(path)
        from dataclasses import replace

        return replace(s, content_sha256="0" * 64)

    monkeypatch.setattr(ta, "salvage", drifted)
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec)
    assert [r.outcome for r in res.results] == ["failed"]
    assert "reproduce" in res.results[0].detail
    assert (a / REL_H).read_bytes() == before and not rec.reindexed


@pytest.mark.unit
def test_push_and_swap_builds_a_working_rsync_and_ssh_call(tmp_path, monkeypatch):
    """The real push_and_swap, with only the host hop removed: the rsync runs
    locally and the ssh command's remote part runs under local bash."""
    import shutil as _sh

    if not _sh.which("rsync"):
        pytest.skip("rsync not installed")
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    real_run = subprocess.run
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "rsync":
            cmd = [c.replace("gpsops@rawdata:", "") for c in cmd]
        elif cmd[0] == "ssh":
            assert cmd[1:5] == ["-o", "BatchMode=yes", "gpsops@rawdata", "bash"]
            cmd = cmd[4:]
        return real_run(cmd, **kw)

    monkeypatch.setattr(ta.subprocess, "run", run)
    cfg = ta.ApplyConfig(
        ssh_target="gpsops@rawdata", dest_root=str(a), catalog_hosts=["h1"],
        local_quarantine=str(tmp_path / "lq"),
        archive_quarantine=str(tmp_path / "aq"), readback_tries=1, readback_wait_s=0,
    )  # fmt: skip
    res = ta.apply_plan(
        plan, cfg, execute=True,
        reindexer=lambda c, f, r: {"h1": object()},
        rinexer=lambda it: None,
        ledger=lambda h, rows: {},
        already_recorded=set(),
        salvaged=frozenset(),
        ledger_check=lambda hosts: [],
        tracker=lambda it, size: True,
    )  # fmt: skip
    assert calls == ["rsync", "ssh"], "one rsync + one ssh per batch"
    assert [r.outcome for r in res.results] == ["repaired"]
    assert probe_gzip(a / REL_H).status == GzipStatus.COMPLETE
    assert (tmp_path / "aq" / cfg.run_id / REL_H).exists()


@pytest.mark.unit
def test_classify_download_output_does_not_read_byte_counts_as_550():
    assert ta.classify_download_output("Archived 2,776,550 bytes") == "failed"
    assert (
        ta.classify_download_output("Remote file X not found on server")
        == "not_on_receiver"
    )


@pytest.mark.unit
def test_apply_refuses_without_the_ledger_on_every_catalog_host(tmp_path):
    """A salvage whose ledger row cannot be written would land unmarked."""
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    before = (a / REL_H).read_bytes()
    cfg = ta.ApplyConfig(ssh_target="x", dest_root=str(a), catalog_hosts=["h1", "h2"],
                         local_quarantine=str(tmp_path / "lq"))  # fmt: skip
    with pytest.raises(RuntimeError, match="h2"):
        ta.apply_plan(plan, cfg, execute=True, ledger_check=lambda hosts: ["h2"],
                      swapper=lambda *x: pytest.fail("wrote without a ledger"))  # fmt: skip
    assert (a / REL_H).read_bytes() == before


@pytest.mark.unit
def test_partial_marker_survives_a_failed_archive_swap(tmp_path):
    """Salvage writes local, the swap fails; the next run REPLACEs the archive
    from that local copy and must still record the content as partial."""
    a, loc = _roots(tmp_path)
    for root in (a, loc):
        write_gz(root / REL_H, sbf_stream(3000), cut=0.6)
        _age(root / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    salvaged_sha = plan.items[0].salvage["content_sha256"]
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec,
                    swapper=lambda swaps, cfg, wd: {s.item.rel: ("FAIL", "gateway down") for s in swaps})  # fmt: skip
    assert [r.outcome for r in res.results] == ["failed"]
    assert rec.ledger[0][17] == salvaged_sha  # the attempted content is recorded
    _age(loc / REL_H)

    plan2 = _plan(FakeConn([], hz), a, loc)
    assert (
        plan2.items[0].action == "replace"
        and plan2.items[0].decision["source"] == "local"
    )
    rec2 = Recorder()
    known = frozenset(r[17] for r in rec.ledger if r[6] == "salvage" and r[17])
    res2, _ = _apply(plan2, tmp_path, rec2, salvaged=known)
    assert [r.outcome for r in res2.results] == ["repaired"]
    (row,) = rec2.ledger
    assert row[6:9] == ("replace", "repaired", True)


@pytest.mark.unit
def test_archive_only_salvage_also_writes_local_for_rinex(tmp_path):
    a, loc = _roots(tmp_path)
    write_gz(a / REL_H, sbf_stream(3000), cut=0.6)
    _age(a / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    assert plan.items[0].decision["targets"] == ["archive", "local"]
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec)
    assert [r.outcome for r in res.results] == ["repaired"]
    assert probe_gzip(loc / REL_H).status == GzipStatus.COMPLETE
    assert rec.rinexed == [("VONC", "1Hz_1hr", "2026-10-22")]


@pytest.mark.unit
def test_repaired_files_are_marked_present_with_their_new_size(tmp_path):
    """file_tracking/local_raw go through the download seam: a stale
    'missing'/'suspect' row and the old size are replaced."""
    a, loc = _roots(tmp_path)
    for root in (a, loc):
        write_gz(root / REL_H, sbf_stream(3000), cut=0.6)
        _age(root / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    rec = Recorder()
    res, _ = _apply(plan, tmp_path, rec)
    assert [r.outcome for r in res.results] == ["repaired"]
    assert rec.tracked == [(REL_H, (loc / REL_H).stat().st_size)]


@pytest.mark.unit
def test_a_failed_repair_is_not_marked_present(tmp_path):
    a, loc = _roots(tmp_path)
    for root in (a, loc):
        write_gz(root / REL_H, sbf_stream(3000), cut=0.6)
        _age(root / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    rec = Recorder()
    _apply(plan, tmp_path, rec,
           swapper=lambda sw, cfg, wd: {x.item.rel: ("FAIL", "down") for x in sw})  # fmt: skip
    assert rec.tracked == []


@pytest.mark.unit
def test_refetch_ledger_row_carries_no_salvage_figures(tmp_path):
    a, loc = _roots(tmp_path)
    raw = sbf_stream(800)
    rel = "2026/oct/VONC/1Hz_1hr/raw/VONC202610271700b.sbf.gz"
    for root in (a, loc):
        write_gz(root / rel, raw, cut=0.6)
        _age(root / rel)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    assert plan.items[0].salvage is not None  # the plan-time estimate exists
    rec = Recorder()
    _apply(
        plan, tmp_path, rec, downloader=lambda it: write_gz(loc / it.rel, raw) and "ok"
    )
    (row,) = rec.ledger
    assert row[6:8] == ("refetch", "repaired")
    assert row[10:13] == (None, None, None)  # readable_bytes, kept_bytes, blocks


@pytest.mark.unit
def test_unwritable_local_quarantine_refuses_before_any_write(tmp_path):
    a, loc = _roots(tmp_path)
    for root in (a, loc):
        write_gz(root / REL_H, sbf_stream(3000), cut=0.6)
        _age(root / REL_H)
    hz = [("VONC", "1Hz_1hr", date(2026, 10, 26), 1, NOW - timedelta(hours=2))]
    plan = _plan(FakeConn([], hz), a, loc)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # a FILE where the quarantine dir would go
    before = {r: (r / REL_H).read_bytes() for r in (a, loc)}
    cfg = ta.ApplyConfig(ssh_target="x", dest_root=str(a), catalog_hosts=["h1"],
                         local_quarantine=str(blocker / "q"))  # fmt: skip
    with pytest.raises(RuntimeError, match="not writable"):
        ta.apply_plan(plan, cfg, execute=True, ledger_check=lambda h: [],
                      swapper=lambda *x: pytest.fail("wrote"),
                      ledger=lambda *x: pytest.fail("ledger written"))  # fmt: skip
    assert {r: (r / REL_H).read_bytes() for r in (a, loc)} == before
    # the dry-run reports it too
    res = ta.apply_plan(plan, cfg, execute=False)
    assert [r.outcome for r in res.results] == ["refused"]
    assert "not writable" in res.results[0].detail


@pytest.mark.unit
def test_default_local_quarantine_is_under_the_user_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert ta.default_local_quarantine() == str(
        tmp_path / "gps_receivers" / "truncation_quarantine"
    )
    assert ta.preflight_local_quarantine(ta.default_local_quarantine()) is None


@pytest.mark.unit
def test_default_tracker_calls_the_download_seam(monkeypatch):
    import receivers.health.file_tracker as ftmod

    calls = []

    class FakeFT:
        def mark_file_archived(self, *a, **k):
            calls.append(a)
            return True

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(ftmod, "FileTracker", FakeFT)
    from receivers.archive.truncation_scan import Item

    it = Item(rel=REL_H, station="VONC", session_type="1Hz_1hr", file_date="2026-10-22",
              file_hour=17, copies={}, catalog=None, receiver={}, salvage=None,
              decision={"action": "salvage"})  # fmt: skip
    assert ta._default_track(it, 2232169) is True
    assert calls == [
        (
            "VONC",
            "1Hz_1hr",
            date(2026, 10, 22),
            17,
            "VONC202610221700b.sbf.gz",
            2232169,
        ),
        "closed",
    ]
