#!/usr/bin/env python3
"""Fix double-compressed .sbf.gz files in the archive.

This script:
1. Scans archive for .sbf.gz files
2. Detects which ones are double-compressed (gzip of gzip)
3. Decompresses them once to get single-compressed files
4. Backs up originals before fixing
5. Provides detailed statistics and dry-run mode
"""

import argparse
import gzip
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from receivers.utils.compression_detector import CompressionDetector


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger(__name__)


def is_double_compressed(file_path: Path, logger: logging.Logger) -> bool:
    """Check if a .gz file is double-compressed.

    Args:
        file_path: Path to .gz file
        logger: Logger instance

    Returns:
        True if file is gzip of gzip, False otherwise
    """
    try:
        # Check outer layer
        detector = CompressionDetector(logger)
        outer = detector.detect_compression(file_path)

        if not outer or outer[0] != 'gzip':
            return False

        # Check inner layer by reading magic bytes
        with gzip.open(file_path, 'rb') as gz:
            magic = gz.read(2)
            # gzip magic bytes: 1f 8b
            return magic == b'\x1f\x8b'

    except Exception as e:
        logger.debug(f"Error checking {file_path.name}: {e}")
        return False


def fix_double_compressed_file(
    file_path: Path,
    backup_dir: Path,
    logger: logging.Logger,
    dry_run: bool = False
) -> Tuple[bool, str]:
    """Fix a single double-compressed file.

    Args:
        file_path: Path to double-compressed file
        backup_dir: Directory for backups
        logger: Logger instance
        dry_run: If True, don't actually modify files

    Returns:
        Tuple of (success, message)
    """
    try:
        # Create backup
        if not dry_run:
            backup_path = backup_dir / file_path.name
            shutil.copy2(file_path, backup_path)
            logger.debug(f"  Backed up to: {backup_path}")

        # Decompress once to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Decompress outer layer
            if not dry_run:
                with gzip.open(file_path, 'rb') as gz_in:
                    with open(tmp_path, 'wb') as f_out:
                        shutil.copyfileobj(gz_in, f_out)

                # Verify inner layer is now correct (still gzip or uncompressed SBF)
                detector = CompressionDetector(logger)
                inner_compression = detector.detect_compression(tmp_path)

                # Get file sizes
                original_size = file_path.stat().st_size
                fixed_size = tmp_path.stat().st_size

                # Replace original with fixed version
                shutil.move(str(tmp_path), str(file_path))

                return (True,
                    f"Fixed: {original_size:,} → {fixed_size:,} bytes "
                    f"(inner: {inner_compression[0] if inner_compression else 'none'})")
            else:
                return (True, "Would fix (dry-run)")

        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    except Exception as e:
        logger.error(f"  Error fixing {file_path.name}: {e}")
        return (False, str(e))


def find_sbf_files(archive_path: Path, logger: logging.Logger) -> List[Path]:
    """Find all .sbf.gz files in archive.

    Args:
        archive_path: Root archive directory
        logger: Logger instance

    Returns:
        List of .sbf.gz file paths
    """
    logger.info(f"Scanning for .sbf.gz files in: {archive_path}")
    files = list(archive_path.rglob("*.sbf.gz"))
    logger.info(f"Found {len(files)} .sbf.gz files")
    return files


def main():
    """Main script entry point."""
    parser = argparse.ArgumentParser(
        description="Fix double-compressed .sbf.gz files in archive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run - see what would be fixed
  %(prog)s /tmp/gpsdata --dry-run

  # Fix double-compressed files with backup
  %(prog)s /tmp/gpsdata --backup-dir /tmp/sbf_backups

  # Fix specific station
  %(prog)s /tmp/gpsdata/2025/nov/ISFS/status_1hr/raw

  # Verbose output
  %(prog)s /tmp/gpsdata -v
        """
    )

    parser.add_argument(
        'archive_path',
        type=Path,
        help='Path to archive directory to scan'
    )

    parser.add_argument(
        '--backup-dir',
        type=Path,
        default=None,
        help='Directory for backups (default: archive_path/backups_YYYYMMDD_HHMMSS)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be fixed without modifying files'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )

    parser.add_argument(
        '--max-files',
        type=int,
        default=None,
        help='Maximum number of files to process (for testing)'
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(args.verbose)

    # Validate archive path
    if not args.archive_path.exists():
        logger.error(f"Archive path does not exist: {args.archive_path}")
        return 1

    # Setup backup directory
    if args.backup_dir is None:
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = args.archive_path / f"backups_{timestamp}"
    else:
        backup_dir = args.backup_dir

    if not args.dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Backups will be saved to: {backup_dir}")

    # Find all .sbf.gz files
    files = find_sbf_files(args.archive_path, logger)

    if not files:
        logger.info("No .sbf.gz files found")
        return 0

    # Apply max-files limit if specified
    if args.max_files:
        files = files[:args.max_files]
        logger.info(f"Limited to {args.max_files} files for testing")

    # Check which files are double-compressed
    logger.info("Checking for double-compressed files...")
    double_compressed = []

    for i, file_path in enumerate(files, 1):
        if i % 50 == 0:
            logger.info(f"  Checked {i}/{len(files)} files...")

        if is_double_compressed(file_path, logger):
            double_compressed.append(file_path)

    logger.info(f"\nFound {len(double_compressed)} double-compressed files")

    if not double_compressed:
        logger.info("✅ No double-compressed files found - archive is clean!")
        return 0

    if args.dry_run:
        logger.info("\n=== DRY RUN - No files will be modified ===")

    # Fix double-compressed files
    logger.info(f"\n{'Would fix' if args.dry_run else 'Fixing'} {len(double_compressed)} files...")

    success_count = 0
    fail_count = 0

    for i, file_path in enumerate(double_compressed, 1):
        logger.info(f"\n[{i}/{len(double_compressed)}] {file_path.name}")

        success, message = fix_double_compressed_file(
            file_path, backup_dir, logger, args.dry_run
        )

        if success:
            success_count += 1
            logger.info(f"  ✅ {message}")
        else:
            fail_count += 1
            logger.error(f"  ❌ Failed: {message}")

    # Summary
    logger.info("\n" + "="*60)
    logger.info("SUMMARY")
    logger.info("="*60)
    logger.info(f"Total files scanned: {len(files)}")
    logger.info(f"Double-compressed found: {len(double_compressed)}")
    logger.info(f"Successfully fixed: {success_count}")
    logger.info(f"Failed: {fail_count}")

    if not args.dry_run and backup_dir.exists():
        logger.info(f"\nBackups saved to: {backup_dir}")
        logger.info(f"Backup size: {sum(f.stat().st_size for f in backup_dir.glob('*')):,} bytes")

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
