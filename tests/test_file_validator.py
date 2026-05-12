"""Tests for receivers.utils.file_validator.

Focused on the partial-gzip classification (truncated vs corrupt) added in
response to the HVEH 2026-05-10 incident, where a partial accumulated under
inconsistent writes ended up the right size but failing integrity check.
"""

import gzip
import io

from receivers.utils.file_validator import FileValidator, classify_partial_gzip


def _make_valid_gzip(payload: bytes) -> bytes:
    """Return a complete valid gzip-compressed bytestring."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(payload)
    return buf.getvalue()


class TestClassifyPartialGzip:
    """Distinguish complete / truncated / corrupt / missing for partials."""

    def test_missing_file(self, tmp_path):
        assert classify_partial_gzip(str(tmp_path / "no_such_file.gz")) == "missing"

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.gz"
        path.write_bytes(b"")
        assert classify_partial_gzip(str(path)) == "missing"

    def test_complete_valid_gzip(self, tmp_path):
        path = tmp_path / "good.gz"
        path.write_bytes(_make_valid_gzip(b"hello world" * 100))
        assert classify_partial_gzip(str(path)) == "complete"

    def test_truncated_mid_stream(self, tmp_path):
        """A real partial download — bytes cut mid-stream cleanly."""
        full = _make_valid_gzip(b"x" * 100_000)
        path = tmp_path / "partial.gz"
        # Keep only the first 60% of the bytes
        path.write_bytes(full[: int(len(full) * 0.6)])
        assert classify_partial_gzip(str(path)) == "truncated"

    def test_corrupt_invalid_block(self, tmp_path):
        """Valid gzip header + invalid DEFLATE body → 'corrupt'.

        Triggers Z_DATA_ERROR ("invalid block type"), the same family of
        zlib error as the HVEH 2026-05-10 incident's "invalid stored block
        lengths". Using deterministic bytes (header + 0xFF garbage) so the
        test never flakes on whatever the gzip decoder makes of random
        XOR'd bytes.
        """
        # Minimal valid gzip header (1f 8b 08 = magic + DEFLATE method)
        gzip_header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
        body = b"\xff" * 1000  # 0xFF bytes contain reserved DEFLATE block type
        path = tmp_path / "corrupt.gz"
        path.write_bytes(gzip_header + body)
        assert classify_partial_gzip(str(path)) == "corrupt"

    def test_invalid_gzip_header(self, tmp_path):
        """Garbage bytes that aren't even a valid gzip header → corrupt."""
        path = tmp_path / "garbage.gz"
        path.write_bytes(b"this is not a gzip file at all" * 10)
        assert classify_partial_gzip(str(path)) == "corrupt"


class TestShouldResumeWithGzipCheck:
    """should_resume_download must use the gzip classifier for .gz files."""

    def test_corrupt_partial_is_deleted(self, tmp_path):
        """The HVEH-shaped scenario: file_size < remote_size but bytes corrupt."""
        path = tmp_path / "HVEH1290.26_.gz"
        # Deterministic corrupt body (Z_DATA_ERROR territory)
        gzip_header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
        path.write_bytes(gzip_header + b"\xff" * 5000)
        local_size = path.stat().st_size
        validator = FileValidator()
        # Pretend remote is bigger so default would say "resume"
        should_resume, offset = validator.should_resume_download(
            str(path), remote_size=local_size + 100_000
        )
        assert should_resume is False
        assert offset == 0
        assert not path.exists(), "corrupt partial should have been deleted"

    def test_truncated_partial_resumes(self, tmp_path):
        """Truncated partial — the normal in-progress download case."""
        path = tmp_path / "ENTC1290.26_.gz"
        full = _make_valid_gzip(b"a" * 100_000)
        path.write_bytes(full[: int(len(full) * 0.5)])
        local_size = path.stat().st_size
        validator = FileValidator()
        should_resume, offset = validator.should_resume_download(
            str(path), remote_size=local_size + 200
        )
        assert should_resume is True
        assert offset == local_size

    def test_complete_partial_does_not_resume(self, tmp_path):
        """Partial that is actually complete — already fully downloaded."""
        path = tmp_path / "THOB1290.26_.gz"
        path.write_bytes(_make_valid_gzip(b"b" * 50_000))
        local_size = path.stat().st_size
        validator = FileValidator()
        # Tell it remote is bigger so default would say "resume" — but the
        # gzip is internally complete, so we should NOT resume
        should_resume, offset = validator.should_resume_download(
            str(path), remote_size=local_size + 999
        )
        assert should_resume is False
        assert offset == 0
        assert path.exists(), "complete partial must not be deleted"

    def test_oversized_partial_still_deleted(self, tmp_path):
        """Pre-existing behaviour preserved: file > remote → delete."""
        path = tmp_path / "FAGC1290.26_.gz"
        path.write_bytes(_make_valid_gzip(b"c" * 100_000))
        local_size = path.stat().st_size
        validator = FileValidator()
        should_resume, offset = validator.should_resume_download(
            str(path), remote_size=local_size - 100
        )
        assert should_resume is False
        assert offset == 0
        assert not path.exists()
