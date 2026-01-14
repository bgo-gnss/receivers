"""Compression format detection and conversion utility.

This module provides utilities to:
1. Detect compression formats by reading magic bytes from file headers
2. Convert between compression formats (decompress + recompress)
3. Ensure files are in the desired compression format
"""

import bz2
import gzip
import logging
import shutil
from pathlib import Path
from typing import BinaryIO, Optional, Tuple


class CompressionFormat:
    """Compression format definitions with magic bytes."""

    GZIP = ('gzip', b'\x1f\x8b', '.gz')
    BZIP2 = ('bzip2', b'\x42\x5a\x68', '.bz2')
    XZ = ('xz', b'\xfd\x37\x7a\x58\x5a\x00', '.xz')
    ZIP = ('zip', b'\x50\x4b\x03\x04', '.zip')
    COMPRESS = ('compress', b'\x1f\x9d', '.Z')
    ZSTD = ('zstd', b'\x28\xb5\x2f\xfd', '.zst')

    # All formats for detection
    ALL_FORMATS = [GZIP, BZIP2, XZ, ZIP, COMPRESS, ZSTD]


class CompressionDetector:
    """Detect compression format by reading file magic bytes."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize compression detector.

        Args:
            logger: Optional logger for diagnostics
        """
        self.logger = logger or logging.getLogger(__name__)

    def detect_compression(self, file_path: Path) -> Optional[Tuple[str, str]]:
        """Detect compression format by reading magic bytes.

        Args:
            file_path: Path to file to check

        Returns:
            Tuple of (format_name, extension) if compressed, None if uncompressed
            Examples: ('gzip', '.gz'), ('bzip2', '.bz2'), None
        """
        if not file_path.exists():
            self.logger.warning(f"File does not exist: {file_path}")
            return None

        try:
            with open(file_path, 'rb') as f:
                # Read enough bytes to check longest magic signature (6 bytes for xz)
                magic_bytes = f.read(6)

                if not magic_bytes:
                    self.logger.debug(f"Empty file: {file_path}")
                    return None

                # Check against known formats
                for format_name, magic, extension in CompressionFormat.ALL_FORMATS:
                    magic_len = len(magic)
                    if len(magic_bytes) >= magic_len:
                        if magic_bytes[:magic_len] == magic:
                            self.logger.debug(
                                f"Detected {format_name} compression: {file_path.name}"
                            )
                            return (format_name, extension)

                # No compression detected
                self.logger.debug(f"No compression detected: {file_path.name}")
                return None

        except Exception as e:
            self.logger.error(f"Error reading file magic bytes: {e}")
            return None

    def is_compressed(self, file_path: Path) -> bool:
        """Check if file is compressed.

        Args:
            file_path: Path to file to check

        Returns:
            True if file is compressed, False otherwise
        """
        return self.detect_compression(file_path) is not None

    def is_gzip_compressed(self, file_path: Path) -> bool:
        """Check if file is gzip compressed.

        Args:
            file_path: Path to file to check

        Returns:
            True if file is gzip compressed, False otherwise
        """
        result = self.detect_compression(file_path)
        return result is not None and result[0] == 'gzip'

    def needs_compression(self, file_path: Path) -> bool:
        """Determine if file needs compression before archiving.

        This is the recommended method to use before archiving operations.

        Args:
            file_path: Path to file to check

        Returns:
            True if file should be compressed, False if already compressed
        """
        return not self.is_compressed(file_path)


def detect_compression(file_path: Path) -> Optional[Tuple[str, str]]:
    """Convenience function to detect compression format.

    Args:
        file_path: Path to file to check

    Returns:
        Tuple of (format_name, extension) if compressed, None if uncompressed
    """
    detector = CompressionDetector()
    return detector.detect_compression(file_path)


def is_compressed(file_path: Path) -> bool:
    """Convenience function to check if file is compressed.

    Args:
        file_path: Path to file to check

    Returns:
        True if file is compressed, False otherwise
    """
    detector = CompressionDetector()
    return detector.is_compressed(file_path)


def needs_compression(file_path: Path) -> bool:
    """Convenience function to check if file needs compression.

    Args:
        file_path: Path to file to check

    Returns:
        True if file should be compressed, False if already compressed
    """
    detector = CompressionDetector()
    return detector.needs_compression(file_path)


class CompressionConverter:
    """Convert files between compression formats.

    This class provides the flexibility to:
    - Decompress files
    - Compress files in a specific format
    - Convert from one format to another (decompress + recompress)
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize compression converter.

        Args:
            logger: Optional logger for diagnostics
        """
        self.logger = logger or logging.getLogger(__name__)
        self.detector = CompressionDetector(logger)

        # Compression handlers
        self._compressors = {
            'gzip': self._compress_gzip,
            'bzip2': self._compress_bz2,
            'none': self._compress_none,  # No compression (copy)
        }

        self._decompressors = {
            'gzip': self._decompress_gzip,
            'bzip2': self._decompress_bz2,
        }

    def decompress_file(self, source: Path, destination: Path) -> bool:
        """Decompress a file to destination.

        Args:
            source: Compressed source file
            destination: Destination for decompressed file

        Returns:
            True if successful, False otherwise
        """
        # Detect current compression
        compression_info = self.detector.detect_compression(source)

        if compression_info is None:
            self.logger.warning(f"File is not compressed: {source}")
            # Copy uncompressed file
            try:
                shutil.copy2(source, destination)
                return True
            except Exception as e:
                self.logger.error(f"Error copying file: {e}")
                return False

        format_name, _ = compression_info
        self.logger.debug(f"Decompressing {format_name} file: {source.name}")

        try:
            decompressor = self._decompressors.get(format_name)
            if not decompressor:
                self.logger.error(f"No decompressor available for {format_name}")
                return False

            decompressor(source, destination)
            self.logger.debug(f"✅ Decompressed to: {destination}")

            # Check if result is still compressed (double-compression case)
            inner_compression = self.detector.detect_compression(destination)
            if inner_compression:
                inner_format, _ = inner_compression
                self.logger.debug(f"Detected double-compression, inner format: {inner_format}")
                # Decompress again to a temp file then replace
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.sbf') as tmp:
                    tmp_path = Path(tmp.name)
                inner_decompressor = self._decompressors.get(inner_format)
                if inner_decompressor:
                    inner_decompressor(destination, tmp_path)
                    # Replace destination with fully decompressed file
                    import shutil
                    shutil.move(str(tmp_path), str(destination))
                    self.logger.debug(f"✅ Double-decompressed to: {destination}")

            return True

        except Exception as e:
            self.logger.error(f"Error decompressing file: {e}")
            return False

    def compress_file(
        self,
        source: Path,
        destination: Path,
        format: str = 'gzip'
    ) -> bool:
        """Compress a file in specified format.

        Args:
            source: Source file to compress
            destination: Destination for compressed file
            format: Compression format ('gzip', 'bzip2', or 'none')

        Returns:
            True if successful, False otherwise
        """
        self.logger.info(f"Compressing with {format}: {source.name}")

        try:
            compressor = self._compressors.get(format)
            if not compressor:
                self.logger.error(f"No compressor available for {format}")
                return False

            compressor(source, destination)
            self.logger.info(f"✅ Compressed to: {destination}")
            return True

        except Exception as e:
            self.logger.error(f"Error compressing file: {e}")
            return False

    def convert_compression(
        self,
        source: Path,
        destination: Path,
        target_format: str = 'gzip'
    ) -> bool:
        """Convert file to specified compression format.

        If file is already in target format, just copy it.
        Otherwise, decompress and recompress in target format.

        Args:
            source: Source file (any compression or uncompressed)
            destination: Destination file
            target_format: Target compression format ('gzip', 'bzip2', 'none')

        Returns:
            True if successful, False otherwise
        """
        # Detect current compression
        current_compression = self.detector.detect_compression(source)

        # Check if already in target format
        if target_format == 'none' and current_compression is None:
            # Already uncompressed, just copy
            self.logger.debug(f"File already uncompressed, copying: {source.name}")
            try:
                shutil.copy2(source, destination)
                return True
            except Exception as e:
                self.logger.error(f"Error copying file: {e}")
                return False

        if current_compression and current_compression[0] == target_format:
            # Already in target format, just copy
            self.logger.debug(
                f"File already in {target_format} format, copying: {source.name}"
            )
            try:
                shutil.copy2(source, destination)
                return True
            except Exception as e:
                self.logger.error(f"Error copying file: {e}")
                return False

        # Need conversion: decompress + recompress
        self.logger.info(
            f"Converting {source.name} from "
            f"{current_compression[0] if current_compression else 'none'} "
            f"to {target_format}"
        )

        try:
            # Use temporary file for intermediate step
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
                tmp_path = Path(tmp.name)

            try:
                # Step 1: Decompress to temp (if compressed)
                if current_compression:
                    if not self.decompress_file(source, tmp_path):
                        return False
                else:
                    # Already uncompressed, copy to temp
                    shutil.copy2(source, tmp_path)

                # Step 2: Compress from temp to destination
                if target_format == 'none':
                    # Just move/copy the decompressed file
                    shutil.move(str(tmp_path), str(destination))
                else:
                    if not self.compress_file(tmp_path, destination, target_format):
                        return False

                self.logger.info(
                    f"✅ Converted to {target_format}: {destination}"
                )
                return True

            finally:
                # Clean up temp file if it still exists
                if tmp_path.exists():
                    tmp_path.unlink()

        except Exception as e:
            self.logger.error(f"Error converting compression: {e}")
            return False

    def _decompress_gzip(self, source: Path, destination: Path):
        """Decompress gzip file."""
        with gzip.open(source, 'rb') as f_in:
            with open(destination, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def _decompress_bz2(self, source: Path, destination: Path):
        """Decompress bzip2 file."""
        with bz2.open(source, 'rb') as f_in:
            with open(destination, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def _compress_gzip(self, source: Path, destination: Path):
        """Compress with gzip."""
        with open(source, 'rb') as f_in:
            with gzip.open(destination, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def _compress_bz2(self, source: Path, destination: Path):
        """Compress with bzip2."""
        with open(source, 'rb') as f_in:
            with bz2.open(destination, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def _compress_none(self, source: Path, destination: Path):
        """No compression - just copy."""
        shutil.copy2(source, destination)
