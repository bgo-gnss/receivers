"""Tests for compression detection utility."""

import bz2
import gzip
import tempfile
from pathlib import Path

import pytest

from receivers.utils.compression_detector import (
    CompressionDetector,
    detect_compression,
    is_compressed,
    needs_compression,
)


@pytest.fixture
def temp_dir():
    """Create temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def uncompressed_file(temp_dir):
    """Create uncompressed test file."""
    file_path = temp_dir / "test.sbf"
    file_path.write_bytes(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data" * 100)
    return file_path


@pytest.fixture
def gzip_file(temp_dir):
    """Create gzip compressed test file."""
    file_path = temp_dir / "test.sbf.gz"
    with gzip.open(file_path, "wb") as f:
        f.write(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data" * 100)
    return file_path


@pytest.fixture
def bzip2_file(temp_dir):
    """Create bzip2 compressed test file."""
    file_path = temp_dir / "test.sbf.bz2"
    with bz2.open(file_path, "wb") as f:
        f.write(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data" * 100)
    return file_path


@pytest.fixture
def double_compressed_file(temp_dir):
    """Create double-compressed (gzip of gzip) test file."""
    file_path = temp_dir / "test_double.sbf.gz"

    # Create inner gzip
    inner_compressed = gzip.compress(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data" * 100)

    # Compress again
    with gzip.open(file_path, "wb") as f:
        f.write(inner_compressed)

    return file_path


class TestCompressionDetector:
    """Tests for CompressionDetector class."""

    def test_detect_uncompressed(self, uncompressed_file):
        """Test detection of uncompressed file."""
        detector = CompressionDetector()
        result = detector.detect_compression(uncompressed_file)
        assert result is None
        assert not detector.is_compressed(uncompressed_file)
        assert detector.needs_compression(uncompressed_file)

    def test_detect_gzip(self, gzip_file):
        """Test detection of gzip compressed file."""
        detector = CompressionDetector()
        result = detector.detect_compression(gzip_file)
        assert result is not None
        assert result[0] == "gzip"
        assert result[1] == ".gz"
        assert detector.is_compressed(gzip_file)
        assert detector.is_gzip_compressed(gzip_file)
        assert not detector.needs_compression(gzip_file)

    def test_detect_bzip2(self, bzip2_file):
        """Test detection of bzip2 compressed file."""
        detector = CompressionDetector()
        result = detector.detect_compression(bzip2_file)
        assert result is not None
        assert result[0] == "bzip2"
        assert result[1] == ".bz2"
        assert detector.is_compressed(bzip2_file)
        assert not detector.is_gzip_compressed(bzip2_file)
        assert not detector.needs_compression(bzip2_file)

    def test_detect_double_compressed(self, double_compressed_file):
        """Test detection of double-compressed file (detects outer layer)."""
        detector = CompressionDetector()
        result = detector.detect_compression(double_compressed_file)
        # Should detect outer gzip layer
        assert result is not None
        assert result[0] == "gzip"
        assert detector.is_compressed(double_compressed_file)
        assert not detector.needs_compression(double_compressed_file)

    def test_nonexistent_file(self, temp_dir):
        """Test handling of non-existent file."""
        detector = CompressionDetector()
        nonexistent = temp_dir / "does_not_exist.sbf"
        result = detector.detect_compression(nonexistent)
        assert result is None

    def test_empty_file(self, temp_dir):
        """Test handling of empty file."""
        detector = CompressionDetector()
        empty_file = temp_dir / "empty.sbf"
        empty_file.write_bytes(b"")
        result = detector.detect_compression(empty_file)
        assert result is None


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_detect_compression_function(self, gzip_file):
        """Test detect_compression convenience function."""
        result = detect_compression(gzip_file)
        assert result is not None
        assert result[0] == "gzip"

    def test_is_compressed_function(self, gzip_file, uncompressed_file):
        """Test is_compressed convenience function."""
        assert is_compressed(gzip_file)
        assert not is_compressed(uncompressed_file)

    def test_needs_compression_function(self, gzip_file, uncompressed_file):
        """Test needs_compression convenience function."""
        assert not needs_compression(gzip_file)
        assert needs_compression(uncompressed_file)


class TestRealWorldScenarios:
    """Tests for real-world usage scenarios."""

    def test_sbf_filename_with_gz_extension_but_uncompressed(self, temp_dir):
        """Test file with .gz extension but actually uncompressed (corrupted)."""
        # This shouldn't happen in practice, but test robustness
        file_path = temp_dir / "test.sbf.gz"
        file_path.write_bytes(
            b"$@\x34\xf1\x1a\x17\x18\x00" + b"not actually compressed"
        )

        detector = CompressionDetector()
        # Should detect it's NOT compressed based on magic bytes
        assert not detector.is_compressed(file_path)
        assert detector.needs_compression(file_path)

    def test_uncompressed_sbf_no_extension(self, temp_dir):
        """Test uncompressed SBF without .gz extension."""
        file_path = temp_dir / "test.sbf"
        file_path.write_bytes(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data")

        detector = CompressionDetector()
        assert not detector.is_compressed(file_path)
        assert detector.needs_compression(file_path)

    def test_compressed_sbf_correct_extension(self, temp_dir):
        """Test compressed SBF with correct .sbf.gz extension."""
        file_path = temp_dir / "test.sbf.gz"
        with gzip.open(file_path, "wb") as f:
            f.write(b"$@\x34\xf1\x1a\x17\x18\x00" + b"test data")

        detector = CompressionDetector()
        assert detector.is_compressed(file_path)
        assert not detector.needs_compression(file_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
