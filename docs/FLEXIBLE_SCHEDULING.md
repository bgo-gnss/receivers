# Flexible Scheduling Implementation Plan

## Overview

Implement flexible schedule syntax for the GPS receiver scheduler to support:
- Multiple times per day
- Interval-based scheduling (every N hours/minutes)
- Full cron expression support
- Backward compatibility with current `schedule_minute` + `frequency`

## Implementation Status

### ✅ Completed
1. **schedule_parser.py** - Created comprehensive parser module (206 lines)
   - Location: `src/receivers/scheduling/schedule_parser.py`
   - Supports all planned schedule formats
   - Includes distribution window support
   - Full documentation and examples

### ⏳ Pending
2. **Update bulk_scheduler.py** to use the parser
3. **Update scheduler.yaml** with flexible syntax examples
4. **Update config_loader.py** to handle new formats
5. **Add tests** for schedule parser
6. **Update documentation** in CLAUDE.md

## Supported Schedule Formats

### 1. Simple Time Formats

```yaml
# Daily at specific time
schedule: "00:10"  # Runs at 00:10 every day

# Every hour at specific minute
schedule: ":15"  # Runs at XX:15 every hour

# Interval-based
schedule: "6h"   # Every 6 hours
schedule: "45m"  # Every 45 minutes
```

### 2. Multiple Times Per Day

```yaml
# List of HH:MM times
schedule: ["00:10", "08:10", "16:10"]  # Three times daily
```

### 3. Full Cron Expression

```yaml
# Raw cron: minute hour day month day_of_week
schedule: "cron: */15 * * * *"  # Every 15 minutes
schedule: "cron: 0 */6 * * *"   # Every 6 hours at minute 0
```

### 4. Legacy Format (Backward Compatible)

```yaml
# Current format still works
schedule_minute: 15
frequency: "hourly"
```

## Example YAML Configurations

```yaml
sessions:
  # Daily download at midnight
  15s_24hr:
    enabled: true
    schedule: "00:10"
    distribution_window: 10
    timeout_minutes: 45

  # Hourly download
  1Hz_1hr:
    enabled: true
    schedule: ":15"  # Every hour at :15
    distribution_window: 10
    timeout_minutes: 30

  # Every 6 hours
  custom_6h:
    enabled: true
    schedule: "6h"
    distribution_window: 10
    timeout_minutes: 30

  # Multiple times per day
  rush_hour:
    enabled: true
    schedule: ["06:00", "12:00", "18:00"]
    distribution_window: 5
    timeout_minutes: 20

  # Complex cron
  advanced:
    enabled: true
    schedule: "cron: */10 6-18 * * 1-5"  # Every 10 min, 6am-6pm, Mon-Fri
    distribution_window: 2
    timeout_minutes: 15
```

## Implementation Steps

### Step 1: Integrate Parser into BulkDownloadScheduler

```python
# In bulk_scheduler.py

from .schedule_parser import parse_schedule, apply_distribution_window

def _schedule_session(self, session_type: str, config: ScheduleConfig):
    """Schedule downloads for a session using flexible format."""
    stations = self._get_stations_for_session(session_type, config)

    # Parse schedule format
    schedule_value = config.schedule  # New attribute
    base_trigger = parse_schedule(schedule_value)

    for i, station_id in enumerate(stations):
        # Apply distribution window
        trigger_type, trigger_kwargs = apply_distribution_window(
            base_trigger, i, len(stations), config.distribution_window
        )

        # Create job
        job_id = f"{session_type}_{station_id}"
        self.scheduler.add_job(
            func=_download_station_data_job,
            trigger=trigger_type,
            args=[station_id, session_type, self.production_mode],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            **trigger_kwargs
        )
```

### Step 2: Update ScheduleConfig Dataclass

```python
@dataclass
class ScheduleConfig:
    """Configuration for scheduled downloads."""
    session_type: str
    schedule: Union[str, List[str], Dict[str, Any]]  # NEW: Flexible format
    distribution_window: int
    enabled: bool = True
    max_concurrent: int = 3
    timeout_minutes: int = 30

    # Legacy fields (for backward compatibility)
    schedule_minute: Optional[int] = None
    frequency: Optional[str] = None

    def __post_init__(self):
        """Convert legacy format to new format if needed."""
        if self.schedule_minute is not None and self.frequency is not None:
            self.schedule = {
                'schedule_minute': self.schedule_minute,
                'frequency': self.frequency
            }
```

### Step 3: Update config_loader.py

```python
def get_session_config(config: Dict[str, Any],
                       session_type: str,
                       station_id: Optional[str] = None) -> ScheduleConfig:
    """Get ScheduleConfig with flexible schedule support."""
    session_cfg = config['sessions'].get(session_type, {})

    # Apply station-specific overrides
    if station_id and station_id in config.get('stations', {}):
        station_cfg = config['stations'][station_id]
        if 'sessions' in station_cfg and session_type in station_cfg['sessions']:
            override = station_cfg['sessions'][session_type]
            session_cfg = {**session_cfg, **override}

    # Get schedule (flexible format or legacy)
    schedule = session_cfg.get('schedule')
    if schedule is None:
        # Legacy format
        schedule_minute = session_cfg.get('schedule_minute', 0)
        frequency = session_cfg.get('frequency', 'daily')
        schedule = {'schedule_minute': schedule_minute, 'frequency': frequency}

    return ScheduleConfig(
        session_type=session_type,
        schedule=schedule,
        distribution_window=session_cfg.get('distribution_window', 10),
        enabled=session_cfg.get('enabled', True),
        max_concurrent=session_cfg.get('max_concurrent', 3),
        timeout_minutes=session_cfg.get('timeout_minutes', 30)
    )
```

### Step 4: Add Tests

```python
# tests/test_schedule_parser.py

def test_parse_single_time():
    trigger = parse_schedule("00:10")
    assert trigger.trigger_type == 'cron'
    assert trigger.trigger_kwargs == {'hour': 0, 'minute': 10}

def test_parse_hourly_minute():
    trigger = parse_schedule(":15")
    assert trigger.trigger_type == 'cron'
    assert trigger.trigger_kwargs == {'minute': 15}

def test_parse_interval_hours():
    trigger = parse_schedule("6h")
    assert trigger.trigger_type == 'interval'
    assert trigger.trigger_kwargs == {'hours': 6}

def test_parse_time_list():
    trigger = parse_schedule(["00:10", "08:10", "16:10"])
    assert trigger.trigger_type == 'cron'
    assert trigger.trigger_kwargs == {'hour': '0,8,16', 'minute': 10}

def test_parse_cron_expression():
    trigger = parse_schedule("cron: */15 * * * *")
    assert trigger.trigger_type == 'cron'
    assert 'minute' in trigger.trigger_kwargs
```

## Benefits

1. **Flexibility**: Users can choose the most natural format for their needs
2. **Backward Compatible**: Existing configs continue to work
3. **APScheduler Native**: Uses APScheduler's full power
4. **Well Documented**: Clear examples and error messages
5. **Distribution Support**: Spreads stations across time windows

## Migration Path

1. **Phase 1** (Current): Legacy format works, parser exists
2. **Phase 2**: Update bulk_scheduler to use parser (optional for users)
3. **Phase 3**: Update docs and examples with new syntax
4. **Phase 4**: Deprecate legacy format (with warning)

## Example Use Cases

### Use Case 1: Download Every 6 Hours
```yaml
custom_6h:
  schedule: "6h"  # Simpler than: ["00:00", "06:00", "12:00", "18:00"]
```

### Use Case 2: Business Hours Only
```yaml
business_hours:
  schedule: "cron: 0 8-17 * * 1-5"  # Every hour, 8am-5pm, Mon-Fri
```

### Use Case 3: High-Frequency Monitoring
```yaml
rapid_status:
  schedule: "15m"  # Every 15 minutes
```

## Notes

- **Distribution window** works with cron triggers but not interval triggers
- **Interval triggers** start immediately when scheduler starts
- **Cron triggers** wait for the next scheduled time
- **Multiple times** must have the same minute value (limitation)

## Testing Checklist

- [ ] Parse single time ("00:10")
- [ ] Parse hourly minute (":15")
- [ ] Parse interval hours ("6h")
- [ ] Parse interval minutes ("45m")
- [ ] Parse time list (["00:10", "08:10"])
- [ ] Parse cron expression ("cron: */15 * * * *")
- [ ] Parse legacy format ({schedule_minute: 10, frequency: "daily"})
- [ ] Distribution window applies correctly
- [ ] Error handling for invalid formats
- [ ] Backward compatibility with existing configs
