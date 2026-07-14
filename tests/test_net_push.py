"""Tests for the shared incremental batched-push utility (utils.net_push).

Offline: rsync to a LOCAL destination (no SSH), real temp files.
"""

from __future__ import annotations

import shutil

import pytest

from receivers.utils.net_push import BatchPush, rsync_tree

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available"
)


def test_rsync_tree_preserves_structure(tmp_path):
    src = tmp_path / "src"
    (src / "2011/jun/RHOF/15s_24hr/rinex").mkdir(parents=True)
    (src / "2011/jun/RHOF/15s_24hr/rinex" / "RHOF1790.11D.Z").write_text("obs")
    dest = tmp_path / "dest"
    stats = rsync_tree(src, str(dest))
    landed = dest / "2011/jun/RHOF/15s_24hr/rinex" / "RHOF1790.11D.Z"
    assert landed.read_text() == "obs"
    assert stats["transferred"] >= 1
    assert stats["rc"] == 0


def test_rsync_tree_pins_perms_dir_755_file_644(tmp_path):
    # Regression: rsync -a alone stamps the destination with the SOURCE (umask-
    # dependent) perms, so the portal dirs/files drifted (664 files, 775 dirs from
    # 0002-umask runs). --chmod=D755,F644 must pin them regardless of source mode.
    import os
    import stat

    src = tmp_path / "src"
    d = src / "2011/jun/RHOF/15s_24hr/rinex"
    d.mkdir(parents=True)
    f = d / "RHOF1790.11D.Z"
    f.write_text("obs")
    os.chmod(d, 0o775)  # a 0002-umask-style dir
    os.chmod(f, 0o664)  # a 0002-umask-style file

    dest = tmp_path / "dest"
    rsync_tree(src, str(dest))

    landed_dir = dest / "2011/jun/RHOF/15s_24hr/rinex"
    landed_file = landed_dir / "RHOF1790.11D.Z"
    assert stat.S_IMODE(landed_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(landed_file.stat().st_mode) == 0o644


def test_batchpush_flushes_every_n_then_remainder(tmp_path):
    dest = tmp_path / "dest"
    files = tmp_path / "files"
    files.mkdir()
    flushed: list = []

    def on_flush(refs, stats):
        flushed.append(list(refs))

    with BatchPush(str(dest), flush_every=3, on_flush=on_flush) as bp:
        for i in range(7):
            f = files / f"f{i}"
            f.write_text(str(i))
            bp.add(f, f"2011/m{i % 2}/RHOF/15s_24hr/rinex", f"F{i}.Z", ref=i)

    # 7 files, flush_every=3 → batches [0,1,2], [3,4,5], then close flushes [6]
    assert [len(b) for b in flushed] == [3, 3, 1]
    assert flushed[0] == [0, 1, 2]
    assert flushed[2] == [6]
    # every file landed at dest under its rel_dir with its published name
    assert (dest / "2011/m0/RHOF/15s_24hr/rinex" / "F0.Z").read_text() == "0"
    assert (dest / "2011/m0/RHOF/15s_24hr/rinex" / "F6.Z").read_text() == "6"
    assert (dest / "2011/m1/RHOF/15s_24hr/rinex" / "F1.Z").read_text() == "1"
    assert bp.total_staged == 7 and bp.total_flushed == 7


def test_batchpush_refs_threaded_in_order(tmp_path):
    dest = tmp_path / "dest"
    f = tmp_path / "f"
    f.write_text("x")
    seen: list = []
    with BatchPush(
        str(dest), flush_every=10, on_flush=lambda r, s: seen.extend(r)
    ) as bp:
        for name in ("a", "b", "c"):
            bp.add(f, "d", f"{name}.Z", ref=name)
    assert seen == ["a", "b", "c"]  # single flush at close, in add order


def test_batchpush_no_files_no_flush_callback(tmp_path):
    calls: list = []
    with BatchPush(
        str(tmp_path / "d"), flush_every=2, on_flush=lambda r, s: calls.append(r)
    ):
        pass  # never add anything
    assert calls == []


def test_batchpush_dry_run_transfers_nothing(tmp_path):
    dest = tmp_path / "dest"
    f = tmp_path / "f"
    f.write_text("x")
    with BatchPush(str(dest), flush_every=1, dry_run=True) as bp:
        bp.add(f, "2011/jun/RHOF/15s_24hr/rinex", "F.Z", ref=1)
    # --dry-run must not create the real destination file
    assert not (dest / "2011/jun/RHOF/15s_24hr/rinex" / "F.Z").exists()


def test_batchpush_stage_cleared_between_flushes(tmp_path):
    # A second flush must not re-send the first batch's files (stage is reset).
    dest = tmp_path / "dest"
    f = tmp_path / "f"
    f.write_text("x")
    transferred: list = []
    with BatchPush(
        str(dest),
        flush_every=1,
        on_flush=lambda r, s: transferred.append(s["transferred"]),
    ) as bp:
        bp.add(f, "d", "A.Z", ref=1)
        bp.add(f, "d", "B.Z", ref=2)
    # each flush sent exactly its own single new file, not the accumulation
    assert transferred == [1, 1]
