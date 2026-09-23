"""The absolute zero-byte refusal in FileArchiver.

Distinct from the statistical data-yield guard (tests/test_yield_guard.py): that
one asks "is this file too small for THIS station?", a judgement that must fail
OPEN when it has no DB, no baseline or too few samples. This one asks "is there
any data here at all?", which needs no baseline — so it must hold in exactly the
states where the statistical guard abstains.

Regression origin: on 2026-07-05 two stations (ROTH, NVEL) archived a 0-byte SBF
that gzip turned into a valid 42-byte empty archive; it pushed to the long-term
archive and was catalogued as the real file. The good local copies survived only
by luck. The yield guard did not exist yet, and its fail-open paths would have
passed the file anyway.
"""

import gzip
from unittest.mock import MagicMock

from receivers.utils.file_archiver import ArchiveMode, FileArchiver
from receivers.utils.yield_guard import YieldGuardConfig


def _paths(tmp_path, station="ROTH", name=None):
    name = name or f"{station}202607040000a.sbf"
    src = tmp_path / "tmp" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    dest = tmp_path / "arch" / "2026" / "jul" / station / "15s_24hr" / "raw" / name
    return src, dest


class TestZeroByteIsAlwaysRefused:
    """The three states in which the statistical guard abstains."""

    def test_refused_with_no_yield_guard_at_all(self, tmp_path):
        src, dest = _paths(tmp_path)
        src.write_bytes(b"")
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
        assert archiver.yield_guard is None
        assert archiver.archive_file(src, dest, compress=False) is False
        assert not dest.exists()
        assert not dest.parent.exists(), "must not even create the archive dir"

    def test_refused_when_the_guard_has_no_db(self, tmp_path):
        """`conn=None` makes check_yield return 'no db connection — fail open'."""
        src, dest = _paths(tmp_path)
        src.write_bytes(b"")
        archiver = FileArchiver(
            mode=ArchiveMode.IMMEDIATE,
            yield_guard=YieldGuardConfig(connection=None),
        )
        assert archiver.archive_file(src, dest, compress=False) is False
        assert not dest.exists()

    def test_refused_when_the_guard_is_disabled(self, tmp_path):
        src, dest = _paths(tmp_path)
        src.write_bytes(b"")
        cfg = MagicMock()
        cfg.enabled = False
        cfg.quarantine_root = None
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE, yield_guard=cfg)
        assert archiver.archive_file(src, dest, compress=False) is False
        assert not dest.exists()


class TestTheMeasuredFailure:
    def test_zero_byte_source_never_becomes_an_empty_gz_on_the_archive(self, tmp_path):
        """The ROTH/NVEL shape: gzip of nothing is a VALID 42-byte archive.

        Without the guard the compression step happily produces a well-formed
        empty .gz, which is why nothing downstream noticed — it is not corrupt,
        it simply holds no data.
        """
        src, dest = _paths(tmp_path, station="NVEL")
        src.write_bytes(b"")
        gz = dest.with_suffix(dest.suffix + ".gz")
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)

        assert archiver.archive_file(src, dest, compress=True) is False
        assert not gz.exists(), "an empty .gz must never reach the archive tree"
        assert not dest.exists()

    def test_an_empty_gz_would_otherwise_be_valid(self, tmp_path):
        """Pins WHY this needed a guard rather than a corruption check."""
        p = tmp_path / "empty.sbf.gz"
        with gzip.GzipFile(p, "wb"):
            pass
        assert p.stat().st_size > 0
        with gzip.open(p, "rb") as fh:
            assert fh.read() == b""


class TestItStaysOutOfTheStatisticalGuardsJob:
    def test_one_byte_is_not_this_guards_business(self, tmp_path):
        """Absolute-zero only. Anything above 0 is a judgement — and with no
        baseline available the statistical guard correctly lets it through."""
        src, dest = _paths(tmp_path)
        src.write_bytes(b"\x00")
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
        assert archiver.archive_file(src, dest, compress=False) is True
        assert dest.exists()

    def test_a_normal_file_still_archives(self, tmp_path):
        src, dest = _paths(tmp_path)
        src.write_bytes(b"x" * 50_000)
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
        assert archiver.archive_file(src, dest, compress=False) is True
        assert dest.exists()


class TestQuarantine:
    def test_quarantined_when_a_root_is_configured(self, tmp_path):
        src, dest = _paths(tmp_path)
        src.write_bytes(b"")
        qroot = tmp_path / "quarantine"
        archiver = FileArchiver(
            mode=ArchiveMode.IMMEDIATE,
            yield_guard=YieldGuardConfig(connection=None, quarantine_root=qroot),
        )
        assert archiver.archive_file(src, dest, compress=False) is False
        assert (qroot / "ROTH" / src.name).exists(), "kept for inspection"
        assert not dest.exists()

    def test_refusal_holds_even_if_quarantine_explodes(self, tmp_path):
        """Quarantine is best-effort; the refusal is not."""
        src, dest = _paths(tmp_path)
        src.write_bytes(b"")
        cfg = MagicMock()
        cfg.enabled = True
        type(cfg).quarantine_root = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE, yield_guard=cfg)
        assert archiver.archive_file(src, dest, compress=False) is False
        assert not dest.exists()
