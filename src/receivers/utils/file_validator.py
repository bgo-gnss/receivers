"""File validation utilities for receivers package.

This module provides utilities for validating downloaded files,
checking compression integrity, and determining file completeness.
"""

import gzip
import logging
import os
import zipfile
from pathlib import Path
from typing import Dict, Optional, Tuple


class FileValidator:
    """Validates downloaded receiver files."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize file validator.

        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)

    def validate_file(
        self, file_path: str, expected_size: Optional[int] = None
    ) -> Dict[str, any]:
        """Comprehensive file validation.

        Args:
            file_path: Path to file to validate
            expected_size: Expected file size (if known)


        Returns:
            Dictionary with validation results:
            {
                'valid': bool,
                'status': str,  # 'valid', 'missing', 'zero_size', 'corrupted', 'incomplete'
                'size': int,
                'compression': str,  # 'gzip', 'zip', 'none', 'unknown'
                'integrity_ok': bool,
                'error': str  # if any
            }
        """
        result = {
            "valid": False,
            "status": "unknown",
            "size": 0,
            "compression": "unknown",
            "integrity_ok": False,
            "error": None,
        }

        try:
            # Check if file exists
            if not os.path.isfile(file_path):
                result.update({"status": "missing", "error": "File does not exist"})
                return result

            # Get file size
            file_size = os.path.getsize(file_path)
            result["size"] = file_size

            # Check for zero-size files
            if file_size == 0:
                result.update(
                    {"status": "zero_size", "error": "File is empty (0 bytes)"}
                )
                return result

            # Detect compression type
            compression_info = self._detect_compression(file_path)
            result["compression"] = compression_info["type"]

            # Validate compression integrity
            integrity_result = self._validate_compression_integrity(
                file_path, compression_info
            )
            result["integrity_ok"] = integrity_result["valid"]

            if not integrity_result["valid"]:
                result.update(
                    {
                        "status": "corrupted",
                        "error": integrity_result.get(
                            "error", "Compression integrity check failed"
                        ),
                    }
                )
                return result

            # Check size expectations
            if expected_size is not None:
                if file_size < expected_size:
                    result.update(
                        {
                            "status": "incomplete",
                            "error": f"File smaller than expected ({file_size} < {expected_size} bytes)",
                        }
                    )
                    return result
                elif file_size > expected_size:
                    result.update(
                        {
                            "status": "corrupted",
                            "error": f"File larger than expected ({file_size} > {expected_size} bytes)",
                        }
                    )
                    return result

            # All checks passed
            result.update({"valid": True, "status": "valid"})

        except Exception as e:
            result.update({"status": "error", "error": f"Validation error: {str(e)}"})

        return result

    def _detect_compression(self, file_path: str) -> Dict[str, any]:
        """Detect compression type from file.

        Args:
            file_path: Path to file

        Returns:
            Dictionary with compression info
        """
        try:
            with open(file_path, "rb") as f:
                # Read first few bytes to detect format
                header = f.read(10)

            # Check for gzip magic bytes
            if len(header) >= 2 and header[:2] == b"\x1f\x8b":
                return {"type": "gzip", "detected_from": "magic_bytes"}

            # Check for zip magic bytes
            if len(header) >= 4 and header[:4] == b"PK\x03\x04":
                return {"type": "zip", "detected_from": "magic_bytes"}

            # Check file extension as fallback
            file_ext = Path(file_path).suffix.lower()
            if file_ext == ".gz":
                return {"type": "gzip", "detected_from": "extension"}
            elif file_ext == ".zip":
                return {"type": "zip", "detected_from": "extension"}

            # No compression detected
            return {"type": "none", "detected_from": "analysis"}

        except Exception as e:
            self.logger.debug(f"Compression detection error for {file_path}: {e}")
            return {"type": "unknown", "detected_from": "error", "error": str(e)}

    def _validate_compression_integrity(
        self, file_path: str, compression_info: Dict
    ) -> Dict[str, any]:
        """Validate compression file integrity.

        Args:
            file_path: Path to file
            compression_info: Compression information from _detect_compression

        Returns:
            Dictionary with integrity check results
        """
        comp_type = compression_info.get("type", "unknown")

        try:
            if comp_type == "gzip":
                return self._validate_gzip_integrity(file_path)
            elif comp_type == "zip":
                return self._validate_zip_integrity(file_path)
            elif comp_type == "none":
                # No compression - assume valid if file exists and has size
                return {"valid": True, "method": "no_compression"}
            else:
                # Unknown compression type - skip validation
                return {"valid": True, "method": "skipped_unknown_compression"}

        except Exception as e:
            return {"valid": False, "error": f"Integrity check failed: {str(e)}"}

    def _validate_gzip_integrity(self, file_path: str) -> Dict[str, any]:
        """Validate gzip file integrity.

        Args:
            file_path: Path to gzip file

        Returns:
            Dictionary with validation results
        """
        try:
            # Try to read the entire gzip file
            with gzip.open(file_path, "rb") as gz_file:
                # Read in chunks to avoid memory issues with large files
                chunk_size = 1024 * 1024  # 1MB chunks
                total_uncompressed = 0

                while True:
                    chunk = gz_file.read(chunk_size)
                    if not chunk:
                        break
                    total_uncompressed += len(chunk)

            return {
                "valid": True,
                "method": "full_read",
                "uncompressed_size": total_uncompressed,
            }

        except (OSError, gzip.BadGzipFile) as e:
            return {
                "valid": False,
                "method": "gzip_read",
                "error": f"Gzip corruption detected: {str(e)}",
            }

    def _validate_zip_integrity(self, file_path: str) -> Dict[str, any]:
        """Validate zip file integrity.

        Args:
            file_path: Path to zip file

        Returns:
            Dictionary with validation results
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zip_file:
                # Test the zip file integrity
                bad_files = zip_file.testzip()
                if bad_files:
                    return {
                        "valid": False,
                        "method": "zip_test",
                        "error": f"Zip corruption in files: {bad_files}",
                    }

                # Get file count and total size
                file_count = len(zip_file.filelist)
                total_size = sum(info.file_size for info in zip_file.filelist)

                return {
                    "valid": True,
                    "method": "zip_test",
                    "file_count": file_count,
                    "total_uncompressed_size": total_size,
                }

        except (zipfile.BadZipFile, OSError) as e:
            return {
                "valid": False,
                "method": "zip_test",
                "error": f"Zip corruption detected: {str(e)}",
            }

    def should_resume_download(
        self, file_path: str, remote_size: Optional[int] = None
    ) -> Tuple[bool, int]:
        """Determine if download should be resumed and from what offset.

        For partial downloads, we use lightweight validation that doesn't
        attempt to validate compression integrity (which would fail for partial files).

        Args:
            file_path: Path to local file
            remote_size: Size of remote file (if known)

        Returns:
            Tuple of (should_resume, resume_offset)
        """
        if not os.path.isfile(file_path):
            return False, 0

        try:
            file_size = os.path.getsize(file_path)

            # Zero-size files should be removed
            if file_size == 0:
                self.logger.info(f"Removing zero-size file: {file_path}")
                try:
                    os.unlink(file_path)
                except OSError as e:
                    self.logger.warning(
                        f"Could not remove zero-size file {file_path}: {e}"
                    )
                return False, 0

            # If we have remote size, compare for sanity check
            if remote_size is not None:
                if file_size > remote_size:
                    # Local file larger than remote - definitely corrupted
                    self.logger.info(
                        f"Removing oversized file: {file_path} ({file_size} > {remote_size} bytes)"
                    )
                    try:
                        os.unlink(file_path)
                    except OSError as e:
                        self.logger.warning(
                            f"Could not remove oversized file {file_path}: {e}"
                        )
                    return False, 0
                elif file_size == remote_size:
                    # File is complete - no need to resume
                    self.logger.debug(
                        f"File already complete: {file_path} ({file_size} bytes)"
                    )
                    return False, 0
                else:
                    # Local file smaller than remote - can resume
                    self.logger.info(
                        f"Resuming download from byte {file_size} (local: {file_size}, remote: {remote_size})"
                    )
                    return True, file_size
            else:
                # No remote size info - assume we can resume any non-zero file
                self.logger.info(
                    f"Resuming download from byte {file_size} (no remote size check)"
                )
                return True, file_size

        except Exception as e:
            self.logger.warning(f"Error checking file {file_path}: {e}")
            return False, 0

    def clean_directory(self, directory: str, pattern: str = "*") -> int:
        """Clean files from directory.

        Args:
            directory: Directory to clean
            pattern: File pattern to match (default: all files)

        Returns:
            Number of files removed
        """
        if not os.path.isdir(directory):
            return 0

        removed_count = 0
        try:
            dir_path = Path(directory)
            for file_path in dir_path.glob(pattern):
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        removed_count += 1
                        self.logger.debug(f"Removed: {file_path}")
                    except OSError as e:
                        self.logger.warning(f"Could not remove {file_path}: {e}")

            self.logger.info(f"Cleaned {removed_count} files from {directory}")

        except Exception as e:
            self.logger.error(f"Error cleaning directory {directory}: {e}")

        return removed_count
