"""Unit tests for FileArchiver utility.

These tests validate that the FileArchiver produces identical results
to the existing receiver implementations' archiving logic, supporting
both immediate and bulk archiving modes.
"""

import gzip
import tempfile
from pathlib import Path

import pytest

from receivers.utils.file_archiver import (
    ArchiveMode,
    ArchiveResult,
    FileArchiver,
    GzipCompression,
    NoCompression,
)
from receivers.utils.file_validator import FileValidator
from tests.fixtures.test_data import (
    ARCHIVING_TEST_CASES,
    create_test_file,
    get_test_case_by_name,
)


class TestCompressionStrategies:
    """Test compression strategy implementations."""

    def setup_method(self):
        """Set up test directories."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.source_file = self.test_dir / "source.sbf"
        self.dest_file = self.test_dir / "dest.sbf.gz"

    def test_gzip_compression(self):
        """Test gzip compression strategy."""
        # Create source file
        create_test_file(self.source_file, size=2048, is_compressed=False)

        # Compress
        strategy = GzipCompression()
        success = strategy.compress(self.source_file, self.dest_file)

        assert success is True
        assert self.dest_file.exists()

        # Verify it's actually gzipped
        with open(self.dest_file, "rb") as f:
            magic = f.read(2)
            assert magic == b"\x1f\x8b"

    def test_gzip_compression_ratio(self):
        """Test compression ratio calculation."""
        strategy = GzipCompression()
        ratio = strategy.get_compression_ratio(2048, 1024)
        assert ratio == 50.0  # 50% reduction

    def test_no_compression_strategy(self):
        """Test no compression (direct copy) strategy."""
        create_test_file(self.source_file, size=2048, is_compressed=False)

        strategy = NoCompression()
        success = strategy.compress(self.source_file, self.dest_file)

        assert success is True
        assert self.dest_file.exists()
        assert self.dest_file.stat().st_size == self.source_file.stat().st_size


class TestArchiveResult:
    """Test ArchiveResult data class."""

    def test_successful_result(self):
        """Test successful archive result."""
        result = ArchiveResult(
            success=True,
            tmp_file=Path("/tmp/file.sbf"),
            archive_file=Path("/archive/file.sbf.gz"),
            source_size=2048,
            archived_size=1024,
            compression_ratio=50.0,
        )

        assert result.success is True
        assert result.compression_ratio == 50.0
        assert "success=True" in str(result)

    def test_failed_result(self):
        """Test failed archive result."""
        result = ArchiveResult(
            success=False, tmp_file=Path("/tmp/file.sbf"), error="File not found"
        )

        assert result.success is False
        assert result.error == "File not found"
        assert "success=False" in str(result)


class TestImmediateModeArchiving:
    """Test immediate archiving mode (PolaRX5 style)."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_immediate_single_file(self):
        """Test immediate archiving of single file."""
        # Create tmp file
        tmp_file = self.tmp_dir / "ELDC202509240000a.sbf"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "ELDC202509240000a.sbf.gz"

        # Archive immediately
        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            success = archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

            assert success is True

        # Verify archive exists
        assert archive_path.exists()
        # Verify tmp removed
        assert not tmp_file.exists()

    def test_immediate_multiple_files(self):
        """Test immediate archiving of multiple files."""
        files = []
        for i in range(3):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            files.append(tmp_file)

        archived_count = 0
        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            for tmp_file in files:
                archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
                if archiver.archive_file(
                    tmp_file, archive_path, compress=True, remove_tmp=True
                ):
                    archived_count += 1

        assert archived_count == 3

        # Verify all archives exist
        for i in range(3):
            assert (self.archive_dir / f"file{i}.sbf.gz").exists()
            assert not (self.tmp_dir / f"file{i}.sbf").exists()

    def test_immediate_without_compression(self):
        """Test immediate archiving without compression."""
        tmp_file = self.tmp_dir / "ELDC202509240000a.T02"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "ELDC202509240000a.T02"

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            success = archiver.archive_file(
                tmp_file, archive_path, compress=False, remove_tmp=True
            )

            assert success is True

        assert archive_path.exists()
        assert not tmp_file.exists()
        # Should be same size (no compression)
        assert archive_path.stat().st_size == 2048


class TestBulkModeArchiving:
    """Test bulk archiving mode (NetR9/NetRS/Leica style)."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_bulk_queues_files(self):
        """Test that bulk mode queues files without immediate archiving."""
        tmp_file = self.tmp_dir / "file.sbf"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "file.sbf.gz"

        archiver = FileArchiver(mode=ArchiveMode.BULK)

        # Queue file
        success = archiver.archive_file(
            tmp_file, archive_path, compress=True, remove_tmp=True
        )

        assert success is True  # Returns true even though not yet archived
        assert archiver.get_pending_count() == 1
        assert tmp_file.exists()  # Still exists
        assert not archive_path.exists()  # Not yet archived

    def test_bulk_flush_archives_all(self):
        """Test that flush archives all pending files."""
        files = []
        for i in range(3):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            files.append(tmp_file)

        archiver = FileArchiver(mode=ArchiveMode.BULK)

        # Queue all files
        for tmp_file in files:
            archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
            archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

        assert archiver.get_pending_count() == 3

        # Flush
        archived_count = archiver.flush_pending_archives()

        assert archived_count == 3
        assert archiver.get_pending_count() == 0

        # Verify all archived
        for i in range(3):
            assert (self.archive_dir / f"file{i}.sbf.gz").exists()
            assert not (self.tmp_dir / f"file{i}.sbf").exists()

    def test_bulk_context_manager_auto_flush(self):
        """Test that context manager auto-flushes on exit."""
        files = []
        for i in range(2):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            files.append(tmp_file)

        with FileArchiver(mode=ArchiveMode.BULK) as archiver:
            for tmp_file in files:
                archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
                archiver.archive_file(
                    tmp_file, archive_path, compress=True, remove_tmp=True
                )

            # Still pending inside context
            assert archiver.get_pending_count() == 2

        # Auto-flushed on exit
        for i in range(2):
            assert (self.archive_dir / f"file{i}.sbf.gz").exists()


class TestBatchArchiving:
    """Test batch archiving method (receiver compatibility)."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_batch_archive_files_immediate_mode(self):
        """Test batch_archive_files with immediate mode."""
        # Create downloaded files
        downloaded_files = []
        archive_dict = {}

        for i in range(3):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            downloaded_files.append(str(tmp_file))

            filename = tmp_file.name
            archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
            archive_dict[filename] = str(archive_path)

        # Archive with immediate mode
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
        archived_count = archiver.batch_archive_files(
            downloaded_files, archive_dict, compress=True, remove_tmp=True
        )

        assert archived_count == 3

        # Verify all archived
        for i in range(3):
            assert (self.archive_dir / f"file{i}.sbf.gz").exists()

    def test_batch_archive_files_bulk_mode(self):
        """Test batch_archive_files with bulk mode."""
        downloaded_files = []
        archive_dict = {}

        for i in range(3):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            downloaded_files.append(str(tmp_file))

            filename = tmp_file.name
            archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
            archive_dict[filename] = str(archive_path)

        # Archive with bulk mode
        archiver = FileArchiver(mode=ArchiveMode.BULK)
        archived_count = archiver.batch_archive_files(
            downloaded_files, archive_dict, compress=True, remove_tmp=True
        )

        assert archived_count == 3

        # Verify all archived
        for i in range(3):
            assert (self.archive_dir / f"file{i}.sbf.gz").exists()


class TestErrorHandling:
    """Test error handling and edge cases."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_archive_nonexistent_file(self):
        """Test archiving file that doesn't exist."""
        tmp_file = self.tmp_dir / "nonexistent.sbf"
        archive_path = self.archive_dir / "nonexistent.sbf.gz"

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            success = archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

            assert success is False

        # Check result
        results = archiver.get_results()
        assert len(results) == 1
        assert results[0].success is False
        assert "not found" in results[0].error.lower()

    def test_archive_existing_file_same_size(self):
        """Test archiving when archive already exists with same size."""
        tmp_file = self.tmp_dir / "file.sbf"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "file.sbf.gz"
        # Pre-create archive with same size
        create_test_file(archive_path, size=2048, is_compressed=True)

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            success = archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

            # Should succeed (skip archiving, remove tmp)
            assert success is True

        # Tmp should be removed
        assert not tmp_file.exists()


class TestStatisticsAndReporting:
    """Test statistics and reporting features."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_get_results(self):
        """Test getting detailed results."""
        tmp_file = self.tmp_dir / "file.sbf"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "file.sbf.gz"

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

        results = archiver.get_results()
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].source_size == 2048
        assert results[0].compression_ratio > 0

    def test_get_statistics(self):
        """Test getting archiving statistics."""
        # Archive multiple files
        files = []
        for i in range(3):
            tmp_file = self.tmp_dir / f"file{i}.sbf"
            create_test_file(tmp_file, size=2048, is_compressed=False)
            files.append(tmp_file)

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            for tmp_file in files:
                archive_path = self.archive_dir / f"{tmp_file.stem}.sbf.gz"
                archiver.archive_file(
                    tmp_file, archive_path, compress=True, remove_tmp=True
                )

            stats = archiver.get_statistics()

            assert stats["total_files"] == 3
            assert stats["successful"] == 3
            assert stats["failed"] == 0
            assert stats["total_source_size"] == 2048 * 3
            assert stats["average_compression_ratio"] > 0

    def test_clear_results(self):
        """Test clearing results history."""
        tmp_file = self.tmp_dir / "file.sbf"
        create_test_file(tmp_file, size=2048, is_compressed=False)

        archive_path = self.archive_dir / "file.sbf.gz"

        with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
            archiver.archive_file(
                tmp_file, archive_path, compress=True, remove_tmp=True
            )

            assert len(archiver.get_results()) == 1

            archiver.clear_results()

            assert len(archiver.get_results()) == 0


class TestModeSwitch:
    """Test switching between modes."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.tmp_dir = self.test_dir / "tmp"
        self.archive_dir = self.test_dir / "archive"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def test_set_mode(self):
        """Test changing mode at runtime."""
        archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
        assert archiver.mode == ArchiveMode.IMMEDIATE

        archiver.set_mode(ArchiveMode.BULK)
        assert archiver.mode == ArchiveMode.BULK


class TestCustomCompressionStrategy:
    """Test custom compression strategy registration."""

    def test_register_custom_strategy(self):
        """Test registering custom compression strategy."""

        class DummyCompression:
            def compress(self, source, destination):
                # Just copy with custom extension
                import shutil

                shutil.copy2(source, destination)
                return True

            def get_extension(self):
                return ".dummy"

            def get_compression_ratio(self, source_size, compressed_size):
                return 0.0

        archiver = FileArchiver()
        archiver.register_compression_strategy(".dummy", DummyCompression())

        # Verify registered
        assert ".dummy" in archiver._compression_strategies
