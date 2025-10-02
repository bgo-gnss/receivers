# GPS Receiver Health Monitoring

Comprehensive health monitoring system for GPS/GNSS receivers across the Icelandic Met Office's 173-station network.

## Overview

The health monitoring system provides:
- **Multi-level connection checks**: Router → HTTP port → Protocol-specific
- **Instrument-specific metrics**: Voltage, temperature, CPU, disk, satellites
- **Standardized data format**: Common JSON schema across all receiver types
- **Dual storage**: PostgreSQL database + JSON files
- **Monitoring integration**: Icinga alerts + Grafana dashboards

## Quick Start

```bash
# Basic connection health check
receivers health ELDC

# Full health extraction from instrument
receivers health ELDC --extract

# Save to database and JSON
receivers health ELDC --extract --save-db --save-json

# Multiple stations
receivers health ELDC THOB ORFC --extract

# Format for Icinga/Nagios
receivers health ELDC --format icinga --warn-temp 60 --crit-temp 70
```

## Documentation

### Core Documentation
- **[Health Data Specification](health-data-spec.md)** - Standardized JSON format
- **[Database Schema](database-schema.md)** - PostgreSQL checkcomm table
- **[Monitoring Integration](monitoring-integration.md)** - Icinga + Grafana setup

### Receiver-Specific Guides
- **[PolaRX5 Health](polarx5-health.md)** - Septentrio PolaRX5 with RxTools
- **[Trimble Health](trimble-health.md)** - NetR9/NetRS HTTP API
- **[Leica G10 Health](leica-health.md)** - G10 FTP-based monitoring

### Technical Specifications
- **[Specifications Directory](specifications/)** - Receiver manuals and API docs

## Architecture

```
┌─────────────────────────────────────────────────┐
│            Health Monitoring System              │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────────┐    ┌──────────────┐          │
│  │   health     │    │    status    │          │
│  │  subcommand  │    │  subcommand  │          │
│  │              │    │              │          │
│  │ (scheduled)  │    │  (manual)    │          │
│  └──────┬───────┘    └──────┬───────┘          │
│         │                   │                   │
│         └─────────┬─────────┘                   │
│                   │                             │
│         ┌─────────▼────────────┐                │
│         │ Connection Checker    │                │
│         │  - Router ping        │                │
│         │  - HTTP port test     │                │
│         │  - Protocol check     │                │
│         └───────────┬──────────┘                │
│                     │                           │
│         ┌───────────▼──────────────┐            │
│         │ Instrument Health Extract │            │
│         ├──────────────────────────┤            │
│         │ PolaRX5: RxTools bin2asc │            │
│         │ NetR9/RS: HTTP API       │            │
│         │ G10: FTP methods         │            │
│         └───────────┬──────────────┘            │
│                     │                           │
│         ┌───────────▼──────────────┐            │
│         │  Standardized JSON       │            │
│         │  (health-data-spec.md)   │            │
│         └───────────┬──────────────┘            │
│                     │                           │
│              ┌──────┴──────┐                    │
│              │             │                    │
│      ┌───────▼──────┐ ┌────▼──────┐            │
│      │  PostgreSQL  │ │   JSON    │            │
│      │  (checkcomm) │ │   Files   │            │
│      └───────┬──────┘ └────┬──────┘            │
│              │             │                    │
│       ┌──────▼──────┐ ┌────▼──────┐            │
│       │   Icinga    │ │  Grafana  │            │
│       │   Alerts    │ │ Dashboard │            │
│       └─────────────┘ └───────────┘            │
└─────────────────────────────────────────────────┘
```

## Data Flow

### 1. Connection Health Check
Every health check starts with 3 levels:
1. **Router/Network**: Can we ping the network device?
2. **HTTP Port**: Does the instrument respond on HTTP port?
3. **Protocol**: Can we establish FTP/HTTP connection?

### 2. Instrument Metrics Extraction
Receiver-specific methods extract health data:

**PolaRX5**:
- SBF status files → RxTools bin2asc → Parse health messages
- Messages: PowerStatus, DiskStatus, ReceiverStatus, WiFiStatus, etc.

**NetR9/NetRS**:
- HTTP API endpoints → Parse responses → Structure data
- Endpoints: /status, /voltage, /temperature, /tracking, etc.

**Leica G10**:
- FTP directory listing → File timestamps → Connection metrics
- Limited health data available via FTP

### 3. Data Storage

**PostgreSQL** (`checkcomm` table):
```sql
INSERT INTO checkcomm (
    sid, timestamp,
    rout_stat, recv_stat,
    recv_temp, recv_volt,
    recv_metrics, data_quality
) VALUES (...);
```

**JSON Files** (`status_1hr/health/`):
```
status_1hr/
└── health/
    ├── ELDC_20251002_1200.health.json
    ├── ELDC_20251002_1300.health.json
    └── ...
```

### 4. Monitoring Integration

**Icinga/Nagios**:
```bash
# Check command returning proper exit codes
receivers health ELDC --format icinga --warn-temp 60 --crit-temp 70
# Exit 0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN
```

**Grafana**:
- Reads from PostgreSQL checkcomm table
- Visualizes metrics over time
- Alert rules for threshold violations

## Usage Examples

### Basic Health Monitoring
```bash
# Single station health check
receivers health ELDC

# Expected output:
Station: ELDC
Receiver Type: PolaRX5
Overall Status: healthy
Timestamp: 2025-10-02 12:00:00
Connection: ✅ (ping: 5ms, http: 120ms, ftp: ok)
```

### Full Health Extraction
```bash
# Extract all available health metrics
receivers health ELDC --extract

# Sample output:
Health Report for ELDC (PolaRX5)
================================
Connection:
  ✅ Router ping: 5ms
  ✅ HTTP port (80): 120ms response
  ✅ FTP connection: OK

Metrics:
  Power: 12.3V (OK)
  Temperature: 45.2°C (OK)
  CPU Load: 25% (OK)
  Disk: 120GB free (60% used - OK)
  Satellites: 12 tracking (GOOD)
  Uptime: 100 days
```

### Database Storage
```bash
# Extract and save to database
receivers health ELDC --extract --save-db

# Check database
psql -h rek2.vedur.is -U gps -d gps -c "
    SELECT timestamp, recv_temp, recv_volt, recv_metrics->>'cpu_load'
    FROM checkcomm
    WHERE sid = 'ELDC'
    ORDER BY timestamp DESC
    LIMIT 5;
"
```

### Scheduled Monitoring
```yaml
# In scheduler.yaml
health_sessions:
  health_1hr:
    schedule: ":30"  # Every hour at :30
    enabled: true
    stations:
      - ELDC
      - THOB
      - ORFC
    options:
      extract: true
      save_db: true
      save_json: true
```

## Development

### Adding Health Metrics for New Receiver
1. Implement in receiver class: `def get_health_status(self) -> Dict[str, Any]`
2. Follow standardized JSON schema (see `health-data-spec.md`)
3. Add instrument-specific extractor if needed
4. Document in receiver-specific guide
5. Add tests

### Testing Health System
```bash
# Unit tests
pytest tests/test_health_checker.py -v

# Integration test with real receiver
receivers health ELDC --extract --verbose

# Test database write
receivers health ELDC --extract --save-db --verbose
```

## Troubleshooting

### Connection Issues
```bash
# Verbose connection diagnostics
receivers health ELDC --extract --verbose

# Test each level individually
ping <receiver-ip>
curl -v http://<receiver-ip>:80
ftp <receiver-ip>
```

### Missing Health Data
- **PolaRX5**: Check if RxTools is installed (`/opt/rxtools/bin/bin2asc`)
- **NetR9/NetRS**: Verify HTTP endpoints accessible
- **G10**: Limited health data available - expected

### Database Connection
```bash
# Test database connection
psql -h rek2.vedur.is -U gps -d gps -c "SELECT version();"

# Check configuration
cat ~/.config/gpsconfig/receivers.cfg | grep -A 5 "\[database\]"
```

## Resources

- [UNAVCO GNSS Equipment KB](https://kb.unavco.org/category/gnss-and-related-equipment/2/)
- [Septentrio PolaRX5 Resources](https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5#resources)
- [RxTools Documentation](https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools#resources)
- [Trimble NetR9 User Guide](https://epic.awi.de/id/eprint/52580/1/Trimble_NetR9_UserGuide_V4_15_RevA_2010.pdf)

---

**Version**: Development (feature/health-subcommand-improvements)
**Last Updated**: 2025-10-02
**Maintainer**: Veðurstofa Íslands GPS Team
