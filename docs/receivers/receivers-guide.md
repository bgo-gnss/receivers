# Individual Receiver Types Guide

This document provides detailed implementation guide for each supported GPS receiver type, including class structures, protocols, and specific configuration requirements.

## Leica G10 Receiver

The Leica G10 implementation handles GPS receivers manufactured by Leica Geosystems, using FTP-based file downloads.

```mermaid
classDiagram
    class LeicaG10 {
        -station_id: str
        -station_info: dict
        -leica_config: dict
        -ftp_downloader: LeicaFTPDownloader
        -file_validator: FileValidator
        +__init__(station_id, station_info, loglevel)
        +test_connection() dict
        +download_data(start, end, session, sync, clean_tmp, archive) dict
        -_generate_file_list(start, end, session) tuple
        -_process_zip_files(downloaded_files) list
        -_validate_archived_file(file_path) bool
    }

    class LeicaFTPDownloader {
        -station_id: str
        -ip: str
        -ftp_port: int
        -connect_timeout: int
        -data_timeout: int
        -use_passive: bool
        +download_file(remote_filename, local_path, remote_dir, expected_size, retry_count) bool
        +download_files(files_dict, tmp_dir, clean_tmp) list
        +test_connection() dict
        -_download_file_single_attempt(remote_filename, local_path, remote_dir, expected_size) bool
    }

    class ProgressBar {
        -total_size: int
        -filename: str
        -width: int
        -current_size: int
        -start_time: float
        +update(bytes_downloaded) void
        +finish() void
        -_display_progress() void
    }

    class BaseReceiver {
        <<abstract>>
        +station_id: str
        +station_info: dict
        +build_path(datetime_list, template, session, frequency) list
        +get_file_extension() str
        +test_connection()* dict
        +download_data(start, end, session, sync, clean_tmp, archive)* dict
    }

    class FileValidator {
        +validate_file(file_path) dict
        +clean_directory(directory_path) int
    }

    LeicaG10 --|> BaseReceiver
    LeicaG10 --> LeicaFTPDownloader
    LeicaG10 --> FileValidator
    LeicaFTPDownloader --> ProgressBar

    note for LeicaG10 "Handles Leica G10 receivers\nFTP downloads from /SD Card/Data/\nZIP file processing (.m00.zip → .m00)\nDOY-based filenames (STATION{DOY}a.m00.zip)"

    note for LeicaFTPDownloader "FTP client optimized for Leica G10\nActive mode (passive=false)\nBinary transfer mode\nRetry logic with exponential backoff\nProgress tracking with speed/ETA"
```

**Diagram Source**: [diagrams/leica-g10.mmd](diagrams/leica-g10.mmd)

### Leica G10 Implementation Details

**File Path**: `src/receivers/leica/g10.py`

#### Key Characteristics
- **Protocol**: FTP (Anonymous login)
- **Port**: 2160 (non-standard)
- **Connection Mode**: Active (passive=false) - Critical for data transfer success
- **File Format**: ZIP compressed (.m00.zip → .m00 → .m00.gz)
- **Directory Structure**: Flat structure directly under session directories

#### Session Support
- **15s_24hr**: Daily files in `/SD Card/Data/15s_24hr/`
- **1Hz_1hr**: Hourly files in `/SD Card/Data/1s_1hr/STATION/YYYY/MM/DD/`
- **status_1hr**: Status files (future implementation)

#### Unique Features
- **ZIP Processing**: Only receiver type that downloads compressed files
- **DOY Filenames**: Uses day-of-year format (267a.m00.zip for Sept 24)
- **Active FTP**: Requires active mode for data connections
- **Binary Mode**: Explicit binary mode setting prevents corruption

#### Critical Configuration
```ini
[leica]
ftp_port = 2160
ftp_passive = false
ftp_timeout_connect = 90
ftp_timeout_data = 600
```

## NetR9 Receiver

Trimble NetR9 receivers use HTTP-based downloads with a cache directory structure.

```mermaid
classDiagram
    class NetR9 {
        -station_id: str
        -station_info: dict
        -netr9_config: dict
        -http_downloader: HTTPDownloadClient
        +__init__(station_id, station_info, loglevel)
        +test_connection() dict
        +download_data(start, end, session, sync, clean_tmp, archive) dict
        -_generate_file_list(start, end, session) tuple
        -_build_cache_dir_structure(dt, session) str
        -_build_remote_url(dt, session, station_id) str
    }

    class HTTPDownloadClient {
        -station_id: str
        -base_url: str
        -http_port: int
        -connect_timeout: int
        -stall_timeout: int
        +download_file(url, local_path, expected_size) bool
        +download_files(files_dict, tmp_dir, clean_tmp) list
        +test_connection(base_url) dict
    }

    class BaseReceiver {
        <<abstract>>
        +station_id: str
        +station_info: dict
        +build_path(datetime_list, template, session, frequency) list
        +get_file_extension() str
        +test_connection()* dict
        +download_data(start, end, session, sync, clean_tmp, archive)* dict
    }

    NetR9 --|> BaseReceiver
    NetR9 --> HTTPDownloadClient

    note for NetR9 "Trimble NetR9 receiver\nHTTP downloads from port 8060\nCache directory structure\n/Internal/YYYYMM/session/\nFilename: STATIONYYYYMMDDHHMM{letter}.T02"

    note for HTTPDownloadClient "HTTP client for Trimble receivers\nStream downloads with progress\nStall timeout (no data timeout)\nResume capability for large files"
```

**Diagram Source**: [diagrams/netr9.mmd](diagrams/netr9.mmd)

### NetR9 Implementation Details

**File Path**: `src/receivers/trimble/netr9.py`

#### Key Characteristics
- **Protocol**: HTTP
- **Port**: 8060
- **File Format**: Raw T02 files (no compression)
- **Directory Structure**: Nested cache directory structure
- **URL Pattern**: `http://station.domain:8060/CACHEDIR.../download/Internal/YYYYMM/session/`

#### Session Support
- **15s_24hr**: Daily files with 'a' suffix
- **1Hz_1hr**: Hourly files with 'b' suffix
- **status_1hr**: Status files with 'c' suffix

#### Unique Features
- **Cache Directory**: Complex nested directory structure for organization
- **Stream Downloads**: HTTP streaming for large files
- **Stall Timeout**: Progress-based timeout (120s without data)

## NetRS Receiver

Trimble NetRS receivers use a simpler HTTP-based approach compared to NetR9.

```mermaid
classDiagram
    class NetRS {
        -station_id: str
        -station_info: dict
        -netrs_config: dict
        -http_downloader: NetRSHTTPDownloadClient
        +__init__(station_id, station_info, loglevel)
        +test_connection() dict
        +download_data(start, end, session, sync, clean_tmp, archive) dict
        -_generate_file_list(start, end, session) tuple
    }

    class NetRSHTTPDownloadClient {
        -station_id: str
        -base_url: str
        -http_port: int
        -connect_timeout: int
        -stall_timeout: int
        +download_file(url, local_path, expected_size) bool
        +download_files(files_dict, tmp_dir, clean_tmp) list
        +test_connection(base_url) dict
    }

    class BaseReceiver {
        <<abstract>>
        +station_id: str
        +station_info: dict
        +build_path(datetime_list, template, session, frequency) list
        +get_file_extension() str
        +test_connection()* dict
        +download_data(start, end, session, sync, clean_tmp, archive)* dict
    }

    NetRS --|> BaseReceiver
    NetRS --> NetRSHTTPDownloadClient

    note for NetRS "Trimble NetRS receiver\nHTTP downloads from port 8060\nSimpler directory structure\n/download/YYYYMM/session/\nFilename: STATIONYYYYMMDDHHMM{letter}.T00"

    note for NetRSHTTPDownloadClient "HTTP client specific to NetRS\nDifferent URL patterns than NetR9\nT00 file extension\nSame timeout and stall logic"
```

**Diagram Source**: [diagrams/netrs.mmd](diagrams/netrs.mmd)

### NetRS Implementation Details

**File Path**: `src/receivers/trimble/netrs.py`

#### Key Characteristics
- **Protocol**: HTTP
- **Port**: 8060
- **File Format**: Raw T00 files (no compression)
- **Directory Structure**: Simple `/download/YYYYMM/session/` pattern
- **File Extension**: .T00 (vs .T02 for NetR9)

#### Session Support
- **15s_24hr**: Daily files in `/download/YYYYMM/a/`
- **1Hz_1hr**: Hourly files in `/download/YYYYMM/b/`
- **status_1hr**: Status files in `/download/YYYYMM/c/`

#### Differences from NetR9
- **Simpler URLs**: No cache directory complexity
- **T00 Extension**: Different file extension
- **Directory Mapping**: Session letters map directly to directories

## Unified Architecture Features

### Common Base Class
All receivers inherit from `BaseReceiver` which provides:
- **Unified Path Generation**: `build_path()` method using gtimes templates
- **Configuration Management**: Consistent config loading
- **Session Parameter Parsing**: Standardized session handling
- **Archive Template Support**: Common archiving patterns

### Timestamp Normalization
**Critical Feature**: All receivers now use consistent timestamp normalization:

```python
# Daily files (15s_24hr): Always normalize to midnight
if ffrequency == "24hr":
    adjusted_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
else:
    # Hourly files: Use actual hour boundaries
    adjusted_dt = dt.replace(minute=0, second=0, microsecond=0)
```

This ensures consistent archive naming:
- Daily files: `STATION202509240000a.ext.gz`
- Hourly files: `STATION202509241500b.ext.gz`

### Error Handling & Reliability

#### Connection Management
- **Timeout Handling**: Separate connect and data timeouts
- **Retry Logic**: Exponential backoff for failed operations
- **Protocol-Specific**: Optimized for each receiver's characteristics

#### File Validation
- **Integrity Checks**: Size and basic structure validation
- **Archive Mapping**: Efficient filename to archive path mapping
- **Resume Capability**: Handle partial downloads gracefully

#### Progress Monitoring
- **Real-time Progress**: Speed, ETA, and percentage completion
- **Stall Detection**: Different strategies per protocol
- **User Feedback**: Clear indication of download status

### Configuration Management

Each receiver type has specific configuration in `~/.config/gpsconfig/receivers.cfg`:

```ini
[leica]
protocol = ftp
ftp_port = 2160
ftp_passive = false
# ... other Leica-specific settings

[netr9]
protocol = http
http_port = 8060
# ... other NetR9-specific settings

[netrs]
protocol = http
http_port = 8060
# ... other NetRS-specific settings
```

This modular approach allows easy addition of new receiver types while maintaining operational consistency across Iceland's diverse GPS receiver network.

## Troubleshooting by Receiver Type

### Leica G10 Common Issues
- **Connection Refused**: Check `ftp_passive = false` setting
- **File Corruption**: Ensure binary mode is enabled
- **Slow Downloads**: Increase `ftp_timeout_data` setting
- **ZIP Errors**: Verify file integrity after download

### NetR9/NetRS Common Issues
- **HTTP 404 Errors**: Verify URL patterns and cache directory structure
- **Stall Timeouts**: Increase `http_stall_timeout` for slow connections
- **File Not Found**: Check session directory mapping
- **Large File Issues**: Monitor progress-based timeout behavior

### Configuration Debugging
```bash
# Test specific receiver type
receivers download STATION --test-connection -v

# Check configuration loading
receivers validate STATION --verbose

# Monitor download progress
receivers download STATION -D 1 --sync -v
```