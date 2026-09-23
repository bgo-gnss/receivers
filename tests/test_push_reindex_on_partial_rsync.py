"""A failed rsync must still reindex the files that DID land.

Both push paths (``_flush_fixed_batch`` — the mid-run batch flush of
``--fix-headers --push`` — and ``_push_reconverted`` — the re-rinex push)
returned on a non-zero rsync exit BEFORE the catalog reindex. Files rsync had
already transferred are durable on the archive with their NEW content, so their
``archive_catalog.content_sha256`` stays at the pre-fix value forever (these are
the only two reindex call sites) and ``archive-verify`` later reports them
CORRUPT for a fix that succeeded.

The fix reindexes exactly the transferred subset (rsync -av's stdout ∩ the
requested list) with the same root/storage_location/dest_prefix the success
path uses, and NEVER runs ``--cleanup`` on that path — cleanup deletes staged
files, and the un-transferred ones must survive for the retry.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

m = importlib.import_module("receivers.cli.main")

ARCHIVE_NAME = "imo_archive"
ARCHIVE_DESTPATH = "~/gpsdata"
ARCHIVE_DEST = f"gpsops@rawdata:{ARCHIVE_DESTPATH}"

REL = [
    "2024/jan/ELDC/15s_24hr/rinex/ELDC0010.24d.Z",
    "2024/jan/ELDC/15s_24hr/rinex/ELDC0020.24d.Z",
    "2024/jan/ELDC/15s_24hr/rinex/ELDC0030.24d.Z",
    "2024/jan/ELDC/15s_24hr/rinex/ELDC0040.24d.Z",
    "2024/jan/ELDC/15s_24hr/rinex/ELDC0050.24d.Z",
]


def _rsync_stdout(transferred: list[str]) -> str:
    """What ``rsync -av --files-from`` prints: header, dirs, files, trailer."""
    lines = ["sending incremental file list", "2024/", "2024/jan/"]
    lines += transferred
    lines += ["", "sent 1,234 bytes  received 56 bytes  2,580.00 bytes/sec"]
    return "\n".join(lines) + "\n"


def _completed(rc: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["rsync"], returncode=rc, stdout=stdout, stderr="rsync error: partial"
    )


def _timeout(stdout):
    exc = subprocess.TimeoutExpired(cmd=["rsync"], timeout=3600)
    exc.stdout = stdout
    return exc


# --------------------------------------------------------------------------
# the shared helper


class TestRsyncTransferred:
    def test_intersects_stdout_with_the_request_list(self):
        out = _rsync_stdout(REL[:2])
        assert m._rsync_transferred(out, REL) == REL[:2]

    def test_header_dirs_and_trailer_never_leak(self):
        out = _rsync_stdout([])
        assert m._rsync_transferred(out, REL) == []

    def test_none_stdout_is_nothing_transferred(self):
        assert m._rsync_transferred(None, REL) == []

    def test_bytes_stdout_is_decoded(self):
        out = _rsync_stdout(REL[1:2]).encode()
        assert m._rsync_transferred(out, REL) == REL[1:2]

    def test_order_follows_the_request_list(self):
        out = _rsync_stdout([REL[3], REL[0]])
        assert m._rsync_transferred(out, REL) == [REL[0], REL[3]]


# --------------------------------------------------------------------------
# site 1: _flush_fixed_batch


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path / "rinex_fixes"


def _flush(work: Path, *, cleanup: bool = False, rel=REL):
    args = Namespace(cleanup=cleanup)
    batch_details = [{"file": str(work / r), "fixed": True} for r in rel]
    with (
        patch.object(m, "_push_reindex") as reindex,
        patch.object(m, "_push_cleanup") as clean,
    ):
        yield_ = m._flush_fixed_batch(
            batch_details,
            args=args,
            work_dir=work,
            archive_dest=ARCHIVE_DEST,
            archive_name=ARCHIVE_NAME,
            archive_destpath=ARCHIVE_DESTPATH,
            tos_cache=None,
        )
    return yield_, reindex, clean


def _reindexed_files(reindex_mock) -> list[str]:
    assert reindex_mock.call_count == 1
    (_args, files), kw = reindex_mock.call_args
    return files, kw


class TestFlushFixedBatchPartial:
    def test_rc23_reindexes_exactly_the_transferred_subset(self, work):
        landed = REL[:3]
        with patch(
            "subprocess.run", return_value=_completed(23, _rsync_stdout(landed))
        ):
            result, reindex, clean = _flush(work)

        files, kw = _reindexed_files(reindex)
        assert files == [str(work / r) for r in landed]  # 3 of 5, staged paths
        assert kw == {
            "root": str(work),
            "storage_location": ARCHIVE_NAME,
            "dest_prefix": ARCHIVE_DESTPATH,
        }
        assert result == {"pushed": 3}

    def test_partial_path_never_runs_cleanup(self, work):
        landed = REL[:2]
        with patch(
            "subprocess.run", return_value=_completed(23, _rsync_stdout(landed))
        ):
            _, _reindex, clean = _flush(work, cleanup=True)
        assert clean.call_count == 0  # un-transferred staged files must survive

    def test_reindex_paths_are_staged_not_archive(self, work):
        with patch(
            "subprocess.run", return_value=_completed(23, _rsync_stdout(REL[:1]))
        ):
            _, reindex, _ = _flush(work)
        files, _kw = _reindexed_files(reindex)
        assert all(f.startswith(str(work)) for f in files)
        assert not any(ARCHIVE_DESTPATH in f for f in files)

    def test_nothing_landed_means_no_reindex(self, work):
        with patch("subprocess.run", return_value=_completed(23, _rsync_stdout([]))):
            result, reindex, clean = _flush(work, cleanup=True)
        assert reindex.call_count == 0
        assert clean.call_count == 0
        assert result == {"pushed": 0}

    def test_timeout_with_none_stdout_does_not_raise(self, work):
        with patch("subprocess.run", side_effect=_timeout(None)):
            result, reindex, clean = _flush(work, cleanup=True)
        assert result == {"pushed": 0}
        assert reindex.call_count == 0
        assert clean.call_count == 0

    def test_timeout_with_partial_stdout_reindexes_that_subset(self, work):
        with patch("subprocess.run", side_effect=_timeout(_rsync_stdout(REL[:2]))):
            result, reindex, clean = _flush(work, cleanup=True)
        files, kw = _reindexed_files(reindex)
        assert files == [str(work / r) for r in REL[:2]]
        assert kw["root"] == str(work)
        assert clean.call_count == 0
        assert result == {"pushed": 2}

    def test_rsync_missing_reindexes_nothing(self, work):
        with patch("subprocess.run", side_effect=FileNotFoundError("rsync")):
            result, reindex, clean = _flush(work, cleanup=True)
        assert result == {"pushed": 0}
        assert reindex.call_count == 0
        assert clean.call_count == 0


class TestFlushFixedBatchSuccessUnchanged:
    def test_rc0_reindexes_the_full_list(self, work):
        # rsync prints only what it sent; the success path reindexes the WHOLE
        # batch regardless (files already identical still have correct rows).
        with patch(
            "subprocess.run", return_value=_completed(0, _rsync_stdout(REL[:1]))
        ):
            result, reindex, clean = _flush(work)
        files, kw = _reindexed_files(reindex)
        assert files == [str(work / r) for r in REL]
        assert kw == {
            "root": str(work),
            "storage_location": ARCHIVE_NAME,
            "dest_prefix": ARCHIVE_DESTPATH,
        }
        assert clean.call_count == 0
        assert result == {"pushed": len(REL)}

    def test_rc0_honours_cleanup(self, work):
        with patch("subprocess.run", return_value=_completed(0, _rsync_stdout(REL))):
            _, _reindex, clean = _flush(work, cleanup=True)
        assert clean.call_count == 1


# --------------------------------------------------------------------------
# site 2: _push_reconverted


def _reconvert_args(**kw):
    base = dict(
        dry_run=False,
        fix_headers=True,  # regenerability gate does not apply
        from_archive=False,
        source_dir=None,
        backup_old=False,
        reindex=True,
        catalog_prod=False,
        catalog_host=None,
    )
    base.update(kw)
    return Namespace(**base)


def _push(work: Path, tmp_path: Path, run_patch):
    """Drive _push_reconverted with only_rel=REL and rsync stubbed."""
    logger = logging.getLogger("test.push")
    with (
        patch.object(m, "_push_reindex") as reindex,
        patch.object(
            m,
            "_resolve_archive_target",
            return_value=(ARCHIVE_DEST, ARCHIVE_NAME, ARCHIVE_DESTPATH),
        ),
        # an empty archive root: the degradation gate finds nothing to compare
        patch.object(m, "_reconvert_source_root", return_value=str(tmp_path / "arch")),
        patch(
            "receivers.archive.format_guard.split_bad_z", lambda rel, *a, **k: (rel, [])
        ),
        patch("subprocess.run", **run_patch),
    ):
        stats = m._push_reconverted(work, _reconvert_args(), logger, only_rel=REL)
    return stats, reindex


class TestPushReconvertedPartial:
    def test_rc23_reindexes_exactly_the_transferred_subset(self, work, tmp_path):
        landed = REL[:2]
        stats, reindex = _push(
            work, tmp_path, dict(return_value=_completed(23, _rsync_stdout(landed)))
        )
        files, kw = _reindexed_files(reindex)
        assert files == [str(work / r) for r in landed]  # 2 of 5, staged paths
        assert kw == {
            "root": str(work),
            "storage_location": ARCHIVE_NAME,
            "dest_prefix": ARCHIVE_DESTPATH,
        }
        assert stats["pushed"] == 2 and stats["rc"] == 23

    def test_rc23_with_nothing_transferred_does_not_reindex(self, work, tmp_path):
        stats, reindex = _push(
            work, tmp_path, dict(return_value=_completed(23, _rsync_stdout([])))
        )
        assert reindex.call_count == 0
        assert stats["pushed"] == 0

    def test_timeout_with_none_stdout_does_not_raise(self, work, tmp_path):
        stats, reindex = _push(work, tmp_path, dict(side_effect=_timeout(None)))
        assert reindex.call_count == 0
        assert stats["pushed"] == 0 and stats["rc"] is None

    def test_timeout_with_partial_stdout_reindexes_that_subset(self, work, tmp_path):
        stats, reindex = _push(
            work, tmp_path, dict(side_effect=_timeout(_rsync_stdout(REL[4:])))
        )
        files, _kw = _reindexed_files(reindex)
        assert files == [str(work / REL[4])]
        assert stats["pushed"] == 1

    def test_rsync_missing_reindexes_nothing(self, work, tmp_path):
        stats, reindex = _push(
            work, tmp_path, dict(side_effect=FileNotFoundError("rsync"))
        )
        assert reindex.call_count == 0
        assert stats["pushed"] == 0


class TestPushReconvertedSuccessUnchanged:
    def test_rc0_reindexes_the_full_list_and_counts_transfers(self, work, tmp_path):
        stats, reindex = _push(
            work, tmp_path, dict(return_value=_completed(0, _rsync_stdout(REL[:3])))
        )
        files, kw = _reindexed_files(reindex)
        assert files == [str(work / r) for r in REL]  # all 5, as before
        assert kw == {
            "root": str(work),
            "storage_location": ARCHIVE_NAME,
            "dest_prefix": ARCHIVE_DESTPATH,
        }
        assert stats["pushed"] == 3  # transferred-count semantics unchanged
        assert stats["rc"] == 0
