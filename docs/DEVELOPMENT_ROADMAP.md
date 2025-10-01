# Development Roadmap - Receivers Package

**Status**: Pre-production development and testing
**Target**: DevOps environment deployment (2-3 months)

## Current Phase: Scheduler Testing & Stabilization

### Completed Features ✅
- APScheduler-based bulk download system
- Flexible schedule syntax (cron, intervals, multiple times/day)
- Session filtering by receiver type (status_1hr only for polarx5)
- lookback_periods support (download multiple past periods)
- Immediate archiving with fault tolerance
- Distribution windows to prevent network congestion

### Active Testing 🔄
- **Gradual Load Testing Plan**:
  - Phase 1: 15 stations (current)
  - Phase 2: 30 stations
  - Phase 3: 50 stations
  - Phase 4: 100 stations
  - Phase 5: 173 stations (full network)
- **Metrics**: Timing, network performance, reliability at each scale
- **lookback_periods**: Testing with values 1-10 to find optimal settings

## Phase 1: Documentation & Git Workflow

**Timeline**: After scheduler testing complete

### Tasks
1. **Documentation**
   - Document scheduler implementation details
   - Update CLAUDE.md with production guidelines
   - Create operator manual for scheduler commands
   - Document configuration options

2. **Git Workflow Transition**
   - Commit current work to main
   - Switch to feature branch workflow
   - Implement PR process (self-review initially)
   - Set up branch naming conventions

### Success Criteria
- All current features documented
- Clean main branch ready for feature branches
- PR template and workflow established

---

## Phase 2: Health Monitoring System

**Timeline**: After Phase 1 complete
**Complexity**: High - Major new feature

### Overview
Real-time health monitoring and alerting system for the 173-station GNSS network.

### Data Flow
```
status_1hr/raw/*.sbf (polarx5 only)
    ↓ [Extract health metrics - receiver-specific extractors]
status_1hr/ascii/*.json (receiver-independent format)
    ↓ [Dual output]
    ├→ PostgreSQL (Grafana time series visualization)
    └→ Icinga2 (Real-time monitoring & alerts)
```

### Architecture Requirements

#### 1. Receiver-Independent Interface
```python
class HealthExtractor(ABC):
    """Abstract base for receiver-specific health extraction."""
    @abstractmethod
    def extract_health(self, raw_file: Path) -> HealthData:
        """Extract health metrics from raw receiver file."""
        pass
```

**Implementations needed**:
- `PolarX5HealthExtractor` - Primary (status_1hr .sbf files)
- `NetR9HealthExtractor` - Future (limited health data)
- `NetRSHealthExtractor` - Future (limited health data)
- `G10HealthExtractor` - Future (limited health data)

#### 2. Health Data Schema
```json
{
  "station_id": "THOB",
  "timestamp": "2025-10-01T16:00:00Z",
  "receiver_type": "polarx5",
  "metrics": {
    "tracking_status": "OK|WARNING|CRITICAL",
    "satellites_tracked": 15,
    "disk_usage_percent": 45.2,
    "temperature_celsius": 28.5,
    "voltage_volts": 12.1,
    "connection_quality": "good|degraded|poor"
  },
  "flags": {
    "disk_full": false,
    "temperature_warning": false,
    "low_voltage": false
  }
}
```

#### 3. Scheduler Integration
New scheduled job type: `health_processing`
```yaml
sessions:
  status_1hr:
    enabled: true
    schedule: ":25"
    lookback_periods: 1
    process_health: true  # NEW: Trigger health processing after download

health_processing:
  enabled: true
  schedule: ":30"  # 5 minutes after status_1hr downloads
  lookback_periods: 1
  postgres_connection: "postgresql://grafana:***@db.vedur.is/gps_health"
  icinga_endpoint: "https://icinga.vedur.is/gps/health"
```

#### 4. Smart Recovery System
**Use health data to detect station recovery and trigger backfill**:

```python
def check_station_recovery(station_id: str) -> Optional[RecoveryPlan]:
    """Check if station came back online, plan data recovery."""
    last_seen = get_last_successful_health_check(station_id)
    current_health = get_current_health(station_id)

    if last_seen and current_health.is_online:
        offline_duration = current_health.timestamp - last_seen
        return RecoveryPlan(
            station_id=station_id,
            start_time=last_seen,
            end_time=current_health.timestamp,
            sessions=['15s_24hr', '1Hz_1hr'],
            priority='high' if offline_duration > timedelta(days=1) else 'normal'
        )
    return None
```

#### 5. Database Schema (PostgreSQL)
```sql
CREATE TABLE station_health (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(4) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    receiver_type VARCHAR(20),
    satellites_tracked INTEGER,
    disk_usage_percent FLOAT,
    temperature_celsius FLOAT,
    voltage_volts FLOAT,
    tracking_status VARCHAR(20),
    flags JSONB,
    raw_metrics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_health_station_time ON station_health(station_id, timestamp DESC);
CREATE INDEX idx_health_timestamp ON station_health(timestamp DESC);
```

### Implementation Plan

#### Step 1: Health Extraction (Receiver-specific)
- Implement PolarX5HealthExtractor using existing SBF parser
- Extract key metrics from status_1hr files
- Write to JSON files (receiver-independent format)

#### Step 2: Database Integration
- Set up PostgreSQL schema
- Implement JSON → PostgreSQL writer
- Test with sample data
- Configure Grafana dashboards

#### Step 3: Icinga Integration
- Define health check format for Icinga2
- Implement REST API client
- Configure alert thresholds
- Test alert delivery

#### Step 4: Scheduler Integration
- Add health_processing job type to scheduler
- Implement automatic processing after status_1hr downloads
- Add station recovery detection
- Test with full network

#### Step 5: Smart Recovery
- Implement recovery plan generator
- Add backfill job scheduling
- Test recovery scenarios (1 hour, 1 day, 1 week offline)

### Success Criteria
- Health data extracted from 100% of polarx5 stations
- Data flowing to both PostgreSQL and Icinga
- Grafana dashboards showing real-time station health
- Alerts triggering correctly for offline stations
- Automatic backfill when stations recover

### Testing Requirements
- Test with all polarx5 stations
- Simulate station offline/online cycles
- Verify alert delivery (email, Icinga)
- Verify Grafana visualizations
- Test PostgreSQL performance with full network data rate

---

## Phase 3: RINEX Conversion System

**Timeline**: After Phase 2 complete
**Complexity**: High - Major new feature

### Overview
Convert raw receiver files to standard RINEX format for scientific processing.

### Data Flow
```
15s_24hr/raw/*.{sbf,T02,T00,m00}.gz
    ↓ [Receiver-specific converters]
15s_24hr/rinex/*.{24o,24n,24g}.gz (RINEX 3.x)

1Hz_1hr/raw/*.{sbf,T02,T00}.gz
    ↓ [Receiver-specific converters]
1Hz_1hr/rinex/*.{24o}.gz (RINEX 3.x)
```

### Architecture Requirements

#### 1. Converter Interface
```python
class RINEXConverter(ABC):
    """Abstract base for receiver-specific RINEX conversion."""
    @abstractmethod
    def convert_to_rinex(self, raw_file: Path, output_path: Path) -> ConversionResult:
        """Convert raw receiver file to RINEX format."""
        pass
```

**Implementations needed**:
- `PolarX5Converter` - sbf2rin tool (Septentrio)
- `NetR9Converter` - teqc or similar
- `NetRSConverter` - teqc or similar
- `G10Converter` - bin2asc + custom processing

#### 2. Tool Integration
External tools required:
- `sbf2rin` - Septentrio proprietary (already in PATH)
- `teqc` - UNAVCO tool (already in PATH)
- `bin2asc` - Leica proprietary (already in PATH)

#### 3. Scheduler Integration
New scheduled job type: `rinex_conversion`
```yaml
rinex_conversion:
  enabled: true
  schedule: "01:00"  # 1 AM daily for 15s_24hr
  sessions_to_convert: ['15s_24hr', '1Hz_1hr']
  lookback_periods: 1
  rinex_version: 3.04
  compression: .gz
  max_concurrent: 5  # CPU-intensive
```

#### 4. Quality Control
- Verify RINEX file integrity
- Check observation counts
- Validate time spans
- Generate conversion reports

### Implementation Plan

#### Step 1: Converter Framework
- Create RINEXConverter ABC
- Implement tool wrappers (sbf2rin, teqc, bin2asc)
- Add error handling and logging

#### Step 2: Receiver-Specific Converters
- Implement PolarX5Converter (priority)
- Implement NetR9Converter
- Implement NetRSConverter
- Implement G10Converter

#### Step 3: Scheduler Integration
- Add rinex_conversion job type
- Schedule daily conversions
- Test with subset of stations

#### Step 4: Quality Control
- Implement RINEX validation
- Add conversion reports
- Set up failure alerts

#### Step 5: Full Network Testing
- Test with all 173 stations
- Monitor CPU/disk usage
- Optimize conversion settings

### Success Criteria
- All receiver types converting successfully
- RINEX files validated for scientific use
- Conversion completing within time windows
- Error rates < 1%
- Full audit trail of conversions

---

## Cross-Cutting Concerns

### Retry Logic (Needed for Phases 1-3)
**Status**: Partially implemented at download level, needs scheduler-level implementation

#### Requirements
1. Scheduler-level retry tracking (separate from per-download retries)
2. Exponential backoff for persistent failures
3. Max retry limits configurable per session type
4. Retry state persisted to database (survives restarts)
5. Manual retry triggering for operator intervention

#### Implementation Plan
```python
class RetryPolicy:
    """Configurable retry behavior for scheduled jobs."""
    max_retries: int
    initial_delay_minutes: int
    backoff_multiplier: float  # 1.5 = exponential backoff
    max_delay_minutes: int

class JobRetryTracker:
    """Track retry attempts in SQLite database."""
    def record_failure(self, job_id: str, error: Exception)
    def get_retry_count(self, job_id: str) -> int
    def should_retry(self, job_id: str, policy: RetryPolicy) -> bool
    def schedule_retry(self, job_id: str, delay_minutes: int)
```

### Performance Monitoring
**Needed for all phases**

#### Metrics to Track
1. **Download Performance**
   - Average download time per station/session
   - Success/failure rates
   - Network throughput
   - Concurrent connection count

2. **Processing Performance** (Phase 2 & 3)
   - Health extraction time
   - RINEX conversion time
   - Database write latency
   - API call latency (Icinga)

3. **System Resources**
   - CPU utilization during conversions
   - Disk I/O during downloads
   - Network bandwidth usage
   - Database connection pool status

#### Implementation
- Prometheus metrics export
- Grafana system dashboard
- Alert on anomalies

### Configuration Management
**Critical for production deployment**

#### Current State
- YAML configuration files in `~/.config/gpsconfig/`
- Some hardcoded paths need cleanup

#### Production Requirements
- Environment-based configuration (dev/staging/prod)
- Secrets management (database passwords, API keys)
- Configuration validation on startup
- Hot reload for non-critical settings

---

## Testing Strategy

### Load Testing (Current Phase)
- **Goal**: Find breaking points and optimal settings
- **Method**: Gradual increase (15 → 30 → 50 → 100 → 173 stations)
- **Metrics**: Timing, failure rates, resource usage

### Integration Testing (Phase 2 & 3)
- Test full pipeline: download → process → store
- Verify data flow through all systems
- Test failure recovery scenarios

### End-to-End Testing (Before Production)
- 24-hour test run with full network
- Verify all dashboards and alerts
- Test operator procedures
- Disaster recovery simulation

---

## Deployment Plan

### DevOps Environment Setup
1. Provision production server(s)
2. Set up PostgreSQL database
3. Configure Icinga2 integration
4. Set up Grafana instance
5. Deploy scheduler as systemd service
6. Configure monitoring and alerts

### Migration Strategy
- Parallel run with existing system
- Gradual station migration
- Fallback plan to old system
- Data validation period

### Operator Training
- Document all commands and procedures
- Create troubleshooting guide
- Define escalation procedures
- Schedule training sessions

---

## Timeline Summary

| Phase | Duration Estimate | Dependencies |
|-------|------------------|--------------|
| Current: Scheduler Testing | 2-3 weeks | None |
| Phase 1: Documentation & Git | 1 week | Scheduler testing complete |
| Phase 2: Health Monitoring | 4-6 weeks | Phase 1 |
| Phase 3: RINEX Conversion | 3-4 weeks | Phase 2 |
| Integration & Testing | 2-3 weeks | Phase 3 |
| **Total** | **12-17 weeks** | - |

**Target Production Date**: ~3-4 months from now

---

## Open Questions

1. **Health Monitoring**:
   - What are critical vs warning thresholds for each metric?
   - Who receives alerts? Email list?
   - What is acceptable alert latency?

2. **RINEX Conversion**:
   - Which RINEX version (3.04 is current)?
   - What metadata needs to be included?
   - Archive retention policy for raw vs RINEX files?

3. **Performance**:
   - What is acceptable max download time per station?
   - CPU/memory limits for production server?
   - Network bandwidth limits?

4. **Operations**:
   - Who will be primary operators?
   - On-call rotation needed?
   - SLA requirements?

---

**Last Updated**: 2025-10-01
**Document Owner**: Benedikt Gunnar Ófeigsson
**Review Cycle**: Update after each phase completion
