# GPS Health Time-Series JSON Schema v2.0

**Status**: Implementation in progress
**Created**: 2025-11-20
**Purpose**: Daily time-series health data extraction from SBF status files

---

## Overview

This schema defines the structure for daily GPS receiver health data files. Each file contains:
- Complete time-series data (typically 1440 samples/day from status_1hr session)
- Aggregated statistics (daily and hourly)
- Data provenance and quality metadata
- Source file tracking for intelligent downloading

## File Location and Naming

**Directory Pattern**:
```
{data_prepath}/{year}/{month}/{station}/status_1hr/json/
```

**Filename Pattern**:
```
{STATION}_{YYYYMMDD}_health.json
```

**Examples**:
```
/data/2025/nov/ISFS/status_1hr/json/ISFS_20251119_health.json
/data/2025/oct/ELEY/status_1hr/json/ELEY_20251029_health.json
```

---

## Schema Version 2.0 Structure

```json
{
  "station_id": "ISFS",
  "receiver_type": "PolaRX5",
  "date": "2025-11-19",
  "schema_version": "2.0",
  "sample_count": 1440,
  "time_range": {
    "start": "2025-11-19T00:00:00Z",
    "end": "2025-11-19T23:59:00Z"
  },

  "timeseries": [ /* array of samples */ ],
  "aggregated": { /* statistics */ },
  "data_files": { /* source file tracking */ },
  "extraction_metadata": { /* extraction info */ }
}
```

---

## Top-Level Fields

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `station_id` | string | Station identifier (uppercase, e.g., "ISFS", "ELEY") |
| `receiver_type` | string | Receiver model (e.g., "PolaRX5", "NetR9", "G10") |
| `date` | string | ISO 8601 date (YYYY-MM-DD) for this data |
| `schema_version` | string | Schema version (currently "2.0") |
| `sample_count` | integer | Total number of samples in timeseries array |
| `time_range` | object | Start and end timestamps |
| `timeseries` | array | Time-series sample data (see below) |
| `aggregated` | object | Statistical aggregations (see below) |
| `extraction_metadata` | object | Extraction provenance (see below) |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `data_files` | object | Source file tracking and status |

---

## Time-Series Data Structure

### Format

Time-series data is stored as an **array of samples**, one object per timestamp. Each sample contains all available metrics for that time.

### Sample Object

```json
{
  "time": "2025-11-19T00:00:00Z",
  "voltage": {"value": 14.25, "unit": "V"},
  "cpu_load": {"value": 23.5, "unit": "%"},
  "temperature": {"value": 45.2, "unit": "C"},
  "disk_usage": {"value": 35.2, "unit": "%"},
  "satellites": {
    "total": 12,
    "by_system": {
      "GPS": 6,
      "GLONASS": 4,
      "Galileo": 2,
      "BeiDou": 0
    }
  }
}
```

### Metric Fields

All metric fields follow the pattern: `{"value": <number>, "unit": "<string>"}`

**Standard Metrics** (from SBF PowerStatus, ReceiverStatus, DiskStatus blocks):

| Metric | Unit | SBF Block | Field Name | Typical Range |
|--------|------|-----------|------------|---------------|
| `voltage` | V | 4101 (PowerStatus) | Vin Voltage [V] | 12-15V |
| `cpu_load` | % | 4014 (ReceiverStatus2) | CPU Load [%] | 0-100% |
| `temperature` | C | 4014 (ReceiverStatus2) | Temperature [degC] | 40-70°C |
| `disk_usage` | % | 4059 (DiskStatus) | Disk Usage [%] | 0-100% |

**Satellite Data** (from SBF ChannelStatus and QualityInd blocks):

```json
"satellites": {
  "total": 12,           // Total tracked satellites (from QualityInd block 4082)
  "by_system": {         // Per-constellation breakdown (from ChannelStatus block 4013)
    "GPS": 6,
    "GLONASS": 4,
    "Galileo": 2,
    "BeiDou": 0
  }
}
```

### Missing Data Handling

**Missing Fields**: If a metric is not available for a timestamp, **omit the field entirely** (do not use `null` or `"N/A"`).

**Example - Missing temperature**:
```json
{
  "time": "2025-11-19T00:01:00Z",
  "voltage": {"value": 14.26, "unit": "V"},
  "cpu_load": {"value": 23.8, "unit": "%"},
  // temperature omitted - not available for this sample
  "disk_usage": {"value": 35.2, "unit": "%"},
  "satellites": {...}
}
```

**Missing Timestamps**: If an entire timestamp has no data, omit it from the array.

**Receiver Capabilities**: Check `extraction_metadata.receiver_capabilities` to distinguish:
- Field not in capabilities → Receiver doesn't support this metric
- Field in capabilities but missing from sample → Temporary gap

---

## Aggregated Statistics

### Structure

```json
"aggregated": {
  "daily": { /* daily statistics for each metric */ },
  "hourly": [ /* hourly statistics, 24 objects */ ]
}
```

### Daily Aggregation

Statistics computed across all samples for the day:

```json
"daily": {
  "voltage": {
    "mean": 14.25,
    "std": 0.15,
    "min": 14.10,
    "max": 14.40,
    "unit": "V",
    "samples": 1440
  },
  "cpu_load": {
    "mean": 23.5,
    "std": 2.3,
    "min": 18.0,
    "max": 32.0,
    "unit": "%",
    "samples": 1440
  },
  "satellites": {
    "total": {
      "mean": 11.8,
      "std": 1.2,
      "min": 9,
      "max": 14,
      "samples": 1440
    },
    "by_system": {
      "GPS": {
        "mean": 6.2,
        "std": 0.8,
        "min": 5,
        "max": 8,
        "samples": 1440
      },
      "GLONASS": {...},
      "Galileo": {...},
      "BeiDou": {...}
    }
  }
}
```

### Hourly Aggregation

Statistics for each hour (0-23):

```json
"hourly": [
  {
    "hour": 0,
    "voltage": {
      "mean": 14.23,
      "std": 0.12,
      "min": 14.10,
      "max": 14.35,
      "unit": "V",
      "samples": 60
    },
    "cpu_load": {...},
    "temperature": {...},
    "satellites": {
      "total": {...},
      "by_system": {...}
    }
  },
  {
    "hour": 1,
    ...
  }
  // ... hours 2-23
]
```

**Note**: If no data available for a metric in a time period, omit that metric from the aggregation.

---

## Data Files Tracking

### Purpose

Track which source SBF files were processed and their status. This enables:
- Intelligent re-downloading of missing files
- Avoiding repeated failed downloads
- Tracking data completeness

### Structure

```json
"data_files": {
  "status_1hr": {
    "expected_files": 24,
    "files": [
      {
        "filename": "ISFS202511190000c.sbf.gz",
        "status": "included",
        "samples_extracted": 60,
        "file_size_bytes": 45231
      },
      {
        "filename": "ISFS202511191500c.sbf.gz",
        "status": "missing_on_receiver",
        "last_check": "2025-11-20T10:30:00Z"
      },
      {
        "filename": "ISFS202511192300c.sbf.gz",
        "status": "not_downloaded"
      }
    ]
  },
  "15s_24hr": {
    "expected_files": 1,
    "files": [
      {
        "filename": "ISFS319024hr.sbf.gz",
        "status": "included",
        "file_size_bytes": 125678900
      }
    ]
  },
  "1Hz_1hr": {
    "expected_files": 24,
    "files": [...]
  }
}
```

### File Status Values

| Status | Meaning | Action |
|--------|---------|--------|
| `included` | File was processed and data extracted | None |
| `missing_on_receiver` | File doesn't exist on receiver (confirmed) | Don't retry |
| `not_downloaded` | File exists on receiver but not in archive | Download it |
| `corrupted` | File exists but is corrupted/unreadable | Re-download |
| `empty` | File exists but contains no health data | Investigate |

---

## Extraction Metadata

### Structure

```json
"extraction_metadata": {
  "extracted_at": "2025-11-20T10:30:00Z",
  "extractor_version": "2.0",
  "missing_hours": [15],
  "data_quality": {
    "completeness": 95.8,
    "gaps_detected": 1,
    "corrupted_samples": 0
  },
  "receiver_capabilities": {
    "metrics": {
      "voltage": true,
      "cpu_load": true,
      "temperature": true,
      "disk_usage": false
    },
    "satellites": {
      "total": true,
      "by_system": true
    }
  }
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `extracted_at` | string | ISO 8601 timestamp when extraction occurred |
| `extractor_version` | string | Version of extraction code (matches schema_version) |
| `missing_hours` | array[int] | List of hours (0-23) with no data |
| `data_quality.completeness` | float | Percentage of expected samples present (0-100) |
| `data_quality.gaps_detected` | int | Number of data gaps found |
| `data_quality.corrupted_samples` | int | Number of corrupted/invalid samples |
| `receiver_capabilities` | object | Which metrics this receiver type supports |

---

## Receiver Type Variations

### PolaRX5 (Full Support)

All metrics available:
```json
{
  "receiver_type": "PolaRX5",
  "timeseries": [{
    "voltage": {...},
    "cpu_load": {...},
    "temperature": {...},
    "disk_usage": {...},
    "satellites": {
      "total": 12,
      "by_system": {...}
    }
  }]
}
```

### Trimble NetR9 (Partial Support)

Limited metrics (from HTTP interface):
```json
{
  "receiver_type": "NetR9",
  "timeseries": [{
    "voltage": {...},
    "temperature": {...},
    "satellites": {
      "total": 10
      // by_system omitted - not available
    }
  }],
  "extraction_metadata": {
    "receiver_capabilities": {
      "metrics": {
        "voltage": true,
        "cpu_load": false,
        "temperature": true,
        "disk_usage": false
      },
      "satellites": {
        "total": true,
        "by_system": false
      }
    }
  }
}
```

### Leica G10 (Minimal Support)

Connection only:
```json
{
  "receiver_type": "G10",
  "timeseries": [],  // No health metrics available
  "extraction_metadata": {
    "receiver_capabilities": {
      "metrics": {
        "voltage": false,
        "cpu_load": false,
        "temperature": false,
        "disk_usage": false
      },
      "satellites": {
        "total": false,
        "by_system": false
      }
    }
  }
}
```

---

## Usage Examples

### Reading Voltage Time Series

```python
import json

with open('ISFS_20251119_health.json') as f:
    data = json.load(f)

# Extract voltage time series
voltages = []
for sample in data['timeseries']:
    if 'voltage' in sample:
        voltages.append({
            'time': sample['time'],
            'value': sample['voltage']['value']
        })

# Plot voltage
import matplotlib.pyplot as plt
times = [v['time'] for v in voltages]
values = [v['value'] for v in voltages]
plt.plot(times, values)
plt.ylabel(f"Voltage ({data['timeseries'][0]['voltage']['unit']})")
plt.show()
```

### Checking Data Completeness

```python
# Check if receiver supports all metrics
caps = data['extraction_metadata']['receiver_capabilities']

if caps['metrics']['voltage']:
    print(f"Voltage: {data['aggregated']['daily']['voltage']['mean']}V")
else:
    print("Voltage: Not supported by this receiver")

# Check data quality
quality = data['extraction_metadata']['data_quality']
print(f"Completeness: {quality['completeness']}%")
print(f"Missing hours: {data['extraction_metadata']['missing_hours']}")
```

### Querying Satellite Counts by System

```python
# Get daily average satellites per GNSS system
sats_by_sys = data['aggregated']['daily']['satellites']['by_system']

for system, stats in sats_by_sys.items():
    print(f"{system}: {stats['mean']:.1f} avg (range: {stats['min']}-{stats['max']})")

# Output:
# GPS: 6.2 avg (range: 5-8)
# GLONASS: 3.5 avg (range: 2-5)
# Galileo: 2.1 avg (range: 1-3)
```

---

## Migration from v1.0

### Key Differences

| Aspect | v1.0 (Single Sample) | v2.0 (Time Series) |
|--------|---------------------|-------------------|
| **Location** | `status_1hr/health/` | `status_1hr/json/` |
| **Filename** | `{STATION}_{YYYYMMDD}_{HHMMSS}.json` | `{STATION}_{YYYYMMDD}_health.json` |
| **Data** | Single snapshot | Full day time series |
| **Samples** | 1 | 1440 (typical) |
| **Statistics** | None | Daily + hourly aggregations |
| **File tracking** | No | Yes (`data_files` section) |
| **Use case** | Real-time status check | Historical analysis, trending |

### Backward Compatibility

- v1.0 files (single sample) remain in `status_1hr/health/`
- v2.0 files (time series) created in `status_1hr/json/`
- Both can coexist during transition period
- Use `schema_version` field to distinguish

---

## Validation

### Required Validations

1. **Schema version**: Must be "2.0"
2. **Time range**: `start` < `end`, both ISO 8601
3. **Sample count**: Must match `timeseries` array length
4. **Timestamps**: Must be ascending order in time series
5. **Units**: Must match specified units for each metric
6. **Statistics**: `samples` count must match data used

### Optional Validations

1. **Completeness**: Compare `sample_count` to `expected_files * 60`
2. **Gaps**: Verify `missing_hours` matches actual gaps in data
3. **Ranges**: Check min/max in aggregations match timeseries data
4. **Capabilities**: Verify only declared metrics are present

---

## Future Enhancements (Potential)

### Possible Extensions (Not in v2.0)

1. **Additional SBF Blocks**:
   - WiFiAPStatus (4054) - WiFi clients
   - LogStatus (4102) - Logging sessions
   - NTRIPServerStatus (4122) - NTRIP connections
   - ReceiverSetup (4027) - Firmware version

2. **Satellite Details**:
   - Individual satellite PRNs
   - Signal strength (C/N0) per satellite
   - Elevation/azimuth per satellite

3. **Data Quality Metrics**:
   - Multipath indicators
   - Cycle slip counts
   - Position accuracy metrics

4. **Compression**:
   - Optional gzip compression of JSON files
   - Reduced precision for floats

---

## References

- **SBF Format**: Septentrio Binary Format documentation
- **RxTools**: Septentrio RxTools bin2asc utility
- **ISO 8601**: Date/time format standard
- **Nagios**: Performance data format (used in Icinga integration)

---

**Document Version**: 1.0
**Last Updated**: 2025-11-20
**Maintained By**: GPS Receivers Package Development Team
