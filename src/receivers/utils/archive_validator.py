"""Archive validation utilities for receivers package.

This module provides unified archive validation logic extracted from all receiver
implementations. It validates file integrity, checks compression format, and
discovers existing archive files across multiple locations.

Design for extensibility:
- Plugin architecture for compression format validators
- Configurable validation rules
- Support for custom validation strategies
"""

import gzip
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple, Any

from .file_validator import FileValidator


class ArchiveLocation(Enum):
    """Possible locations where an archive file might exist."""
    ARCHIVE = "archive"  # Uncompressed in archive directory
    ARCHIVE_COMPRESSED = "archive_compressed"  # Compressed in archive directory
    TMP = "tmp"  # In temporary download directory
    NOT_FOUND = "not_found"  # File doesn't exist


class CompressionValidator(Protocol):
    """Protocol for compression format validators.

    Allows future extension for different compression formats (bz2, xz, zst, etc.)
    without modifying the core ArchiveValidator logic.
    """

    def validate_magic_bytes(self, file_path: Path) -> bool:
        """Validate compression format magic bytes.

        Args:
            file_path: Path to file to validate

        Returns:
            True if magic bytes are valid for this compression format
        """
        ...

    def get_extension(self) -> str:
        """Get file extension for this compression format.

        Returns:
            File extension including dot (e.g., '.gz', '.bz2')
        """
        ...


class GzipValidator:
    """Gzip compression validator."""

    MAGIC_BYTES = b'\x1f\x8b'

    def validate_magic_bytes(self, file_path: Path) -> bool:
        """Validate gzip magic bytes."""
        try:
            with open(file_path, 'rb') as f:
                magic = f.read(2)
                return magic == self.MAGIC_BYTES
        except (OSError, IOError):
            return False

    def get_extension(self) -> str:
        """Get gzip extension."""
        return '.gz'


class ArchiveValidator:
    """Unified archive file validation and discovery.

    This class consolidates validation logic that was duplicated across
    all receiver implementations. It provides:
    - File size validation
    - Compression format validation
    - Multi-location archive discovery
    - Batch validation operations

    Design considerations:
    - Uses composition with FileValidator for file-level checks
    - Supports plugin-based compression validators
    - Configurable minimum file size threshold
    - Extensible for future validation rules
    """

    def __init__(
        self,
        file_validator: Optional[FileValidator] = None,
        logger: Optional[logging.Logger] = None,
        min_file_size: int = 1024  # 1KB minimum (configurable)
    ):
        """Initialize archive validator.

        Args:
            file_validator: Optional FileValidator instance for comprehensive checks
            logger: Optional logger instance
            min_file_size: Minimum file size in bytes (default 1KB)
        """
        self.file_validator = file_validator
        self.logger = logger or logging.getLogger(__name__)
        self.min_file_size = min_file_size

        # Compression validators registry (extensible for future formats)
        self._compression_validators: Dict[str, CompressionValidator] = {
            '.gz': GzipValidator(),
            # Future: '.bz2': Bzip2Validator(), '.xz': XzValidator(), etc.
        }

    def register_compression_validator(
        self,
        extension: str,
        validator: CompressionValidator
    ) -> None:
        """Register a new compression format validator.

        Allows extending support for new compression formats without
        modifying the core ArchiveValidator code.

        Args:
            extension: File extension (e.g., '.zst', '.br')
            validator: Compression validator instance
        """
        self._compression_validators[extension] = validator
        self.logger.debug(f"Registered compression validator for {extension}")

    def validate_archived_file(self, file_path: Path) -> bool:
        """Validate archived file integrity.

        This is the core validation logic extracted from all receiver implementations.
        Performs basic sanity checks that were identical across receivers:
        1. File size >= minimum threshold
        2. For compressed files: validate compression format

        Args:
            file_path: Path to archived file

        Returns:
            True if file passes validation, False otherwise
        """
        try:
            # Check 1: File must exist
            if not file_path.exists():
                self.logger.debug(f"File does not exist: {file_path}")
                return False

            # Check 2: File size must meet minimum threshold
            file_size = file_path.stat().st_size
            if file_size < self.min_file_size:
                self.logger.debug(
                    f"File too small ({file_size} bytes, minimum {self.min_file_size}): {file_path}"
                )
                return False

            # Check 3: If file is compressed, validate compression format
            file_extension = ''.join(file_path.suffixes[-1:])  # Get last extension
            if file_extension in self._compression_validators:
                validator = self._compression_validators[file_extension]
                if not validator.validate_magic_bytes(file_path):
                    self.logger.debug(
                        f"File doesn't have valid {file_extension} magic header: {file_path}"
                    )
                    return False

            # All checks passed
            return True

        except (OSError, IOError) as e:
            self.logger.debug(f"Error validating archived file {file_path}: {e}")
            return False

    def find_existing_archive(
        self,
        filename: str,
        archive_path: str,
        tmp_dir: Optional[Path] = None
    ) -> Tuple[bool, Optional[Path], ArchiveLocation]:
        """Find existing archive file across multiple locations.

        Search priority:
        1. Uncompressed in archive directory
        2. Compressed versions in archive directory
        3. Temporary download directory (if provided)

        Args:
            filename: Base filename to search for
            archive_path: Expected archive path (without compression extension)
            tmp_dir: Optional temporary directory to check

        Returns:
            Tuple of (found, path, location):
            - found: True if file exists and is valid
            - path: Path to found file (None if not found)
            - location: ArchiveLocation enum indicating where file was found
        """
        archive_path_obj = Path(archive_path)

        # Check 1: Uncompressed in archive
        if archive_path_obj.exists():
            if self.validate_archived_file(archive_path_obj):
                self.logger.debug(
                    f"Archive file exists: {archive_path_obj.name} "
                    f"({archive_path_obj.stat().st_size} bytes)"
                )
                return True, archive_path_obj, ArchiveLocation.ARCHIVE
            else:
                self.logger.warning(
                    f"Archived file failed validation: {archive_path_obj}"
                )

        # Check 2: Compressed versions in archive
        for ext, validator in self._compression_validators.items():
            compressed_path = Path(str(archive_path) + ext)
            if compressed_path.exists():
                if self.validate_archived_file(compressed_path):
                    self.logger.debug(
                        f"Archive file exists with compression: {compressed_path.name} "
                        f"({compressed_path.stat().st_size} bytes)"
                    )
                    return True, compressed_path, ArchiveLocation.ARCHIVE_COMPRESSED
                else:
                    self.logger.warning(
                        f"Compressed archived file failed validation: {compressed_path}"
                    )

        # Check 3: Temporary directory
        if tmp_dir:
            tmp_file_path = tmp_dir / filename
            if tmp_file_path.exists():
                if self.validate_archived_file(tmp_file_path):
                    self.logger.debug(
                        f"File exists in tmp: {tmp_file_path.name} "
                        f"({tmp_file_path.stat().st_size} bytes)"
                    )
                    return True, tmp_file_path, ArchiveLocation.TMP
                else:
                    self.logger.warning(
                        f"Tmp file failed validation: {tmp_file_path}"
                    )

        # Not found in any location
        return False, None, ArchiveLocation.NOT_FOUND

    def batch_validate_archives(
        self,
        files_dict: Dict[str, str],
        archive_files_dict: Dict[str, str],
        tmp_dir: Optional[Path] = None
    ) -> Tuple[Dict[str, str], int, int, Dict[str, Path]]:
        """Batch validation of archive files.

        This method consolidates the file filtering logic that was duplicated
        in all receiver download_data() methods. It identifies which files
        need to be downloaded by checking if they already exist in the archive.

        Args:
            files_dict: Dict mapping filename -> remote_directory
            archive_files_dict: Dict mapping filename -> archive_path
            tmp_dir: Optional temporary directory to check

        Returns:
            Tuple of (missing_files_dict, files_found_count, files_validated_count, files_in_tmp_dict):
            - missing_files_dict: Dict of files that need to be downloaded
            - files_found_count: Number of files found in archive (not tmp)
            - files_validated_count: Total number of files validated
            - files_in_tmp_dict: Dict mapping filename -> tmp_path for files in tmp that need archiving
        """
        missing_files_dict = {}
        files_in_tmp_dict = {}
        files_found_in_archive = 0
        validated_files = 0

        for filename, remote_dir in files_dict.items():
            validated_files += 1
            archive_path = archive_files_dict.get(filename)

            if archive_path:
                # Check all possible locations
                found, path, location = self.find_existing_archive(
                    filename, archive_path, tmp_dir
                )

                if found:
                    if location == ArchiveLocation.TMP:
                        # File exists in tmp but needs archiving
                        files_in_tmp_dict[filename] = path
                        self.logger.debug(
                            f"File found in tmp (needs archiving): {filename}"
                        )
                    else:
                        # File is properly archived
                        files_found_in_archive += 1
                        self.logger.debug(
                            f"File found in {location.value}: {filename}"
                        )
                    continue  # Skip download in both cases

            # File is missing - add to download list
            missing_files_dict[filename] = remote_dir

        # Log summary
        if files_found_in_archive > 0:
            self.logger.info(
                f"Found {files_found_in_archive} files already archived, "
                f"skipping re-download"
            )

        if files_in_tmp_dict:
            self.logger.info(
                f"Found {len(files_in_tmp_dict)} files in tmp directory that need archiving"
            )

        return missing_files_dict, files_found_in_archive, validated_files, files_in_tmp_dict

    def get_compression_extensions(self) -> List[str]:
        """Get list of supported compression extensions.

        Useful for checking which compression formats are currently supported.

        Returns:
            List of compression extensions (e.g., ['.gz', '.bz2'])
        """
        return list(self._compression_validators.keys())

    def set_min_file_size(self, min_size: int) -> None:
        """Set minimum file size threshold.

        Allows runtime configuration of validation rules.

        Args:
            min_size: Minimum file size in bytes
        """
        self.min_file_size = min_size
        self.logger.debug(f"Updated minimum file size to {min_size} bytes")

    def validate_with_detailed_report(
        self,
        file_path: Path
    ) -> Dict[str, Any]:
        """Validate file with detailed report.

        Provides comprehensive validation results for debugging and logging.

        Args:
            file_path: Path to file to validate

        Returns:
            Dictionary with validation details:
            {
                'valid': bool,
                'file_exists': bool,
                'file_size': int,
                'meets_min_size': bool,
                'compression_format': Optional[str],
                'compression_valid': Optional[bool],
                'errors': List[str]
            }
        """
        report = {
            'valid': False,
            'file_exists': False,
            'file_size': 0,
            'meets_min_size': False,
            'compression_format': None,
            'compression_valid': None,
            'errors': []
        }

        # Check existence
        if not file_path.exists():
            report['errors'].append(f"File does not exist: {file_path}")
            return report

        report['file_exists'] = True

        # Check size
        try:
            file_size = file_path.stat().st_size
            report['file_size'] = file_size
            report['meets_min_size'] = file_size >= self.min_file_size

            if not report['meets_min_size']:
                report['errors'].append(
                    f"File size {file_size} bytes < minimum {self.min_file_size} bytes"
                )
        except (OSError, IOError) as e:
            report['errors'].append(f"Error reading file size: {e}")
            return report

        # Check compression
        file_extension = ''.join(file_path.suffixes[-1:])
        if file_extension in self._compression_validators:
            report['compression_format'] = file_extension
            validator = self._compression_validators[file_extension]
            report['compression_valid'] = validator.validate_magic_bytes(file_path)

            if not report['compression_valid']:
                report['errors'].append(
                    f"Invalid {file_extension} compression format"
                )

        # Overall validity
        report['valid'] = len(report['errors']) == 0

        return report