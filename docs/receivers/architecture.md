# System Architecture

This document provides a comprehensive overview of the GPS receivers package architecture, detailing how components interact to provide reliable GPS data collection across Iceland's 173-station network.

## System Overview

The receivers package implements a layered, extensible architecture supporting multiple GPS receiver manufacturers and communication protocols. The system is designed for reliability, consistency, and operational simplicity while handling the complexity of diverse hardware types.

## Architecture Layers

### Layer 1: CLI & API Interface
**Purpose**: User interaction and system control

- **Command Line Interface**: Primary user interaction point
- **Configuration Management**: Station and receiver type configuration
- **Factory Pattern**: Automatic receiver type detection and instantiation

### Layer 2: Receiver Abstraction
**Purpose**: Unified interface for diverse receiver types

- **Base Receiver Class**: Common interface and functionality
- **Manufacturer Implementations**: Leica, Trimble, Septentrio specific logic
- **Session Management**: Standardized session types across all receivers

### Layer 3: Protocol Layer
**Purpose**: Communication protocol abstraction

- **FTP Client**: For Leica G10 and PolaRX5 receivers
- **HTTP Client**: For NetR9/NetRS Trimble receivers
- **TCP Client**: For direct PolaRX5 connections
- **Progress Tracking**: Real-time download monitoring

### Layer 4: Processing Layer
**Purpose**: Data validation and preparation

- **File Validation**: Integrity checking and corruption detection
- **Format Processing**: ZIP extraction, file format handling
- **Archive Preparation**: Compression and naming standardization

### Layer 5: Storage Layer
**Purpose**: Organized data persistence

- **Temporary Storage**: Download staging area
- **Archive Storage**: Long-term organized storage
- **Audit Logging**: Operational tracking and monitoring

## Key Design Patterns

### Factory Pattern for Receiver Creation
The `ReceiverFactory` automatically detects receiver types based on configuration:

```python
# Automatic receiver type detection
receiver = ReceiverFactory.create_receiver(station_id, station_config)
```

Benefits:
- **Extensibility**: Easy addition of new receiver types
- **Configuration-Driven**: No code changes needed for new stations
- **Error Handling**: Clear messages for unsupported or misconfigured stations

### Unified Path Generation
All receivers use the same `build_path()` method for consistent file naming:

```python
# Unified datetime and path generation
datetime_list = self.build_path(None, "#datelist", session, frequency, start, end)
archive_paths = self.build_path(datetime_list, archive_template, session, frequency)
```

Benefits:
- **Consistency**: Same naming patterns across all receiver types
- **gtimes Integration**: Leverages existing GPS time handling
- **Template Support**: Flexible filename and path patterns

### Protocol Abstraction
Download clients are abstracted behind receiver-specific interfaces:

```python
# Each receiver uses appropriate protocol
leica_receiver.ftp_downloader.download_files(files_dict, tmp_dir)
netr9_receiver.http_downloader.download_files(files_dict, tmp_dir)
```

Benefits:
- **Protocol Optimization**: Each client optimized for its protocol
- **Fault Tolerance**: Protocol-specific retry and error handling
- **Maintainability**: Clear separation of concerns

## Data Flow Architecture

### 1. Request Processing Flow
```
CLI Command → Argument Parsing → Configuration Loading → Receiver Factory → Receiver Instance
```

### 2. File Discovery Flow
```
Time Range → DateTime Generation → Remote Path Building → Archive Path Building → File Mapping
```

### 3. Validation Flow
```
File List → Archive Check → Temp Check → Validation → Missing List Generation
```

### 4. Download Flow
```
Missing Files → Protocol Selection → Download Execution → Progress Tracking → Success Validation
```

### 5. Processing Flow
```
Downloaded Files → Format Processing → Validation → Compression → Archive Storage → Cleanup
```

## Critical Features

### Timestamp Normalization
**Problem**: Inconsistent archive naming based on download time
**Solution**: Normalize timestamps based on file type

```python
if ffrequency == "24hr":
    # Daily files always use midnight timestamp
    adjusted_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
else:
    # Hourly files use actual hour boundaries
    adjusted_dt = dt.replace(minute=0, second=0, microsecond=0)
```

**Result**: Consistent archive naming regardless of download time:
- Daily files: `STATION202509240000a.ext.gz`
- Hourly files: `STATION202509241500b.ext.gz`

### Archive Validation System
Multi-location file checking prevents unnecessary downloads:

1. **Primary Archive**: Check main archive directory
2. **Compressed Archive**: Check for .gz compressed version
3. **Temporary Files**: Check temp directory for existing downloads
4. **Integrity Validation**: Verify file size and basic structure

### Fault Tolerance Design
- **Connection Retry**: Exponential backoff for failed connections
- **Download Resume**: Protocol-specific resume capabilities where supported
- **Partial Recovery**: Continue processing remaining files if individual downloads fail
- **Graceful Degradation**: Clear error reporting without system crashes

## Receiver-Specific Implementations

### Leica G10 Architecture
```
LeicaG10 → LeicaFTPDownloader → ProgressBar
    ↓
FileValidator → ZIP Processing → Archive Storage
```

**Unique Features**:
- FTP active mode (passive=false)
- ZIP file processing pipeline
- DOY-based filename format
- Binary mode requirement

### NetR9/NetRS Architecture
```
NetR9/NetRS → HTTPDownloadClient → Stream Processing
    ↓
FileValidator → Direct Archiving → Storage
```

**Unique Features**:
- HTTP streaming downloads
- Cache directory structure (NetR9) vs simple structure (NetRS)
- Stall timeout rather than connection timeout
- Different file extensions (.T02 vs .T00)

### PolaRX5 Architecture (Future)
```
PolaRX5 → FTP/TCP Client → SBF Processing
    ↓
FileValidator → Compression → Archive Storage
```

**Planned Features**:
- Dual protocol support (FTP/TCP)
- SBF format handling
- Real-time data streaming capability

## Configuration Architecture

### Hierarchical Configuration
```
System Defaults → Receiver Type Config → Station Specific Config → Runtime Parameters
```

### Configuration Sources
1. **`receivers.cfg`**: Receiver type defaults and protocol settings
2. **`stations.cfg`**: Station-specific network and authentication settings
3. **Environment Variables**: Runtime configuration overrides
4. **Command Line**: Session-specific parameters

### Configuration Validation
- **Startup Validation**: Check all required configuration parameters
- **Runtime Validation**: Verify configurations before operations
- **Error Reporting**: Clear messages for missing or invalid configurations

## Performance Considerations

### Network Efficiency
- **Connection Reuse**: Maintain connections across multiple file downloads
- **Progress Monitoring**: Only download truly missing files
- **Batch Operations**: Process multiple files efficiently
- **Protocol Optimization**: Use each protocol's strengths

### Storage Efficiency
- **Immediate Compression**: Automatic .gz compression reduces storage by ~70%
- **Organized Structure**: Year/month hierarchy for efficient navigation
- **Archive Validation**: Prevent duplicate storage
- **Cleanup Management**: Automatic temporary file removal

### Memory Management
- **Streaming Downloads**: Handle large files without excessive memory usage
- **Progress Tracking**: Efficient update mechanisms
- **Resource Cleanup**: Proper connection and file handle management

## Monitoring & Observability

### Success Metrics
- Files validated, found, downloaded per operation
- Download speeds and completion times
- Archive storage efficiency ratios

### Error Tracking
- Connection failure patterns with specific error types
- File corruption detection and recovery actions
- Configuration validation failures

### Audit Trail
- Complete download operation logging
- Archive operations with timestamps
- Performance metrics for trend analysis

## Future Architecture Considerations

### Scalability Enhancements
- **Concurrent Downloads**: Parallel processing for multiple stations
- **Distributed Storage**: Support for networked storage systems
- **Load Balancing**: Distribute load across multiple receiver connections

### Protocol Expansions
- **SFTP Support**: Secure file transfer protocols
- **WebDAV**: Web-based distributed authoring and versioning
- **Cloud Storage**: Direct integration with cloud storage services

### Monitoring Integration
- **Health Checks**: Automated receiver health monitoring
- **Alerting Systems**: Integration with monitoring infrastructure
- **Performance Dashboards**: Real-time operational visibility

This architecture provides a solid foundation for reliable GPS data collection while maintaining flexibility for future enhancements and receiver types.