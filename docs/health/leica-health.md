# Leica G10 Health Monitoring

Health data extraction for Leica G10/GR10 receivers (Limited Capabilities).

## Overview

Leica G10/GR10 receivers have **limited health monitoring capabilities** compared to PolaRX5 and Trimble receivers. Health checks primarily rely on FTP connection testing and file timestamp analysis.

## Available Health Checks

### 1. Connection Health
- Router ping test
- FTP port accessibility (port 2160)
- FTP login capability
- Directory listing success

### 2. Data Quality Indicators
- Recent file presence
- File timestamp analysis
- Expected vs actual file count
- File size validation

## Health Data Extraction

### FTP Connection Test
```bash
# Test FTP connectivity
ftp <receiver-ip>
# Login with credentials
# List directory
ls

# Or using Python
from receivers.leica.ftp_client import LeicaFTPClient

client = LeicaFTPClient(host="receiver-ip", port=2160)
if client.connect():
    files = client.list_directory()
    print(f"Connection OK, {len(files)} files found")
```

### File Timestamp Analysis
```python
# Check if recent files exist
from datetime import datetime, timedelta

def check_recent_files(files, max_age_minutes=30):
    """Check if files are recent enough."""
    now = datetime.now()
    recent_files = [
        f for f in files
        if (now - f['timestamp']).total_seconds() < (max_age_minutes * 60)
    ]
    return len(recent_files) > 0
```

## Health Data Format

### G10 Health JSON (Minimal)
```json
{
  "station_id": "BLEI",
  "receiver_type": "G10",
  "timestamp": "2025-10-02T12:00:00Z",
  "connection": {
    "router_ping": {
      "status": "ok",
      "latency_ms": 8.5
    },
    "protocol": {
      "status": "ok",
      "type": "ftp",
      "port": 2160,
      "connected": true,
      "response_time_ms": 350
    }
  },
  "metrics": {
    "connection_quality": {
      "status": "ok",
      "response_time_ms": 350,
      "login_successful": true
    }
  },
  "data_quality": {
    "logging_status": "assumed_active",
    "sessions": {
      "15s_24hr": {
        "enabled": true,
        "last_file": "2025-10-02T11:30:00Z",
        "age_minutes": 30,
        "status": "ok",
        "file_count_24h": 48
      }
    }
  },
  "overall_status": "healthy",
  "extraction_metadata": {
    "note": "Limited health data available via FTP"
  }
}
```

## Automated Health Checks

```bash
# Basic G10 health check
receivers health BLEI

# Output:
# Station: BLEI
# Receiver Type: G10
# Overall Status: healthy
# Connection: ✅ (FTP accessible)
# Data Quality: ✅ (Recent files found)
# Note: Limited health metrics available
```

## Known Limitations

### No Direct Health Metrics
G10 receivers do not provide:
- ❌ Voltage readings
- ❌ Temperature sensors
- ❌ CPU load information
- ❌ Disk usage statistics
- ❌ Satellite tracking details via API

### Workarounds
1. **Infer health from data flow**: If files are being created regularly, receiver is likely healthy
2. **Monitor download success rate**: Frequent download failures may indicate issues
3. **File timestamp gaps**: Missing expected files could indicate problems
4. **Connection stability**: Track FTP connection success/failure rates

## Alternative Access Methods

### Serial/RS232 Connection
Some G10 models may provide health information via serial console:
```bash
# Connect via serial (if available)
screen /dev/ttyUSB0 115200

# Check if status commands available
# (manufacturer-specific commands)
```

### Web Interface
Some G10 configurations may have web interface:
```bash
# Try HTTP access (if enabled)
curl http://<receiver-ip>:80
# Or
curl http://<receiver-ip>:8080
```

## Health Monitoring Strategy

Given limitations, use a multi-indicator approach:

### Indicators of Healthy Receiver
✅ FTP connection succeeds consistently
✅ New files appear at expected intervals
✅ File sizes are within normal range
✅ No download errors
✅ Network latency is stable

### Indicators of Problems
❌ FTP connection failures
❌ Missing expected files
❌ Old file timestamps (>1 hour for hourly data)
❌ Repeated download errors
❌ High network latency or packet loss

## Future Improvements

### Research Needed
- [ ] Investigate Leica proprietary protocols
- [ ] Check if newer firmware provides health API
- [ ] Explore UNAVCO/manufacturer documentation for additional methods
- [ ] Test serial console access for health commands
- [ ] Evaluate SCPI commands if supported

### Potential Enhancements
- Integrate with Leica LGO software if available
- Monitor receiver logs if accessible
- Parse RINEX headers for receiver information
- Implement trend analysis on download success rates

## Resources

- [UNAVCO Leica GR10 Resources](https://kb.unavco.org/article/137/leica-gr10-resource-page-674.html)
- [UNAVCO GNSS Equipment KB](https://kb.unavco.org/category/gnss-and-related-equipment/2/)

---

**Status**: Development - Limited Capabilities
**Last Updated**: 2025-10-02
**Supported**: G10, GR10
**Note**: This receiver type requires further research for enhanced health monitoring
