# Receiver Utilities Usage Guide

Quick reference for using Phase 1 utilities in receiver implementations.

## ArchiveValidator

### Basic Usage

```python
from receivers.utils.archive_validator import ArchiveValidator

# Initialize
validator = ArchiveValidator(logger=self.logger)

# Validate single file
is_valid = validator.validate_archived_file(file_path)

# Find existing archive
found, path, location = validator.find_existing_archive(
    filename="ELDC202509240000a.sbf",
    archive_path="/path/to/archive/ELDC202509240000a.sbf",
    tmp_dir=Path("/tmp/downloads")
)

# Batch validation
missing_files, found_count, validated_count = validator.batch_validate_archives(
    files_dict={'file1.sbf': '/remote/path1', 'file2.sbf': '/remote/path2'},
    archive_files_dict={'file1.sbf': '/archive/file1.sbf.gz', ...},
    tmp_dir=Path("/tmp")
)
```

### Advanced Features

```python
# Custom minimum file size
validator = ArchiveValidator(min_file_size=512)

# Runtime configuration
validator.set_min_file_size(2048)

# Detailed validation report
report = validator.validate_with_detailed_report(file_path)
# Returns: {'valid': bool, 'file_exists': bool, 'file_size': int,
#           'meets_min_size': bool, 'compression_format': str,
#           'compression_valid': bool, 'errors': list}

# Register custom compression format
from receivers.utils.archive_validator import CompressionValidator

class ZstdValidator:
    def validate_magic_bytes(self, file_path):
        with open(file_path, 'rb') as f:
            return f.read(4) == b'\\x28\\xb5\\x2f\\xfd'
    def get_extension(self):
        return '.zst'

validator.register_compression_validator('.zst', ZstdValidator())
```

## TimeParameterProcessor

### Basic Usage

```python
from receivers.utils.time_processor import TimeParameterProcessor

# Initialize
processor = TimeParameterProcessor(logger=self.logger)

# Parse flexible datetime formats
dt = processor.parse_datetime_flexible("2025-09-24")  # Date string
dt = processor.parse_datetime_flexible("2025-09-24 14:30:00")  # Datetime string
dt = processor.parse_datetime_flexible("20250924-1430")  # Compact format
dt = processor.parse_datetime_flexible(datetime.now())  # Passthrough

# Process time parameters (handles defaults)
start, end = processor.process_time_parameters(
    start="2025-09-24",
    end="2025-09-25",
    session="15s_24hr"
)

# Calculate default time range (-D parameter logic)
start, end = processor.calculate_default_time_range(
    days_back=4,
    session="1Hz_1hr",
    reference_time=datetime.now()
)

# Normalize timestamps
normalized = processor.normalize_timestamp(
    dt=datetime(2025, 9, 24, 15, 30, 45),
    ffrequency="24hr"  # Returns midnight: 2025-09-24 00:00:00
)

normalized = processor.normalize_timestamp(
    dt=datetime(2025, 9, 24, 15, 30, 45),
    ffrequency="1hr"  # Returns hour boundary: 2025-09-24 15:00:00
)

# Normalize list of timestamps
normalized_list = processor.normalize_timestamps(
    dt_list=[datetime(...), datetime(...), ...],
    ffrequency="1hr"
)
```

### Advanced Features

```python
# Register custom datetime parser
from receivers.utils.time_processor import DatetimeParser

class GPSWeekDOYParser:
    def parse(self, dt_string):
        # Parse GPS week + DOY format
        ...
        return datetime_obj

    def get_format_description(self):
        return "GPS Week:DOY format (e.g., 2250:267)"

processor.register_parser(GPSWeekDOYParser())

# Register custom normalization strategy
from receivers.utils.time_processor import TimestampNormalization

processor.register_normalization_strategy(
    ffrequency='15min',
    strategy=TimestampNormalization.MINUTE_BOUNDARY
)

# Get human-readable time range description
description = processor.get_time_range_description(
    start=datetime(2025, 9, 24, 0, 0),
    end=datetime(2025, 9, 25, 0, 0),
    session="15s_24hr"
)
# Returns: "1 day(s) from 2025-09-24 to 2025-09-25"
```

## FileArchiver

### Immediate Mode (PolaRX5 Style)

```python
from receivers.utils.file_archiver import FileArchiver, ArchiveMode

# Context manager auto-flushes on exit
with FileArchiver(logger=self.logger, mode=ArchiveMode.IMMEDIATE) as archiver:
    for tmp_file in downloaded_files:
        success = archiver.archive_file(
            tmp_file=Path(tmp_file),
            archive_path=Path(archive_path),
            compress=True,
            remove_tmp=True
        )
        if success:
            # File is already archived, tmp file removed
            archived_files.append(archive_path)

# Get statistics
stats = archiver.get_statistics()
logger.info(f"Archived {stats['successful']}/{stats['total_files']} files")
logger.info(f"Average compression: {stats['average_compression_ratio']:.1f}%")
```

### Bulk Mode (NetR9/NetRS Style)

```python
# Queue all files during download, archive at end
with FileArchiver(logger=self.logger, mode=ArchiveMode.BULK) as archiver:
    for tmp_file in downloaded_files:
        # Just queues the file
        archiver.archive_file(
            tmp_file=Path(tmp_file),
            archive_path=Path(archive_path),
            compress=True,
            remove_tmp=True
        )

    # Pending count
    logger.info(f"Pending: {archiver.get_pending_count()} files")

    # Auto-flushes on context exit (or manually call flush_pending_archives())

# Get results
for result in archiver.get_results():
    if result.success:
        logger.info(f"✅ {result.archive_file.name}: {result.compression_ratio:.1f}% reduction")
    else:
        logger.error(f"❌ {result.tmp_file.name}: {result.error}")
```

### Batch Archiving

```python
# Drop-in replacement for receiver _archive_files() method
archiver = FileArchiver(mode=ArchiveMode.BULK)

archived_count = archiver.batch_archive_files(
    downloaded_files=['/tmp/file1.sbf', '/tmp/file2.sbf'],
    archive_files_dict={
        'file1.sbf': '/archive/file1.sbf.gz',
        'file2.sbf': '/archive/file2.sbf.gz'
    },
    compress=True,
    remove_tmp=True
)

logger.info(f"Archived {archived_count} files")
```

### Advanced Features

```python
# Register custom compression strategy
from receivers.utils.file_archiver import CompressionStrategy

class BrotliCompression:
    def compress(self, source, destination):
        import brotli
        with open(source, 'rb') as f_in:
            with open(destination, 'wb') as f_out:
                f_out.write(brotli.compress(f_in.read()))
        return True

    def get_extension(self):
        return '.br'

    def get_compression_ratio(self, source_size, compressed_size):
        return ((source_size - compressed_size) / source_size) * 100

archiver = FileArchiver()
archiver.register_compression_strategy('.br', BrotliCompression())

# Switch modes at runtime
archiver.set_mode(ArchiveMode.IMMEDIATE)

# Manual flush (if not using context manager)
archiver.flush_pending_archives()

# Get detailed results
results = archiver.get_results()
for result in results:
    print(result)  # ArchiveResult with full details

# Clear results history
archiver.clear_results()
```

## Integration Example

Complete example showing all utilities in a receiver:

```python
from pathlib import Path
from datetime import datetime
from receivers.base.receiver import BaseReceiver
from receivers.utils.archive_validator import ArchiveValidator
from receivers.utils.time_processor import TimeParameterProcessor
from receivers.utils.file_archiver import FileArchiver, ArchiveMode

class MyReceiver(BaseReceiver):
    def __init__(self, station_id, station_info):
        super().__init__(station_id, station_info)

        # Initialize utilities
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)
        # FileArchiver created per download for proper lifecycle

    def download_data(self, start, end, session, sync=True, archive=True, **kwargs):
        # Process time parameters
        start, end = self.time_processor.process_time_parameters(
            start, end, session
        )

        # Generate file lists
        files_dict, archive_files_dict = self._generate_file_list(start, end, session)

        # Batch validate existing archives
        missing_files, found_count, validated_count = self.archive_validator.batch_validate_archives(
            files_dict,
            archive_files_dict,
            tmp_dir=Path(self.tmp_dir) / self.station_id
        )

        self.logger.info(f"Validated {validated_count} files, {found_count} already in archive")
        self.logger.info(f"Missing files: {len(missing_files)}")

        if not missing_files:
            return {"status": "up_to_date", "files_downloaded": 0}

        # Download missing files
        downloaded_files = []
        if sync:
            downloaded_files = self._download_files(missing_files)

        # Archive files
        if archive and downloaded_files:
            # Choose mode based on receiver type
            mode = ArchiveMode.IMMEDIATE if self.immediate_archive else ArchiveMode.BULK

            with FileArchiver(logger=self.logger, mode=mode) as archiver:
                archived_count = archiver.batch_archive_files(
                    downloaded_files,
                    archive_files_dict,
                    compress=True,
                    remove_tmp=True
                )

            # Get statistics
            stats = archiver.get_statistics()
            self.logger.info(
                f"Archived {stats['successful']} files, "
                f"avg compression: {stats['average_compression_ratio']:.1f}%"
            )

        return {
            "status": "completed",
            "files_downloaded": len(downloaded_files),
            "files_archived": archived_count if archive else 0
        }
```

## Feature Flags

Enable utilities gradually in receivers.cfg:

```ini
[development]
use_archive_validator = true  # Enable ArchiveValidator
use_time_processor = true     # Enable TimeParameterProcessor
use_file_archiver = true      # Enable FileArchiver
```

Check flags in receiver:

```python
def __init__(self, ...):
    receivers_config = get_receivers_config()
    dev_config = receivers_config.get_receiver_config("development")

    if dev_config.get("use_archive_validator", False):
        self.archive_validator = ArchiveValidator(logger=self.logger)
    else:
        self.archive_validator = None  # Use old code path
```

---

**See also**:
- [Phase 1 Implementation](phase1_implementation.md) - Complete implementation details
- [API Documentation](api/) - Full API reference
- [Test Examples](../tests/) - Unit test examples