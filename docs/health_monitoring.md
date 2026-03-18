# Health Monitoring for PolaRX5 Receivers

## Overview

The receivers package now includes comprehensive health monitoring for Septentrio PolaRX5 receivers using the official RxTools `bin2asc` utility to extract health data from SBF (Septentrio Binary Format) files.

## Implementation

### RxTools Integration

The health monitoring system uses verified RxTools utilities:

- **Location**: `src/receivers/utils/rxtools_extractor.py`
- **Purpose**: Python wrapper around RxTools bin2asc command
- **Benefits**:
  - Official Septentrio-validated data extraction
  - Correct voltage scaling (verified: `raw_value / 640 = volts`)
  - CSV parsing with proper field name mapping
  - Support for compressed (.gz) and double-compressed files

### Health Data Extractor

- **Location**: `src/receivers/health/rxtools_extractor.py`
- **Purpose**: Extract health metrics from status_1hr SBF files
- **Extracted Blocks**:
  - **4101 PowerStatus**: Power supply voltage
  - **4014 ReceiverStatus**: CPU load, temperature, uptime
  - **4059 DiskStatus**: Internal disk usage percentage
  - **4082 QualityInd**: Tracked satellites, data quality

### CLI Commands

#### `receivers health <STATION>`

Get comprehensive health status for a receiver:

```bash
# Basic health check
receivers health THOB

# Verbose output with debug logging
receivers health THOB -v

# JSON output
receivers health THOB --json

# Save to JSON file
receivers health THOB --save-json

# Save to database
receivers health THOB --save-db
```

**Output includes**:
- Connection health (router ping, HTTP port, FTP/protocol connection)
- Power metrics (voltage with status: ok/warning/critical)
- CPU load and temperature
- Disk usage percentage
- Tracked satellites
- Receiver uptime

#### `receivers status <STATION>`

Quick connection status check (no SBF data extraction):

```bash
receivers status THOB
```

**Output**:
- Station ID and IP
- Router ping status
- Receiver FTP connection status

## Health Status Thresholds

### Voltage
- **Critical**: < 11.0V
- **Warning**: < 11.5V
- **OK**: ≥ 11.5V

Typical range for 12V power systems: 12-15V

### CPU Load
- **Critical**: > 90%
- **Warning**: > 75%
- **OK**: ≤ 75%

### Temperature
- **Critical**: > 70°C
- **Warning**: > 60°C
- **OK**: ≤ 60°C

### Disk Usage
- **Critical**: > 90%
- **Warning**: > 80%
- **OK**: ≤ 80%

## Data Sources

### Status Files

Health data is extracted from `status_1hr` session files:
- **Frequency**: 60 samples per hour (one per minute)
- **Format**: SBF binary format
- **Compression**: .sbf.gz (single or double compression)
- **Location**: `~/.gpsdata/septentrio/<STATION>/status_1hr/`

### Latest Sample

The health extractor uses the most recent sample from the status file:
- PowerStatus: Latest voltage reading
- ReceiverStatus: Latest CPU, temperature, uptime
- DiskStatus: Latest disk usage percentage
- QualityInd: Latest satellite count

## Testing

### Manual Testing

```bash
# Test health extraction from SBF file
python scripts/test_health_extraction.py /path/to/status.sbf.gz

# Test with decompressed file
python scripts/test_health_extraction.py /tmp/test_status_decompressed.sbf
```

### Example Output

```
============================================================
HEALTH DATA EXTRACTION RESULTS
============================================================

Extraction time: 2025-11-12T12:59:00Z

📊 METRICS:
  ✅ Voltage: 14.13 V [ok]
      Timestamp: 2025-11-12T12:59:00
  ✅ CPU Load: 23% [ok]
  ✅ Temperature: 45.2 C [ok]

📈 DATA QUALITY:
  ✅ Disk Usage: 35.2% [ok]
  📡 Tracked Satellites: 12

🔧 RECEIVER SPECIFIC:
  ⏱️  Uptime: 2345678s (651.6h / 27.1d)

============================================================
✅ Health extraction completed successfully
============================================================
```

## Voltage Verification

The voltage extraction has been verified against RxTools bin2asc output:

- **Test data**: status_1hr SBF files
- **Verification method**: Compared manual parser output with bin2asc CSV
- **Scaling factor**: 640 (confirmed: `raw_value / 640 = volts`)
- **Examples**:
  - Raw: 9185 → Voltage: 14.35V ✓
  - Raw: 9201 → Voltage: 14.38V ✓

See `resources/rxtools/examples/plot_voltage_timeseries.py` for voltage time series plotting.

## Future Integration

### Icinga Monitoring

Health data can be sent to Icinga endpoints:

```python
# Planned integration
receivers health THOB --icinga-endpoint https://monitoring.vedur.is/api

# Email alerts for critical status
receivers health THOB --alert-email gps-validation@vedur.is
```

### Grafana/Database

Health metrics can be stored for long-term monitoring:

```bash
# Save to PostgreSQL
receivers health THOB --save-db

# Export for Grafana
receivers health THOB --json > /var/lib/grafana/health_data/THOB.json
```

## Architecture

```
receivers health THOB
    ↓
CLI (cli/main.py:cmd_health)
    ↓
PolaRX5.get_health_status()
    ↓
RxToolsExtractor.extract_health_from_sbf()
    ↓
rxtools_extractor utilities:
  - extract_power_status()      (PowerStatus block 4101)
  - extract_receiver_status()   (ReceiverStatus2 block 4014)
  - extract_disk_status()       (DiskStatus block 4059)
  - extract_quality_ind()       (QualityInd block 4082)
    ↓
bin2asc (RxTools official utility)
  - Extracts SBF blocks to CSV
  - Validates field names and values
  - Handles all SBF revisions
```

## References

- **SBF Documentation**: `resources/documentation/README.md`
- **RxTools Manual**: `/usr/local/rxtools/rxtools_manual.pdf`
- **Block Analysis**: `resources/rxtools/README.md`
- **Voltage Plotting**: `resources/rxtools/examples/plot_voltage_timeseries.py`

---

**Created**: 2025-11-19
**Updated**: 2025-11-19
**Status**: ✅ Implemented and tested
**Next Steps**: Icinga integration, Grafana dashboards
