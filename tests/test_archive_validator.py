"""Unit tests for ArchiveValidator utility.

These tests validate that the ArchiveValidator produces identical results
to the existing receiver implementations' validation logic.
"""

import gzip
import pytest
import tempfile
from pathlib import Path

from receivers.utils.archive_validator import (
    ArchiveValidator,
    ArchiveLocation,
    GzipValidator
)
from receivers.utils.file_validator import FileValidator

from tests.fixtures.test_data import (
    ARCHIVE_VALIDATION_CASES,
    ARCHIVE_DISCOVERY_CASES,
    create_test_file,
    get_test_case_by_name
)


class TestGzipValidator:
    """Test GzipValidator compression validator."""

    def test_valid_gzip_magic_bytes(self, tmp_path):
        """Test validation of file with correct gzip magic bytes."""
        test_file = tmp_path / "test.sbf.gz"
        create_test_file(test_file, size=2048, is_compressed=True)

        validator = GzipValidator()
        assert validator.validate_magic_bytes(test_file) is True

    def test_invalid_gzip_magic_bytes(self, tmp_path):
        """Test validation of file with incorrect gzip magic bytes."""
        test_file = tmp_path / "test.sbf.gz"
        create_test_file(test_file, size=2048, is_compressed=True, gzip_magic=b'\x00\x00')

        validator = GzipValidator()
        assert validator.validate_magic_bytes(test_file) is False

    def test_get_extension(self):
        """Test extension getter."""
        validator = GzipValidator()
        assert validator.get_extension() == '.gz'


class TestArchiveValidatorBasics:
    """Test basic ArchiveValidator functionality."""

    def test_validate_valid_uncompressed_file(self, tmp_path):
        """Test validation of valid uncompressed file."""
        case = get_test_case_by_name(ARCHIVE_VALIDATION_CASES, 'valid_uncompressed_file')
        test_file = tmp_path / case['file_path']
        create_test_file(test_file, size=case['file_size'], is_compressed=False)

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result == case['expected_valid']

    def test_validate_valid_compressed_file(self, tmp_path):
        """Test validation of valid gzip compressed file."""
        case = get_test_case_by_name(ARCHIVE_VALIDATION_CASES, 'valid_compressed_file')
        test_file = tmp_path / case['file_path']
        create_test_file(
            test_file,
            size=case['file_size'],
            is_compressed=True,
            gzip_magic=case['gzip_magic_bytes']
        )

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result == case['expected_valid']

    def test_validate_zero_size_file(self, tmp_path):
        """Test validation of zero-size file (should be invalid)."""
        case = get_test_case_by_name(ARCHIVE_VALIDATION_CASES, 'zero_size_file')
        test_file = tmp_path / case['file_path']
        create_test_file(test_file, size=0, is_compressed=False)

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result == case['expected_valid']

    def test_validate_too_small_file(self, tmp_path):
        """Test validation of file smaller than minimum threshold."""
        case = get_test_case_by_name(ARCHIVE_VALIDATION_CASES, 'too_small_file')
        test_file = tmp_path / case['file_path']
        create_test_file(test_file, size=case['file_size'], is_compressed=False)

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result == case['expected_valid']

    def test_validate_corrupted_gzip_header(self, tmp_path):
        """Test validation of gzip file with invalid magic bytes."""
        case = get_test_case_by_name(ARCHIVE_VALIDATION_CASES, 'corrupted_gzip_header')
        test_file = tmp_path / case['file_path']
        create_test_file(
            test_file,
            size=case['file_size'],
            is_compressed=True,
            gzip_magic=case['gzip_magic_bytes']  # Wrong magic bytes
        )

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result == case['expected_valid']

    def test_validate_nonexistent_file(self, tmp_path):
        """Test validation of file that doesn't exist."""
        test_file = tmp_path / "nonexistent.sbf.gz"

        validator = ArchiveValidator()
        result = validator.validate_archived_file(test_file)

        assert result is False


class TestArchiveValidatorConfiguration:
    """Test configurable aspects of ArchiveValidator."""

    def test_custom_min_file_size(self, tmp_path):
        """Test setting custom minimum file size threshold."""
        test_file = tmp_path / "test.sbf"
        create_test_file(test_file, size=512, is_compressed=False)

        # Default threshold (1024) should reject 512-byte file
        validator_default = ArchiveValidator()
        assert validator_default.validate_archived_file(test_file) is False

        # Custom threshold (256) should accept 512-byte file
        validator_custom = ArchiveValidator(min_file_size=256)
        assert validator_custom.validate_archived_file(test_file) is True

    def test_runtime_min_file_size_change(self, tmp_path):
        """Test changing minimum file size at runtime."""
        test_file = tmp_path / "test.sbf"
        create_test_file(test_file, size=512, is_compressed=False)

        validator = ArchiveValidator()
        # Default: should reject
        assert validator.validate_archived_file(test_file) is False

        # Change threshold
        validator.set_min_file_size(256)
        # Now should accept
        assert validator.validate_archived_file(test_file) is True


class TestArchiveDiscovery:
    """Test archive file discovery across multiple locations."""

    def setup_method(self):
        """Set up test directories."""
        self.test_dir = tempfile.mkdtemp()
        self.test_path = Path(self.test_dir)
        self.archive_dir = self.test_path / "archive"
        self.tmp_dir = self.test_path / "tmp"

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def test_find_uncompressed_in_archive(self):
        """Test finding uncompressed file in archive directory."""
        filename = "ELDC202509240000a.sbf"
        archive_path = self.archive_dir / filename
        create_test_file(archive_path, size=2048, is_compressed=False)

        validator = ArchiveValidator()
        found, path, location = validator.find_existing_archive(
            filename,
            str(archive_path),
            self.tmp_dir
        )

        assert found is True
        assert path == archive_path
        assert location == ArchiveLocation.ARCHIVE

    def test_find_compressed_in_archive(self):
        """Test finding compressed file in archive directory."""
        filename = "ELDC202509240000a.sbf"
        archive_path_base = self.archive_dir / filename
        archive_path_gz = Path(str(archive_path_base) + ".gz")
        create_test_file(archive_path_gz, size=1536, is_compressed=True)

        validator = ArchiveValidator()
        found, path, location = validator.find_existing_archive(
            filename,
            str(archive_path_base),
            self.tmp_dir
        )

        assert found is True
        assert path == archive_path_gz
        assert location == ArchiveLocation.ARCHIVE_COMPRESSED

    def test_find_in_tmp_directory(self):
        """Test finding file in temporary directory."""
        filename = "ELDC202509240000a.sbf"
        tmp_file = self.tmp_dir / filename
        create_test_file(tmp_file, size=2048, is_compressed=False)

        # Archive path doesn't exist
        archive_path = self.archive_dir / filename

        validator = ArchiveValidator()
        found, path, location = validator.find_existing_archive(
            filename,
            str(archive_path),
            self.tmp_dir
        )

        assert found is True
        assert path == tmp_file
        assert location == ArchiveLocation.TMP

    def test_file_not_found_anywhere(self):
        """Test when file doesn't exist in any location."""
        filename = "ELDC202509240000a.sbf"
        archive_path = self.archive_dir / filename

        validator = ArchiveValidator()
        found, path, location = validator.find_existing_archive(
            filename,
            str(archive_path),
            self.tmp_dir
        )

        assert found is False
        assert path is None
        assert location == ArchiveLocation.NOT_FOUND

    def test_priority_order(self):
        """Test that uncompressed archive has priority over tmp."""
        filename = "ELDC202509240000a.sbf"

        # Create file in both archive and tmp
        archive_path = self.archive_dir / filename
        create_test_file(archive_path, size=2048, is_compressed=False)

        tmp_file = self.tmp_dir / filename
        create_test_file(tmp_file, size=2048, is_compressed=False)

        validator = ArchiveValidator()
        found, path, location = validator.find_existing_archive(
            filename,
            str(archive_path),
            self.tmp_dir
        )

        # Should find in archive, not tmp
        assert found is True
        assert path == archive_path
        assert location == ArchiveLocation.ARCHIVE


class TestBatchValidation:
    """Test batch validation operations."""

    def setup_method(self):
        """Set up test environment."""
        self.test_dir = tempfile.mkdtemp()
        self.test_path = Path(self.test_dir)
        self.archive_dir = self.test_path / "archive"
        self.tmp_dir = self.test_path / "tmp"

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def test_batch_with_all_files_existing(self):
        """Test batch validation when all files exist in archive."""
        files_dict = {
            'ELDC202509240000a.sbf': '/remote/path/a',
            'ELDC202509250000a.sbf': '/remote/path/b',
            'ELDC202509260000a.sbf': '/remote/path/c',
        }

        archive_files_dict = {}
        # Create all files in archive
        for filename in files_dict.keys():
            archive_path = self.archive_dir / filename
            create_test_file(archive_path, size=2048, is_compressed=False)
            archive_files_dict[filename] = str(archive_path)

        validator = ArchiveValidator()
        missing, found, validated = validator.batch_validate_archives(
            files_dict,
            archive_files_dict,
            self.tmp_dir
        )

        assert len(missing) == 0  # No files missing
        assert found == 3  # All 3 files found
        assert validated == 3  # All 3 validated

    def test_batch_with_some_files_missing(self):
        """Test batch validation with some files missing."""
        files_dict = {
            'ELDC202509240000a.sbf': '/remote/path/a',
            'ELDC202509250000a.sbf': '/remote/path/b',
            'ELDC202509260000a.sbf': '/remote/path/c',
        }

        archive_files_dict = {}
        # Create only first file
        first_filename = 'ELDC202509240000a.sbf'
        archive_path = self.archive_dir / first_filename
        create_test_file(archive_path, size=2048, is_compressed=False)
        archive_files_dict[first_filename] = str(archive_path)

        # Add archive paths for missing files
        for filename in files_dict.keys():
            if filename not in archive_files_dict:
                archive_files_dict[filename] = str(self.archive_dir / filename)

        validator = ArchiveValidator()
        missing, found, validated = validator.batch_validate_archives(
            files_dict,
            archive_files_dict,
            self.tmp_dir
        )

        assert len(missing) == 2  # 2 files missing
        assert found == 1  # 1 file found
        assert validated == 3  # All 3 validated
        assert 'ELDC202509250000a.sbf' in missing
        assert 'ELDC202509260000a.sbf' in missing


class TestDetailedReport:
    """Test detailed validation reporting."""

    def test_detailed_report_valid_file(self, tmp_path):
        """Test detailed report for valid file."""
        test_file = tmp_path / "test.sbf.gz"
        create_test_file(test_file, size=2048, is_compressed=True)

        validator = ArchiveValidator()
        report = validator.validate_with_detailed_report(test_file)

        assert report['valid'] is True
        assert report['file_exists'] is True
        assert report['file_size'] == 2048
        assert report['meets_min_size'] is True
        assert report['compression_format'] == '.gz'
        assert report['compression_valid'] is True
        assert len(report['errors']) == 0

    def test_detailed_report_missing_file(self, tmp_path):
        """Test detailed report for missing file."""
        test_file = tmp_path / "nonexistent.sbf"

        validator = ArchiveValidator()
        report = validator.validate_with_detailed_report(test_file)

        assert report['valid'] is False
        assert report['file_exists'] is False
        assert len(report['errors']) > 0

    def test_detailed_report_corrupted_compression(self, tmp_path):
        """Test detailed report for file with invalid compression."""
        test_file = tmp_path / "test.sbf.gz"
        create_test_file(test_file, size=2048, is_compressed=True, gzip_magic=b'\x00\x00')

        validator = ArchiveValidator()
        report = validator.validate_with_detailed_report(test_file)

        assert report['valid'] is False
        assert report['file_exists'] is True
        assert report['compression_format'] == '.gz'
        assert report['compression_valid'] is False
        assert len(report['errors']) > 0