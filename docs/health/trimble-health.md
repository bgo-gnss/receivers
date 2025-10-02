# Trimble Receiver Health Monitoring

Health data extraction for Trimble NetR9 and NetRS receivers via HTTP API.

## Overview

Trimble receivers provide health information through HTTP API endpoints. Both NetR9 and NetRS share similar interfaces with slight variations.

## HTTP API Endpoints

### Common Endpoints (NetR9 & NetRS)

| Endpoint | Port | Description | Metrics Available |
|----------|------|-------------|-------------------|
| `/status` | 8060 | Overall receiver status | General health, tracking |
| `/voltage` | 8060 | Power supply voltage | Voltage readings |
| `/temperature` | 8060 | Internal temperature | Temperature readings |
| `/tracking` | 8060 | Satellite tracking | Satellites tracked/visible |
| `/logging` | 8060 | Logging status | Active sessions, errors |
| `/sessions` | 8060 | Session information | Session details |

## Health Data Extraction

### Connection Test
```bash
# Test HTTP port accessibility
curl -I http://<receiver-ip>:8060/status

# Expected: HTTP/1.1 200 OK
```

### Voltage Check
```bash
# Get voltage information
curl http://<receiver-ip>:8060/voltage

# Example response:
# Main Power: 12.3 V
# Backup Battery: 3.7 V
```

### Temperature Check
```bash
# Get temperature readings
curl http://<receiver-ip>:8060/temperature

# Example response:
# Internal Temperature: 45.2°C
```

### Satellite Tracking
```bash
# Get tracking information
curl http://<receiver-ip>:8060/tracking

# Example response:
# Tracking: 12 satellites
# GPS: 8, GLONASS: 4
```

## Python Integration

### Using TrimbleHealthParser

```python
from receivers.trimble.health_parser import TrimbleHealthParser

# Initialize parser
parser = TrimbleHealthParser(station_id="MANA", receiver_type="NetR9")

# Parse voltage response
voltage_response = http_get("http://receiver:8060/voltage")
voltage_data = parser.parse_voltage_response(voltage_response)
# Returns: {"value": 12.3, "unit": "V", "status": "ok"}

# Parse temperature response
temp_response = http_get("http://receiver:8060/temperature")
temp_data = parser.parse_temperature_response(temp_response)
# Returns: {"value": 45.2, "unit": "C", "status": "ok"}

# Create standardized health report
health_report = parser.create_standard_health_report({
    "voltage": voltage_data,
    "temperature": temp_data,
    "tracking": tracking_data
})
```

## Health Data Format

### NetR9 Health JSON
```json
{
  "station_id": "MANA",
  "receiver_type": "NetR9",
  "timestamp": "2025-10-02T12:00:00Z",
  "connection": {
    "http_port": {
      "status": "ok",
      "port": 8060,
      "response_time_ms": 120
    }
  },
  "metrics": {
    "power": {
      "voltage": 12.3,
      "unit": "V",
      "status": "ok"
    },
    "temperature": {
      "value": 45.2,
      "unit": "C",
      "status": "ok"
    },
    "satellites": {
      "tracking": 12,
      "status": "good"
    },
    "trimble": {
      "firmware_version": "4.85",
      "receiver_channels": 72
    }
  },
  "overall_status": "healthy"
}
```

## Automated Health Checks

```bash
# Health command automatically queries HTTP endpoints
receivers health MANA --extract

# Internally:
# 1. Test HTTP port 8060
# 2. Query /voltage endpoint
# 3. Query /temperature endpoint
# 4. Query /tracking endpoint
# 5. Query /logging endpoint
# 6. Parse all responses
# 7. Convert to standardized JSON
```

## Troubleshooting

### HTTP Port Not Accessible
```bash
# Check if receiver is online
ping <receiver-ip>

# Check if HTTP server is running
telnet <receiver-ip> 8060

# Check firewall rules
# NetR9/NetRS HTTP server runs on port 8060
```

### Empty or Malformed Responses
```bash
# Test endpoint directly
curl -v http://<receiver-ip>:8060/status

# Check response content-type
curl -I http://<receiver-ip>:8060/voltage
```

### Authentication Required
Some configurations may require authentication:
```bash
curl -u username:password http://<receiver-ip>:8060/status
```

## Resources

- [Trimble NetR9 User Guide](https://epic.awi.de/id/eprint/52580/1/Trimble_NetR9_UserGuide_V4_15_RevA_2010.pdf)
- [UNAVCO NetR9 Resources](https://kb.unavco.org/category/gnss-and-related-equipment/gnss-receivers/trimble/trimble-netr9/191/)
- [UNAVCO NetRS Resources](https://kb.unavco.org/article/trimble-netrs-resource-page-471.html)

---

**Status**: Development
**Last Updated**: 2025-10-02
**Supported**: NetR9, NetRS
