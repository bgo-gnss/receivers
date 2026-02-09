# Code Review Tracker - Receivers Package

Tracking document for code weaknesses, recurring issues, and optimization opportunities discovered during dashboard and health monitoring development.

## Issue Categories

- **DATA-MODEL**: Database schema, views, value vocabulary
- **PROTOCOL**: FTP vs HTTP receiver protocol handling
- **DASHBOARD**: Grafana SQL, value mappings, display logic
- **EXTRACTOR**: Health data extraction from receivers
- **DB-WRITER**: Database persistence layer
- **CONFIG**: Configuration loading and syncing
- **SCHEDULER**: Job scheduling and execution

---

## Resolved Issues

### PROTOCOL-001: Port status view ignores `'ok'` values
- **Category**: DATA-MODEL / PROTOCOL
- **Severity**: High
- **Found**: 2026-02-08
- **Resolved**: 2026-02-08
- **Files**: `migrations/010_port_status.sql`, `station_port_status` view
- **Root cause**: `station_port_status` view only treated `'open'` as good status. Trimble connectivity_writer writes `'ok'`/`'critical'` instead of `'open'`/`'refused'` because it takes a different code path (metrics.ports vs connection.protocol).
- **Fix**: Updated view CASE to accept both `'open'` and `'ok'` as active states.
- **Deeper issue**: Two code paths in `connectivity_writer.py` produce different value vocabularies for the same column. Should standardize to one set of values.

### PROTOCOL-002: Overview Port column NULL for Trimble receivers
- **Category**: DASHBOARD / PROTOCOL
- **Severity**: High
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: `docs/grafana/gps_health_dashboard.json` (main table SQL)
- **Root cause**: Port SQL checked `ftp_open IS NULL THEN NULL` first. Trimble receivers have `ftp_open=NULL` (no FTP port) so they always returned NULL/grey.
- **Fix**: Changed to `ftp_open IS NULL AND http_open IS NULL THEN NULL` and `COALESCE(ftp_open, true)` to treat unchecked ports as OK.
- **Deeper issue**: All port-related dashboard SQL was written for the PolaRX5 model (FTP=download, HTTP=health, Control=management). Trimble uses HTTP for everything.

### PROTOCOL-003: Logging panel used file_tracking instead of receiver API
- **Category**: DASHBOARD / EXTRACTOR
- **Severity**: High
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: `trimble_http_extractor.py`, `db_writer.py`, `migrations/014_logging_status.sql`, detail dashboard JSON
- **Root cause**: Logging panel queried `file_tracking` table (download records) to determine if sessions were active. This only shows whether *we've downloaded* files, not whether the *receiver is logging*.
- **Fix**: Added `_parse_logging_from_activity_html()` to Trimble extractor, `block_logging_status` table, and updated dashboard SQL to use `station_logging_status` view.
- **Deeper issue**: PolaRX5 extractor also needs to report logging sessions from SBF data. G10 already does via `_parse_logging_sessions()`.

### PROTOCOL-004: NetR5 receiver_type hardcoded as NetR9
- **Category**: EXTRACTOR / PROTOCOL
- **Severity**: Medium
- **Found**: 2026-02-08
- **Resolved**: 2026-02-09
- **Files**: `trimble/netr9.py` line 213
- **Root cause**: `get_health_status()` created TrimbleHTTPExtractor with `receiver_type="NetR9"` hardcoded. NetR5 subclass calls `super()` so it also used "NetR9", causing the extractor to try unsupported `/prog/show?` endpoints.
- **Fix**: Changed to `self.get_receiver_type()` which returns the actual class name.

### PROTOCOL-005: Status session shown as "Unknown" for non-PolaRX5
- **Category**: DASHBOARD
- **Severity**: Low
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: Detail dashboard JSON (Logging panel SQL)
- **Root cause**: `status_1hr` session only exists on PolaRX5, but the SQL showed "Unknown" for all receivers instead of "N/A" for unsupported types.
- **Fix**: Added `WHEN d.receiver_type != 'PolaRX5' THEN -2` (N/A) for the Status column.

### DB-001: VARCHAR overflow poisons PostgreSQL transaction
- **Category**: DB-WRITER
- **Severity**: Critical
- **Found**: 2026-02-08
- **Resolved**: 2026-02-08
- **Files**: `health/db_writer.py` `_update_station_identity()`
- **Root cause**: Firmware version string exceeded VARCHAR(30) limit. Failed SQL aborted the transaction; ALL subsequent writes for that station failed silently with "current transaction is aborted".
- **Fix**: Added value truncation (`firmware[:30]`, `model[:60]`, `serial[:30]`) and SAVEPOINT/ROLLBACK TO SAVEPOINT to isolate identity updates.
- **Deeper issue**: Other DB writer methods may have similar vulnerability. Need audit of all INSERT/UPDATE statements for VARCHAR overflow potential and transaction isolation.

### DB-002: Firmware error strings stored as firmware version
- **Category**: EXTRACTOR
- **Severity**: Low
- **Found**: 2026-02-08
- **Resolved**: 2026-02-09
- **Files**: `trimble_http_extractor.py` line 1050
- **Root cause**: When `/prog/show?FirmwareVersion` returned "ERROR: Unknown Command", the fallback path stored the error string as firmware version (it was under 60 chars).
- **Fix**: Added `not stripped.startswith("ERROR:")` check.

### CONFIG-001: Station metadata not synced from stations.cfg
- **Category**: CONFIG / DB-WRITER
- **Severity**: Medium
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: `health/db_writer.py` `_ensure_station()`
- **Root cause**: `_ensure_station()` only wrote `receiver_type` and `power_type`. Antenna, RINEX metadata, IP, HTTP port were never synced.
- **Fix**: Now reads full config from `stations.cfg` and syncs antenna_type, marker_name, marker_number, observer, agency, ip_address, http_port on every health check.
- **Note**: Also added hostname-to-IP resolution for stations with DNS names instead of IP addresses.

### CONFIG-002: Hostname in inet column causes silent failure
- **Category**: CONFIG / DB-WRITER
- **Severity**: Medium
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: `health/db_writer.py` `_ensure_station()`
- **Root cause**: Some stations have hostnames (e.g., `insk.gps.vedur.is`) instead of IPs in `router_ip`. PostgreSQL `inet` type rejects hostnames.
- **Fix**: Added `socket.gethostbyname()` resolution before DB insert.

### DASHBOARD-001: CRITICAL status showing green in overview
- **Category**: DASHBOARD
- **Severity**: High
- **Found**: 2026-02-08
- **Resolved**: 2026-02-08
- **Files**: `gps_health_dashboard.json` (Status column)
- **Root cause**: Two issues: (1) `cellOptions.mode: "gradient"` fell back to default green threshold for text values. (2) Regex `^CRITICAL:.*` failed on multi-line `status_details` because `.*` doesn't match newlines.
- **Fix**: Changed to `mode: "basic"` and regex `^CRITICAL:[\\s\\S]*`.

### DASHBOARD-002: Status details empty for ping-only warnings
- **Category**: DB-WRITER
- **Severity**: Medium
- **Found**: 2026-02-09
- **Resolved**: 2026-02-09
- **Files**: `health/db_writer.py` `_build_status_details()`
- **Root cause**: `_build_status_details()` only checked metric statuses and port states, not connection-level data. When ping latency caused warning/critical, `status_details` was NULL, showing generic "Check file/port status".
- **Fix**: Added connection data inspection — now reports "Ping high latency (520ms)" or "Ping packet loss (50%)".

---

## Open Issues / Future Work

### PROTOCOL-010: Standardize port status value vocabulary
- **Category**: DATA-MODEL
- **Severity**: Medium
- **Status**: Open
- **Description**: `connectivity_writer.py` has two code paths that produce different values:
  - Path 1 (metrics.ports): writes `'ok'`, `'critical'`, `'warning'`
  - Path 2 (connection.protocol): writes `'open'`, `'refused'`, `'timeout'`, `'error'`
  - Both write to the same `block_port_status` columns
- **Proposal**: Standardize on one vocabulary. Recommend `'open'`/`'refused'`/`'timeout'`/`'error'` since those describe actual port states.

### PROTOCOL-011: PolaRX5 logging session extraction
- **Category**: EXTRACTOR
- **Severity**: Medium
- **Status**: Open
- **Description**: PolaRX5 extractor doesn't report logging session status to `block_logging_status`. Currently only Trimble (via activity CGI) and G10 (via XML) report this. PolaRX5 should extract from SBF ExeScript or status_1hr data.
- **Files**: `septentrio/polarx5.py`, `health/sbf_extractor.py`

### DB-010: Audit all DB writers for VARCHAR overflow
- **Category**: DB-WRITER
- **Severity**: Medium
- **Status**: Open
- **Description**: `_update_station_identity()` had VARCHAR overflow. Other writers may have similar issues. Need to audit all INSERT/UPDATE statements and add truncation or check column limits.
- **Files**: All `_write_*` methods in `db_writer.py`, `connectivity_writer.py`

### DB-011: Transaction isolation patterns
- **Category**: DB-WRITER
- **Severity**: Medium
- **Status**: Open
- **Description**: Only `_update_station_identity()` uses SAVEPOINT. Other non-critical writes (like satellite tracking, NTRIP status) could also fail and poison the transaction. Consider wrapping all individual block writes in SAVEPOINTs or using separate transactions.
- **Files**: `health/db_writer.py` `write_health_data()`

### DB-012: Pyright type safety for Optional[Connection]
- **Category**: DB-WRITER
- **Severity**: Low
- **Status**: Open
- **Description**: All `self._conn.cursor()` calls flag `reportOptionalMemberAccess` because `_conn` is typed as `Optional[Connection]`. Should add proper null checks or assert-after-connect pattern.
- **Files**: `health/db_writer.py`

### DASHBOARD-010: Protocol-aware detail dashboard
- **Category**: DASHBOARD
- **Severity**: Medium
- **Status**: Open
- **Description**: Detail dashboard Ports panel shows FTP/HTTP/Ctrl for all receivers. Trimble receivers don't have FTP or Control ports — should show only HTTP or adapt labels based on receiver type.
- **Files**: `gps_station_detail_dashboard.json`

### CONFIG-010: Config change detection / live reload
- **Category**: CONFIG / SCHEDULER
- **Severity**: Low
- **Status**: Open
- **Description**: Currently config is loaded fresh each health check (via `_ensure_station()`), which picks up `stations.cfg` changes within ~5 minutes. However, adding/removing stations requires scheduler restart. Consider file watching or periodic config diff.
- **Files**: `scheduling/bulk_scheduler.py`, `config_utils.py`

### EXTRACTOR-010: NetR5 endpoint support matrix
- **Category**: EXTRACTOR
- **Severity**: Low
- **Status**: Open
- **Description**: NetR5 supports a subset of NetR9/NetRS endpoints. Currently gated by `has_prog_show = receiver_type != "NetR5"` but some individual endpoints may work. Need to build a proper capability matrix per firmware version.
- **Files**: `health/trimble_http_extractor.py`

### SCHEDULER-010: No duplicate instance protection
- **Category**: SCHEDULER
- **Severity**: High
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: Two scheduler processes ran simultaneously, causing duplicate health checks and corrupted SBF data (bogus 1.12V readings). Fixed with `fcntl.flock()` exclusive lock in `start()` — second instance fails immediately with PID of running instance.
- **Files**: `scheduling/bulk_scheduler.py`

### SCHEDULER-011: Thread pool starvation from offline stations
- **Category**: SCHEDULER / EXTRACTOR
- **Severity**: High
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: 15 APScheduler workers blocked by slow offline station health checks (30-133s per station). Root cause: ping failure did not gate port checks or data extraction. Offline stations consumed threads doing futile port probes (3×2s timeout each), HTTP extraction (10s timeout), and NTRIP checks.
- **Fix**: Added ping gate to all 4 receiver types — skip port checks and extraction when ping fails. `check_connection_health()` already had `fail_fast=True` for port checks; now extraction is also gated.
- **Files**: `septentrio/polarx5.py`, `trimble/netr9.py`, `trimble/netrs.py`, `leica/g10.py`

### EXTRACTOR-012: NetR9 logging sessions not extracted (merge.xml)
- **Category**: EXTRACTOR
- **Severity**: Medium
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: NetR9 receivers showed "Logging: Unknown" because the activity CGI (`/perl-scripts/rstatusActivity.cgi`) is NetRS-only (returns 404 on NetR9). Logging session data was available in merge.xml `dataLogger` block but not parsed.
- **Fix**: Added `_parse_logging_from_merge_xml()` to parse `<session>` elements (enabled=1, status=2 = actively logging). Now falls back: activity CGI → merge.xml → skip.
- **Files**: `health/trimble_http_extractor.py`

### CONFIG-011: Passive stations lack health_check marker
- **Category**: CONFIG
- **Severity**: Low
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: 8 stations (GRVM, GRVV, KRAC, MYVA, RVIT, SYRF, THRC, TORK) have no `receiver_type` or `router_ip` — they are either inactive or externally managed. They appear in the dashboard as "unknown" with no health data. Added `health_check = passive` marker to `stations.cfg` for future filtering.
- **Files**: `gps-config-data/stations.cfg`

### EXTRACTOR-011: G10 logging session data not written to DB
- **Category**: EXTRACTOR / DB-WRITER
- **Severity**: Medium
- **Status**: Open
- **Description**: G10 extractor already parses logging sessions (`_parse_logging_sessions()`) and puts them in `metrics["logging_sessions"]`. The db_writer now has `_write_logging_status()` but the G10 format uses `active_sessions` count without individual session booleans. Need to map G10 session names to canonical names.
- **Files**: `health/g10_http_extractor.py`, `health/db_writer.py`

### DASHBOARD-011: "Checked" column shows data age, not last check time
- **Category**: DASHBOARD
- **Severity**: Medium
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: The overview table "Checked" column used `seconds_since_update` (time since last successful data extraction) not `last_check` (time since last health check attempt). For offline stations with all ports down, this showed hours/days even though health checks ran every 5 minutes.
- **Fix**: Changed to `EXTRACT(epoch FROM now() - d.last_check)::integer`. Kept existing thresholds (green <2h, yellow 2-24h, red >24h) for flexibility if check frequency is reduced later. Data staleness is already shown by the Conn column ("offline 1d").
- **Files**: `gps_health_dashboard.json` (table SQL + column overrides)

### CONFIG-012: Passive stations not filtered from scheduler/dashboard
- **Category**: CONFIG / SCHEDULER / DASHBOARD
- **Severity**: Low
- **Status**: Open
- **Description**: Stations marked `health_check = passive` still appear in dashboard as "unknown" and trigger scheduler config errors. Should be filtered out of health monitoring or shown with "Passive" status. The `health_check` field was added 2026-02-09 but not yet consumed by any code.
- **Files**: `gps-config-data/stations.cfg`, `scheduling/bulk_scheduler.py`, `gps_health_dashboard.json`

---

## Review Checklist (for systematic audit)

When reviewing a file, check for these patterns:

### Data Model
- [ ] Does it assume FTP as download protocol?
- [ ] Does it handle NULL port values correctly?
- [ ] Are status values consistent with the standard vocabulary?
- [ ] Does it work for all 5 receiver types (PolaRX5, NetR9, NetRS, NetR5, G10)?

### Database
- [ ] Are VARCHAR lengths respected (truncation before insert)?
- [ ] Is the transaction protected with SAVEPOINT for non-critical operations?
- [ ] Are Optional[Connection] accesses guarded?
- [ ] Does ON CONFLICT handle all expected scenarios?

### Dashboard
- [ ] Do value mappings cover all possible values (including NULL)?
- [ ] Does the SQL work for all receiver types?
- [ ] Are regex patterns tested against multi-line values?
- [ ] Is `cellOptions.mode` set to "basic" for text columns?

### Configuration
- [ ] Are hostname-vs-IP differences handled?
- [ ] Are config field types validated before use?
- [ ] Are missing/optional fields handled with proper defaults?

---

**Created**: 2026-02-09
**Last updated**: 2026-02-09
**Purpose**: Track code quality issues for systematic review and optimization
