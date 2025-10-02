# Health Data Specification

Standardized JSON format for GPS receiver health data across all receiver types.

## Overview

All receiver types (PolaRX5, NetR9, NetRS, G10) must output health data in this standardized format to ensure:
- **Consistency**: Same structure regardless of receiver type
- **Extensibility**: Easy to add new metrics
- **Compatibility**: Works with database, monitoring systems, and analysis tools
- **Validation**: Clear schema for automated validation

## Core Schema

```json
{
  "station_id": "ELDC",
  "receiver_type": "PolaRX5",
  "timestamp": "2025-10-02T12:00:00Z",
  "schema_version": "1.0",
  "connection": {
    "router_ping": {
      "status": "ok",
      "latency_ms": 5.2,
      "packet_loss": 0
    },
    "http_port": {
      "status": "ok",
      "port": 80,
      "response_time_ms": 120,
      "accessible": true
    },
    "protocol": {
      "status": "ok",
      "type": "ftp",
      "port": 21,
      "connected": true,
      "response_time_ms": 250
    }
  },
  "metrics": {
    "power": {
      "voltage": 12.3,
      "unit": "V",
      "status": "ok",
      "threshold_warning": 11.5,
      "threshold_critical": 10.0
    },
    "temperature": {
      "value": 45.2,
      "unit": "C",
      "status": "ok",
      "threshold_warning": 60.0,
      "threshold_critical": 70.0
    },
    "cpu_load": {
      "percent": 25,
      "status": "ok",
      "threshold_warning": 80,
      "threshold_critical": 95
    },
    "memory": {
      "used_mb": 512,
      "total_mb": 2048,
      "percent": 25,
      "status": "ok"
    },
    "disk": {
      "used_gb": 80,
      "total_gb": 200,
      "free_gb": 120,
      "percent_used": 40,
      "status": "ok",
      "threshold_warning": 80,
      "threshold_critical": 90
    },
    "satellites": {
      "tracking": 12,
      "visible": 15,
      "status": "good",
      "threshold_warning": 4,
      "threshold_critical": 2
    },
    "uptime": {
      "seconds": 8640000,
      "days": 100.0,
      "last_restart": "2025-06-24T12:00:00Z"
    }
  },
  "data_quality": {
    "logging_status": "active",
    "sessions": {
      "15s_24hr": {
        "enabled": true,
        "last_file": "2025-10-02T11:45:00Z",
        "age_minutes": 15,
        "status": "ok"
      },
      "1Hz_1hr": {
        "enabled": true,
        "last_file": "2025-10-02T11:55:00Z",
        "age_minutes": 5,
        "status": "ok"
      },
      "status_1hr": {
        "enabled": true,
        "last_file": "2025-10-02T11:55:00Z",
        "age_minutes": 5,
        "status": "ok"
      }
    },
    "data_gaps": {
      "last_24h": 0,
      "last_7d": 2
    }
  },
  "network": {
    "ntrip_client": {
      "enabled": true,
      "connected": true,
      "server": "rtcm.vedur.is",
      "mountpoint": "ISREF",
      "age_correction_seconds": 2.1,
      "status": "ok"
    },
    "ntrip_server": {
      "enabled": false
    }
  },
  "overall_status": "healthy",
  "status_summary": {
    "healthy": 15,
    "warning": 0,
    "critical": 0,
    "unknown": 1
  },
  "extraction_metadata": {
    "extraction_time": "2025-10-02T12:00:15Z",
    "extraction_duration_ms": 1250,
    "data_source": "sbf_status_session",
    "tool_version": "0.1.0"
  }
}
```

## Field Definitions

### Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `station_id` | string | Yes | 4-character station identifier (e.g., "ELDC") |
| `receiver_type` | string | Yes | Receiver model (PolaRX5, NetR9, NetRS, G10) |
| `timestamp` | string | Yes | ISO 8601 timestamp when health was measured |
| `schema_version` | string | Yes | Schema version for compatibility (current: "1.0") |
| `connection` | object | Yes | Connection health at all levels |
| `metrics` | object | Yes | Instrument health metrics |
| `data_quality` | object | No | Data logging and quality metrics |
| `network` | object | No | Network service status (NTRIP, etc.) |
| `overall_status` | string | Yes | Summary status: `healthy`, `warning`, `critical`, `unknown` |
| `status_summary` | object | Yes | Count of metrics by status |
| `extraction_metadata` | object | Yes | Metadata about data extraction |

### Connection Object

**Purpose**: Multi-level connection health verification

```json
{
  "router_ping": {
    "status": "ok|warning|critical|unknown",
    "latency_ms": 5.2,
    "packet_loss": 0
  },
  "http_port": {
    "status": "ok|error|unknown",
    "port": 80,
    "response_time_ms": 120,
    "accessible": true
  },
  "protocol": {
    "status": "ok|error|unknown",
    "type": "ftp|http|tcp",
    "port": 21,
    "connected": true,
    "response_time_ms": 250
  }
}
```

**Status Values**:
- `ok`: Connection successful
- `warning`: Connection degraded (high latency, packet loss)
- `critical`: Connection failed or severely degraded
- `error`: Connection error
- `unknown`: Cannot determine status

### Metrics Object

Health metrics vary by receiver type. All metrics follow this pattern:

```json
{
  "metric_name": {
    "value": <number>,
    "unit": "V|C|%|GB|count",
    "status": "ok|warning|critical|unknown",
    "threshold_warning": <number>,
    "threshold_critical": <number>
  }
}
```

**Common Metrics**:

**power**:
- `voltage` (V): Power supply voltage
- Status: `ok` (>11.5V), `warning` (<11.5V), `critical` (<10V)

**temperature**:
- `value` (°C): Internal temperature
- Status: `ok` (<60°C), `warning` (60-70°C), `critical` (>70°C)

**cpu_load**:
- `percent` (%): CPU utilization
- Status: `ok` (<80%), `warning` (80-95%), `critical` (>95%)

**memory**:
- `used_mb`, `total_mb`, `percent`
- Status: `ok` (<80%), `warning` (80-90%), `critical` (>90%)

**disk**:
- `used_gb`, `total_gb`, `free_gb`, `percent_used`
- Status: `ok` (<80%), `warning` (80-90%), `critical` (>90%)

**satellites**:
- `tracking` (count): Satellites being tracked
- `visible` (count): Satellites visible
- Status: `good` (>8), `fair` (4-8), `poor` (<4)

**uptime**:
- `seconds`, `days`: System uptime
- `last_restart`: ISO 8601 timestamp

### Data Quality Object

**Purpose**: Monitor data logging and completeness

```json
{
  "logging_status": "active|inactive|error",
  "sessions": {
    "session_name": {
      "enabled": true,
      "last_file": "2025-10-02T11:45:00Z",
      "age_minutes": 15,
      "status": "ok|warning|critical"
    }
  },
  "data_gaps": {
    "last_24h": 0,
    "last_7d": 2
  }
}
```

**Session Status**:
- `ok`: Recent file (<10min old for hourly, <30min for daily)
- `warning`: Moderately old file
- `critical`: Very old file or missing

### Network Object

**Purpose**: Monitor network services (NTRIP, WiFi, etc.)

```json
{
  "ntrip_client": {
    "enabled": true,
    "connected": true,
    "server": "rtcm.vedur.is",
    "mountpoint": "ISREF",
    "age_correction_seconds": 2.1,
    "status": "ok|warning|error"
  },
  "ntrip_server": {
    "enabled": false
  },
  "wifi": {
    "enabled": true,
    "connected": true,
    "ssid": "IMO_GPS",
    "signal_strength": -45,
    "status": "ok"
  }
}
```

### Overall Status

Determined by aggregating all metric statuses:

```python
if any(status == "critical"):
    overall_status = "critical"
elif any(status == "warning"):
    overall_status = "warning"
elif all(status == "ok"):
    overall_status = "healthy"
else:
    overall_status = "unknown"
```

## Receiver-Specific Extensions

### PolaRX5-Specific Metrics

```json
{
  "metrics": {
    // ... standard metrics ...
    "septentrio": {
      "firmware_version": "5.4.0",
      "receiver_status": "0x00000000",
      "receiver_error": "0x00000000",
      "quality_ind": {
        "main_antenna_status": 0,
        "main_antenna_power": 5.2
      },
      "pvt_mode": "3D",
      "pvt_error": 0.023
    }
  }
}
```

### Trimble-Specific Metrics

```json
{
  "metrics": {
    // ... standard metrics ...
    "trimble": {
      "firmware_version": "4.85",
      "receiver_channels": 72,
      "pdop": 1.8,
      "hdop": 1.2,
      "vdop": 1.4
    }
  }
}
```

### Leica-Specific Metrics

```json
{
  "metrics": {
    // ... standard metrics ...
    "leica": {
      "firmware_version": "4.30",
      "antenna_status": "ok",
      "tracking_channels": 60
    }
  }
}
```

## Minimal Health Data

For receivers with limited health capabilities (e.g., G10):

```json
{
  "station_id": "BLEI",
  "receiver_type": "G10",
  "timestamp": "2025-10-02T12:00:00Z",
  "schema_version": "1.0",
  "connection": {
    "router_ping": {
      "status": "ok",
      "latency_ms": 8.5
    },
    "http_port": {
      "status": "unknown",
      "accessible": false
    },
    "protocol": {
      "status": "ok",
      "type": "ftp",
      "connected": true
    }
  },
  "metrics": {
    "connection_quality": {
      "status": "ok",
      "response_time_ms": 350
    }
  },
  "data_quality": {
    "logging_status": "assumed_active",
    "sessions": {
      "15s_24hr": {
        "enabled": true,
        "last_file": "2025-10-02T11:30:00Z",
        "age_minutes": 30,
        "status": "ok"
      }
    }
  },
  "overall_status": "healthy",
  "status_summary": {
    "healthy": 2,
    "warning": 0,
    "critical": 0,
    "unknown": 1
  },
  "extraction_metadata": {
    "extraction_time": "2025-10-02T12:00:15Z",
    "extraction_duration_ms": 450,
    "data_source": "ftp_connection_test",
    "tool_version": "0.1.0",
    "note": "Limited health data available via FTP"
  }
}
```

## Schema Validation

### Python Validation Example

```python
from typing import Dict, Any
import jsonschema

HEALTH_SCHEMA = {
    "type": "object",
    "required": [
        "station_id", "receiver_type", "timestamp",
        "schema_version", "connection", "metrics",
        "overall_status", "status_summary", "extraction_metadata"
    ],
    "properties": {
        "station_id": {"type": "string", "minLength": 4, "maxLength": 4},
        "receiver_type": {"type": "string", "enum": ["PolaRX5", "NetR9", "NetRS", "G10"]},
        "timestamp": {"type": "string", "format": "date-time"},
        "schema_version": {"type": "string"},
        "overall_status": {
            "type": "string",
            "enum": ["healthy", "warning", "critical", "unknown"]
        },
        # ... full schema ...
    }
}

def validate_health_data(health_data: Dict[str, Any]) -> bool:
    """Validate health data against schema."""
    try:
        jsonschema.validate(health_data, HEALTH_SCHEMA)
        return True
    except jsonschema.ValidationError as e:
        print(f"Validation error: {e}")
        return False
```

## Usage in Code

### Generating Health Data

```python
from receivers.health.health_schema import HealthDataBuilder

# Initialize builder
builder = HealthDataBuilder(station_id="ELDC", receiver_type="PolaRX5")

# Add connection health
builder.add_connection_health(
    router_ping={"status": "ok", "latency_ms": 5.2},
    http_port={"status": "ok", "response_time_ms": 120},
    protocol={"status": "ok", "type": "ftp"}
)

# Add metrics
builder.add_metric("power", value=12.3, unit="V", status="ok")
builder.add_metric("temperature", value=45.2, unit="C", status="ok")
builder.add_metric("cpu_load", percent=25, status="ok")

# Build final health data
health_data = builder.build()

# Validate
if builder.validate():
    print("Health data valid!")
```

### Reading Health Data

```python
from receivers.health.health_reader import HealthDataReader

# Load from JSON file
health = HealthDataReader.from_json_file("ELDC_20251002_1200.health.json")

# Access data
print(f"Station: {health.station_id}")
print(f"Status: {health.overall_status}")
print(f"Temperature: {health.metrics.temperature.value}°C")
print(f"Satellites: {health.metrics.satellites.tracking}")

# Check specific status
if health.is_critical():
    print("CRITICAL: Receiver needs attention!")
```

## Database Mapping

See [database-schema.md](database-schema.md) for how this JSON maps to PostgreSQL `checkcomm` table.

## Version History

- **1.0** (2025-10-02): Initial specification
  - Core schema with connection, metrics, data_quality
  - Receiver-specific extensions
  - Validation framework

---

**Version**: 1.0
**Last Updated**: 2025-10-02
**Status**: Development
