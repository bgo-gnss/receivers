# Receivers - GPS/GNSS Receiver Management Toolkit

A Python package for managing GPS/GNSS receivers, downloading data, and monitoring station health. Primarily designed for Septentrio PolaRX5 receivers in the Icelandic Met Office GPS network.

## 🚀 Quick Start

### Installation

```bash
# Development installation
cd receivers
pip install -e .

# With development dependencies  
pip install -e .[dev]
```

### Basic Usage

```bash
# Check receiver health (using ELDC as working example)
receivers health ELDC

# Download data with sync and archive
receivers download ELDC --sync --archive --days 7

# Download specific date range with progress bar
receivers download ELDC THOB --start 20250905 --end 20250906 --sync --archive

# Force clean download (restart partial files)
receivers download ELDC --sync --archive --clean_tmp --days 3

# Test connection before download
receivers download ELDC --sync --test-connection --archive

# Get station status
receivers status ELDC

# Extract health monitoring data from SBF files
python3 extract_health_bin2asc.py --station ORFC
python3 extract_health_bin2asc.py --station ELDC --session status_1hr
```

### Python API

```python
from receivers import PolaRX5

# Create receiver instance (requires station configuration)
receiver = PolaRX5("REYK", station_info)

# Check health
health = receiver.get_health_status()
print(f"Status: {health['overall_status']}")

# Download data
result = receiver.download_data(
    start="2024-01-15",
    end="2024-01-20", 
    sync=True
)
print(f"Downloaded {result['files_downloaded']} files")
```

## 📊 Health Monitoring System

The receivers package includes comprehensive GPS receiver health monitoring through SBF message extraction:

### Health Data Extraction

```bash
# Extract all health messages from ORFC station
python3 extract_health_bin2asc.py --station ORFC

# Extract from specific session type  
python3 extract_health_bin2asc.py --station ELDC --session status_1hr

# Process specific file pattern
python3 extract_health_bin2asc.py --station ORFC --pattern "*.sbf.gz"
```

### Health Message Types Monitored

- **PowerStatus**: Voltage monitoring (12.58V - 14.60V range detected)
- **DiskStatus**: Storage space and usage percentages
- **ReceiverStatus2**: Comprehensive receiver operational status
- **WiFiAPStatus**: Wireless access point connectivity
- **LogStatus**: Logging system health
- **NTRIPServerStatus**: NTRIP server operational status  
- **NTRIPClientStatus**: NTRIP client connectivity status

### Output Formats

**CSV Format** (optimized for grep/awk analysis):
```csv
2025-09-06T22:59:42Z,12.83
2025-09-06T23:00:42Z,12.83
2025-09-06T23:01:42Z,12.78
```

**JSON Lines Format** (structured for APIs and databases):
```json
{
  "gps_week": 2382,
  "gps_sow": 601200.0,
  "timestamp": "2025-09-06T22:59:42Z",
  "voltage": 12.83,
  "message_type": "PowerStatus"
}
```

### Key Features

- **RxTools Integration**: Uses Septentrio's bin2asc tool for precise SBF parsing
- **GPS Time Conversion**: Proper UTC timestamps via gtimes module (eliminates redundant time fields)
- **Consolidated Processing**: Appends data from multiple SBF files into single output files
- **Production Scale**: Processes 35+ SBF files extracting 1,680+ messages per health type
- **Dual Format Output**: Both CSV and JSON Lines for operational flexibility

## 🏗️ Architecture

### Receiver Classes

- `BaseReceiver`: Abstract base class defining common interface
- `PolaRX5`: Septentrio PolaRX5 implementation

### CLI Commands

- `receivers health STATION_ID`: Check receiver connectivity and status
- `receivers download STATION_ID`: Download data for specified period
- `receivers status STATION_ID`: Display detailed receiver information

### Design Principles

- **Modular**: Easy to add support for new receiver types
- **Unified Interface**: Consistent API across receiver types
- **Operational Ready**: Designed for 24/7 operational use
- **Rich Output**: Beautiful CLI output with Rich library

## 🔧 Development

### Current Status: Phase 1 MVP

✅ **Completed**:
- Modern package structure with pyproject.toml
- Abstract base receiver class  
- PolaRX5 implementation with modernized download logic
- Advanced CLI with getSeptentrio3 compatibility and progress bars
- Comprehensive file validation and integrity checking
- Partial download resumption with --clean_tmp support
- Remote file missing protection (locks local copies)
- Atomic file operations with proper tmp directory workflow
- Rich console output with detailed logging
- Type hints and comprehensive error handling

🔄 **In Progress**:
- Integration with gps_parser for station configuration (requires full receiver config data)
- Unit tests

✅ **Recently Completed**:
- **Comprehensive Health Monitoring**: Complete SBF health message extraction system
  - RxTools bin2asc integration for precise data extraction
  - GPS time conversion using gtimes module for proper UTC timestamps
  - Dual format output (CSV + JSON Lines) for operational flexibility
  - PowerStatus voltage monitoring (12.58V - 14.60V variations detected)
  - DiskStatus, ReceiverStatus2, WiFiAPStatus, LogStatus, NTRIP status monitoring
  - Consolidated multi-file processing with 1,680+ messages per health type
  - Production-ready extraction: `extract_health_bin2asc.py --station ORFC`

⚠️ **Current Configuration Status**:
- gps_parser package available but only contains basic station info (name, ID)
- Missing router/receiver connection details (IP, ports, etc.)  
- Using fallback configuration for testing (ELDC: 10.6.1.90:2160)
- Full integration requires completing station configuration files

🎯 **Future Integration Objective**:
- **tostools integration**: tostools will be enhanced to automatically update stations.cfg
- This will populate missing router/receiver connection details from operational data
- Enables seamless configuration management between TOS API and receivers package
- Reduces manual configuration maintenance and ensures data consistency

📋 **Planned**:
- Additional receiver types (Leica, NetRS, etc.)
- API integration endpoints
- Advanced health analytics
- Comprehensive documentation

### Testing

```bash
# Run tests (when available)
pytest tests/ -v

# Code quality
ruff check src/ tests/
black src/ tests/
mypy src/receivers/
```

## 📦 Dependencies

### Core Dependencies
- `gtimes>=0.4.0`: GPS time conversions and RINEX filename formatting
- `gps_parser`: Station configuration (local package)
- `rich>=13.0.0`: Console output and logging
- `tqdm>=4.60.0`: Modern progress bar with transfer speed and ETA

### Development Dependencies
- `pytest`: Testing framework
- `ruff`: Linting and formatting
- `mypy`: Type checking

## 🌐 Integration

This package is part of the GPS library ecosystem:

- **gtimes**: GPS time processing
- **gps_parser**: Station configuration management  
- **geo_dataread**: GPS data analysis
- **tostools**: TOS API integration → **Future objective: auto-update stations.cfg**

### Integration Roadmap

The receivers package will integrate with tostools to automatically maintain station configurations:

```mermaid
graph LR
    A[TOS API] --> B[tostools]
    B --> C[stations.cfg]
    C --> D[gps_parser]
    D --> E[receivers]
    E --> F[Station Health/Data]
```

This eliminates manual configuration maintenance and ensures operational data consistency.

## 📋 Configuration

Receivers require station configuration information including:

```python
station_info = {
    "router": {
        "ip": "10.6.1.90"  # Example: ELDC station
    },
    "receiver": {
        "ftpport": "2160"  # Port forward for FTP access
    }
}
```

Configuration is typically managed through the `gps_parser` package.

## 🚨 Operational Notes

### Septentrio PolaRX5 Specifics

- Uses FTP for data download with automatic passive/active mode selection
- Downloads SBF format files with RINEX naming convention (.sbf.gz)
- Supports multiple session types (15s_24hr, 1Hz_1hr, status_1hr)
- Real-time progress bars with transfer speed and ETA display
- Comprehensive file validation with size checking and corruption detection
- Automatic partial download resumption (resume from interruption)
- Remote file missing protection (preserves existing local files)
- getSeptentrio3 compatibility with modern enhancements

### Network Configuration

- Internal IMO network: Uses non-passive FTP (10.4.1.x, 10.4.2.x)
- External networks: Uses passive FTP by default  
- Port forwards available: 2160, 8060 for remote station access
- Configurable timeouts and retry logic

### Tested Stations

- **ELDC** (Eldvörp): 10.6.1.90:2160 - ✅ HEALTHY (solar/wind powered)
- **THOB** (Þorbjörn): Grid powered
- Additional stations require proper configuration setup

### File Organization

Downloaded files follow the structure:
```
/data/YYYY/MMM/STATION/SESSION/raw/
```

## 📁 Project Structure

```
receivers/
├── src/                      # Source code
├── tests/                    # Test suite
├── docs/                     # Documentation
│   ├── diagrams/            # Architecture diagrams (Mermaid)
│   ├── development/         # Development guides & changelog
│   ├── examples/            # Example scripts & sample data
│   ├── legacy/              # Legacy code (replaced by modern CLI)
│   └── RECEIVER_IMPLEMENTATION.md
├── CLAUDE.md                # AI assistant instructions
├── README.md                # This file
├── LICENSE                  # MIT License
├── pyproject.toml          # Python project configuration
└── environment.yml         # Conda environment
```

## 📄 License

MIT License - See LICENSE file for details.

## 🙏 Acknowledgments

- Based on original work by Fjalar Sigurdsson (fjalar@vedur.is)
- Continued development by Benedikt Gunnar Ófeigsson (bgo@vedur.is)
- Veðurstofan Íslands (Icelandic Met Office) for operational requirements