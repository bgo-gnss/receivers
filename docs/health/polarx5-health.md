# PolaRX5 Health Monitoring

Health data extraction for Septentrio PolaRX5 receivers using RxTools.

## Overview

PolaRX5 receivers provide comprehensive health data through SBF (Septentrio Binary Format) status sessions. RxTools `bin2asc` converts these binary files to human-readable format for parsing.

## Prerequisites

### RxTools Installation

**Download**: https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools#resources

**Installation**:
```bash
# Extract RxTools package
tar -xzf RxTools-<version>-Linux64.tar.gz
sudo mv RxTools /opt/rxtools

# Add to PATH
echo 'export PATH=$PATH:/opt/rxtools/bin' >> ~/.bashrc
source ~/.bashrc

# Verify installation
bin2asc --version
```

## Health Message Types

PolaRX5 status sessions contain 8 health message types:

| Message Type | SBF ID | Description | Key Metrics |
|--------------|--------|-------------|-------------|
| **PowerStatus** | 4101 | Power supply information | Voltage, power source |
| **DiskStatus** | 4059 | Internal storage status | Free space, usage % |
| **ReceiverStatus** | 4014 | Overall receiver status | CPU load, uptime, errors |
| **ReceiverStatus2** | 4014 | Extended receiver status | Additional status flags |
| **WiFiAPStatus** | 4054 | WiFi access point status | Connected clients, signal |
| **LogStatus** | 4102 | Logging session status | Active sessions, errors |
| **NTRIPServerStatus** | 4122 | NTRIP server status | Client connections |
| **NTRIPClientStatus** | 4053 | NTRIP client status | Connection, corrections age |

## Data Extraction Process

### 1. Status Session Download

Status sessions are automatically downloaded hourly (status_1hr):

```bash
receivers download ELDC --session status_1hr --sync --archive
```

File location:
```
data/<year>/<month>/<station>/status_1hr/
└── raw/sbf/
    ├── ELDC0010.25__
    └── ELDC0011.25__
```

### 2. Binary to ASCII Conversion

Use RxTools `bin2asc` to extract health messages:

```bash
# Extract specific message type
bin2asc -f ReceiverStatus ELDC0010.25__ > ELDC0010_receiver_status.asc

# Extract all health messages
bin2asc -f PowerStatus,DiskStatus,ReceiverStatus,WiFiAPStatus,LogStatus,NTRIPServerStatus,NTRIPClientStatus \
    ELDC0010.25__ > ELDC0010_health.asc

# With pretty formatting
bin2asc -p -f ReceiverStatus ELDC0010.25__ > ELDC0010_receiver_status_pretty.asc
```

### 3. Parse ASCII Output

Python parsing example:

```python
def parse_receiver_status(line: str) -> dict:
    """Parse ReceiverStatus ASCII line."""
    parts = line.strip().split()
    if len(parts) >= 5 and parts[0] == '-7':
        return {
            'block_type': int(parts[0]),
            'gps_time': float(parts[1]),
            'cpu_load': int(parts[2]),
            'uptime': int(parts[3]),
            'rx_status': parts[4]
        }
    return None
```

## Health Data Structure

### PowerStatus (4101)
```json
{
  "power": {
    "voltage": 12.3,
    "unit": "V",
    "status": "ok",
    "power_source": "external",
    "battery_charge": 95
  }
}
```

### DiskStatus (4059)
```json
{
  "disk": {
    "total_gb": 200,
    "used_gb": 80,
    "free_gb": 120,
    "percent_used": 40,
    "status": "ok"
  }
}
```

### ReceiverStatus (4014)
```json
{
  "cpu_load": {
    "percent": 25,
    "status": "ok"
  },
  "uptime": {
    "seconds": 8640000,
    "days": 100.0
  },
  "receiver_status": "0x00000000",
  "receiver_error": "0x00000000"
}
```

### WiFiAPStatus (4054)
```json
{
  "wifi": {
    "enabled": true,
    "ssid": "IMO_GPS",
    "clients_connected": 2,
    "status": "ok"
  }
}
```

### NTRIPClientStatus (4053)
```json
{
  "ntrip_client": {
    "enabled": true,
    "connected": true,
    "server": "rtcm.vedur.is",
    "mountpoint": "ISREF",
    "age_correction_seconds": 2.1,
    "status": "ok"
  }
}
```

## Automated Extraction

### Using extract_health_bin2asc.py

```bash
# Extract health from status session
python3 examples/extract_health_bin2asc.py --station ELDC --date 20251002

# Extract and save to JSON
python3 examples/extract_health_bin2asc.py --station ELDC --output json

# Extract and save to database
python3 examples/extract_health_bin2asc.py --station ELDC --save-db
```

### Integration with Health Command

```bash
# Health command uses RxTools automatically
receivers health ELDC --extract

# Under the hood:
# 1. Find latest status_1hr SBF file
# 2. Run bin2asc to extract health messages
# 3. Parse ASCII output
# 4. Convert to standardized JSON format
# 5. Optionally save to database/file
```

## Troubleshooting

### RxTools Not Found
```bash
# Check installation
which bin2asc

# Add to PATH if needed
export PATH=$PATH:/opt/rxtools/bin

# Verify version
bin2asc --version
```

### Empty ASCII Output
```bash
# Check SBF file is valid
file ELDC0010.25__

# List message types in file
bin2asc -l ELDC0010.25__

# Verify health messages present
bin2asc -s ELDC0010.25__ | grep -E "PowerStatus|ReceiverStatus"
```

### Parse Errors
```python
# Enable verbose parsing
receivers health ELDC --extract --verbose

# Check ASCII format
head -20 ELDC0010_health.asc

# Validate with RxTools
bin2asc -p -f ReceiverStatus ELDC0010.25__
```

## Resources

- [PolaRX5 Reference Manual](https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5#resources)
- [RxTools Documentation](https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools#resources)
- [SBF Reference Guide](https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5#resources) - SBF message specifications

---

**Status**: Development
**Last Updated**: 2025-10-02
**Required**: RxTools installation
