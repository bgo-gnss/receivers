"""Test data fixtures capturing current receiver behavior.

These fixtures document the expected behavior of the receivers package
for regression testing during refactoring.
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# Base test configuration
TEST_STATION_ID = "ELDC"
TEST_DATA_DIR = Path("/tmp/test_receivers_data")
TEST_ARCHIVE_DIR = TEST_DATA_DIR / "archive"
TEST_TMP_DIR = TEST_DATA_DIR / "tmp"


# Archive Validation Test Cases
# ============================

ARCHIVE_VALIDATION_CASES = [
    {
        'name': 'valid_uncompressed_file',
        'file_path': 'ELDC202509240000a.sbf',
        'file_size': 2048,  # > 1KB minimum
        'is_compressed': False,
        'expected_valid': True,
        'description': 'Valid uncompressed file with sufficient size'
    },
    {
        'name': 'valid_compressed_file',
        'file_path': 'ELDC202509240000a.sbf.gz',
        'file_size': 1536,
        'is_compressed': True,
        'gzip_magic_bytes': b'\x1f\x8b',
        'expected_valid': True,
        'description': 'Valid gzip compressed file with magic bytes'
    },
    {
        'name': 'zero_size_file',
        'file_path': 'ELDC202509240000a.sbf.gz',
        'file_size': 0,
        'is_compressed': True,
        'expected_valid': False,
        'description': 'Zero-size file should be invalid'
    },
    {
        'name': 'too_small_file',
        'file_path': 'ELDC202509240000a.sbf',
        'file_size': 512,  # < 1KB minimum
        'is_compressed': False,
        'expected_valid': False,
        'description': 'File smaller than 1KB minimum should be invalid'
    },
    {
        'name': 'corrupted_gzip_header',
        'file_path': 'ELDC202509240000a.sbf.gz',
        'file_size': 2048,
        'is_compressed': True,
        'gzip_magic_bytes': b'\x00\x00',  # Wrong magic bytes
        'expected_valid': False,
        'description': 'Compressed file with invalid gzip header'
    },
]


# Timestamp Normalization Test Cases
# ==================================

TIMESTAMP_NORMALIZATION_CASES = [
    {
        'name': 'daily_file_midnight_normalization',
        'session': '15s_24hr',
        'ffrequency': '24hr',
        'input_datetime': datetime(2025, 9, 24, 15, 30, 45),  # Downloaded at 3:30 PM
        'expected_normalized': datetime(2025, 9, 24, 0, 0, 0),  # Normalized to midnight
        'description': 'Daily files should normalize to midnight regardless of download time'
    },
    {
        'name': 'hourly_file_hour_boundary',
        'session': '1Hz_1hr',
        'ffrequency': '1hr',
        'input_datetime': datetime(2025, 9, 24, 15, 30, 45),  # Downloaded at 3:30 PM
        'expected_normalized': datetime(2025, 9, 24, 15, 0, 0),  # Normalized to hour boundary
        'description': 'Hourly files should normalize to hour boundary'
    },
    {
        'name': 'status_hourly_normalization',
        'session': 'status_1hr',
        'ffrequency': '1hr',
        'input_datetime': datetime(2025, 9, 24, 15, 45, 30),
        'expected_normalized': datetime(2025, 9, 24, 15, 0, 0),
        'description': 'Status hourly files should normalize to hour boundary'
    },
]


# File List Generation Test Cases
# ===============================

FILE_LIST_GENERATION_CASES = [
    {
        'name': 'polarx5_daily_session',
        'receiver_type': 'polarx5',
        'session': '15s_24hr',
        'start': datetime(2025, 9, 24, 0, 0, 0),
        'end': datetime(2025, 9, 26, 0, 0, 0),
        'expected_file_count': 3,
        'expected_filenames': [
            'ELDC202509240000a.sbf.gz',
            'ELDC202509250000a.sbf.gz',
            'ELDC202509260000a.sbf.gz',
        ],
        'expected_archive_pattern': '{prepath}/2025/#b/ELDC/15s_24hr/raw/ELDC{timestamp}a.sbf.gz',
        'description': 'PolaRX5 daily session should generate 3 daily files'
    },
    {
        'name': 'polarx5_hourly_session',
        'receiver_type': 'polarx5',
        'session': '1Hz_1hr',
        'start': datetime(2025, 9, 24, 10, 0, 0),
        'end': datetime(2025, 9, 24, 12, 0, 0),
        'expected_file_count': 3,
        'expected_filenames': [
            'ELDC202509241000b.sbf.gz',
            'ELDC202509241100b.sbf.gz',
            'ELDC202509241200b.sbf.gz',
        ],
        'expected_archive_pattern': '{prepath}/2025/#b/ELDC/1Hz_1hr/raw/ELDC{timestamp}b.sbf.gz',
        'description': 'PolaRX5 hourly session should generate hourly files'
    },
    {
        'name': 'leica_daily_session',
        'receiver_type': 'leica',
        'session': '15s_24hr',
        'start': datetime(2025, 9, 24, 0, 0, 0),
        'end': datetime(2025, 9, 24, 0, 0, 0),
        'expected_file_count': 1,
        'expected_remote_filename': 'ELDC267a.m00.zip',  # DOY 267
        'expected_archive_filename': 'ELDC202509240000a.m00.gz',
        'description': 'Leica daily session uses DOY format for remote, standard format for archive'
    },
    {
        'name': 'netr9_hourly_session',
        'receiver_type': 'netr9',
        'session': '1Hz_1hr',
        'start': datetime(2025, 9, 24, 14, 0, 0),
        'end': datetime(2025, 9, 24, 16, 0, 0),
        'expected_file_count': 3,
        'expected_filenames': [
            'ELDC202509241400b.T02',
            'ELDC202509241500b.T02',
            'ELDC202509241600b.T02',
        ],
        'expected_remote_dir': '/Internal/202509/1Hz_1hr/',
        'description': 'NetR9 hourly session with T02 extension'
    },
]


# Archive Discovery Test Cases
# ============================

ARCHIVE_DISCOVERY_CASES = [
    {
        'name': 'file_exists_uncompressed',
        'filename': 'ELDC202509240000a.sbf',
        'archive_locations': {
            'uncompressed': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf',
            'compressed': None,
            'tmp': None,
        },
        'expected_found': True,
        'expected_location': 'archive',
        'expected_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf',
        'description': 'File found in archive (uncompressed)'
    },
    {
        'name': 'file_exists_compressed',
        'filename': 'ELDC202509240000a.sbf',
        'archive_locations': {
            'uncompressed': None,
            'compressed': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf.gz',
            'tmp': None,
        },
        'expected_found': True,
        'expected_location': 'archive_compressed',
        'expected_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf.gz',
        'description': 'File found in archive (compressed)'
    },
    {
        'name': 'file_exists_in_tmp',
        'filename': 'ELDC202509240000a.sbf',
        'archive_locations': {
            'uncompressed': None,
            'compressed': None,
            'tmp': TEST_TMP_DIR / 'ELDC' / 'ELDC202509240000a.sbf',
        },
        'expected_found': True,
        'expected_location': 'tmp',
        'expected_path': TEST_TMP_DIR / 'ELDC' / 'ELDC202509240000a.sbf',
        'description': 'File found in temporary directory'
    },
    {
        'name': 'file_not_found',
        'filename': 'ELDC202509240000a.sbf',
        'archive_locations': {
            'uncompressed': None,
            'compressed': None,
            'tmp': None,
        },
        'expected_found': False,
        'expected_location': 'not_found',
        'expected_path': None,
        'description': 'File not found in any location'
    },
]


# Time Parameter Processing Test Cases
# ====================================

TIME_PARAMETER_CASES = [
    {
        'name': 'datetime_objects_passthrough',
        'start': datetime(2025, 9, 24, 0, 0, 0),
        'end': datetime(2025, 9, 25, 0, 0, 0),
        'session': '15s_24hr',
        'expected_start': datetime(2025, 9, 24, 0, 0, 0),
        'expected_end': datetime(2025, 9, 25, 0, 0, 0),
        'description': 'Datetime objects should pass through unchanged'
    },
    {
        'name': 'iso_format_string_parsing',
        'start': '2025-09-24T00:00:00',
        'end': '2025-09-25T00:00:00',
        'session': '15s_24hr',
        'expected_start': datetime(2025, 9, 24, 0, 0, 0),
        'expected_end': datetime(2025, 9, 25, 0, 0, 0),
        'description': 'ISO format strings should parse correctly'
    },
    {
        'name': 'date_string_parsing',
        'start': '2025-09-24',
        'end': '2025-09-25',
        'session': '15s_24hr',
        'expected_start': datetime(2025, 9, 24, 0, 0, 0),
        'expected_end': datetime(2025, 9, 25, 0, 0, 0),
        'description': 'Date-only strings should parse correctly'
    },
    {
        'name': 'datetime_string_with_space',
        'start': '2025-09-24 14:30:00',
        'end': '2025-09-24 16:30:00',
        'session': '1Hz_1hr',
        'expected_start': datetime(2025, 9, 24, 14, 30, 0),
        'expected_end': datetime(2025, 9, 24, 16, 30, 0),
        'description': 'Datetime strings with space separator should parse'
    },
]


# Archiving Test Cases (Immediate vs Bulk)
# ========================================

ARCHIVING_TEST_CASES = [
    {
        'name': 'immediate_archiving_single_file',
        'mode': 'immediate',
        'files': [
            {
                'tmp_path': TEST_TMP_DIR / 'ELDC' / 'ELDC202509240000a.sbf',
                'archive_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf.gz',
                'compress': True,
                'remove_tmp': True,
            }
        ],
        'expected_archived_count': 1,
        'expected_tmp_removed': True,
        'description': 'Immediate mode should archive file right away'
    },
    {
        'name': 'bulk_archiving_multiple_files',
        'mode': 'bulk',
        'files': [
            {
                'tmp_path': TEST_TMP_DIR / 'ELDC' / 'ELDC202509240000a.sbf',
                'archive_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.sbf.gz',
                'compress': True,
                'remove_tmp': True,
            },
            {
                'tmp_path': TEST_TMP_DIR / 'ELDC' / 'ELDC202509250000a.sbf',
                'archive_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509250000a.sbf.gz',
                'compress': True,
                'remove_tmp': True,
            },
        ],
        'expected_pending_count': 2,  # Before flush
        'expected_archived_count': 2,  # After flush
        'expected_tmp_removed': True,
        'description': 'Bulk mode should queue files and archive on flush'
    },
    {
        'name': 'uncompressed_archiving',
        'mode': 'immediate',
        'files': [
            {
                'tmp_path': TEST_TMP_DIR / 'ELDC' / 'ELDC202509240000a.T02',
                'archive_path': TEST_ARCHIVE_DIR / '2025' / 'sep' / 'ELDC' / '15s_24hr' / 'raw' / 'ELDC202509240000a.T02',
                'compress': False,  # NetR9/NetRS don't compress during download
                'remove_tmp': True,
            }
        ],
        'expected_archived_count': 1,
        'expected_compressed': False,
        'description': 'Should support archiving without compression'
    },
]


def get_test_case_by_name(cases: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    """Helper to retrieve test case by name."""
    for case in cases:
        if case['name'] == name:
            return case
    raise ValueError(f"Test case '{name}' not found")


def create_test_file(file_path: Path, size: int, is_compressed: bool = False,
                     gzip_magic: bytes = b'\x1f\x8b') -> None:
    """Helper to create test files with specific characteristics."""
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if size == 0:
        file_path.touch()
    else:
        if is_compressed and gzip_magic:
            # Write gzip magic bytes followed by random data
            with open(file_path, 'wb') as f:
                f.write(gzip_magic)
                f.write(b'X' * (size - 2))
        else:
            # Write uncompressed data
            with open(file_path, 'wb') as f:
                f.write(b'X' * size)