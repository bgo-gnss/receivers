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

### EXTRACTOR-013: PolaRX5 port check timeout too short for 3G/4G
- **Category**: EXTRACTOR / PROTOCOL
- **Severity**: High
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10 (updated 2026-02-11)
- **Files**: `health/polarx5_tcp_extractor.py`
- **Root cause**: TCP port check used 2s timeout. On 3G/4G links, TCP handshake can take 3-5s. Single attempt meant ~30% false-negative rate on slow links.
- **Fix**: Increased timeout to 5s. Added retry-on-timeout via `_check_single_port()` helper. Reduces false negatives from ~30% to ~2% on slow links.
- **Update (2026-02-11)**: CONN-003 extended retry to also cover "refused" — NAT routers on lossy links can send spurious RST packets. `refused` is no longer treated as definitive.

### EXTRACTOR-014: PolaRX5 single-packet ping unreliable
- **Category**: EXTRACTOR / PROTOCOL
- **Severity**: Medium
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10
- **Files**: `septentrio/polarx5.py`
- **Root cause**: PolaRX5 health check used `count=1` for ping (separate from connection_checker's `count=5`). One dropped ICMP packet = station marked offline + extraction skipped.
- **Fix**: Changed to `count=3`. Also added self-correction: when TCP data extraction succeeds, control port status is corrected from timeout→open (proves the port works even if the TCP check was slow).

### DATA-FLOW-001: No RINEX file tracking in file_tracking table
- **Category**: DATA-MODEL / SCHEDULER
- **Severity**: High
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10
- **Files**: `health/file_tracker.py`, `scheduling/gap_scheduler.py`, `migrations/018_data_flow_status.sql`
- **Root cause**: `file_tracking` table only tracked raw SBF files (`15s_24hr`, `1Hz_1hr`, `status_1hr`). RINEX conversion output was invisible to the monitoring system. Operators had no visibility into RINEX freshness.
- **Fix**: Multi-part fix:
  1. `get_archive_directory()`: Support `_rinex` suffix session types → `rinex/` subdir
  2. `_generate_expected_files()`: Treat `15s_24hr_rinex` as daily
  3. `scan_rinex_files()`: New method on GapDetector, parses RINEX 2 short names, upserts to file_tracking with file_size
  4. `_run_gap_detection_job()`: Calls RINEX scanner for PolaRX5 stations
  5. `station_data_flow_status` view: Computes combined raw+RINEX+logging status codes per station

### DATA-FLOW-002: Archive scanner counts zero-byte files as present
- **Category**: DATA-MODEL
- **Severity**: Medium
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10
- **Files**: `health/file_tracker.py`
- **Root cause**: `check_file_status()` glob loop counted any file matching the pattern as "found", including 0-byte corrupt files. A truncated RINEX conversion would show as "green" in the dashboard.
- **Fix**: Added `MIN_ARCHIVE_FILE_SIZE = 50` bytes threshold. Files below this size are skipped in the glob loop. Protects both raw SBF and RINEX file counting.

### DASHBOARD-012: No data flow columns in overview table
- **Category**: DASHBOARD
- **Severity**: Medium
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10
- **Files**: `gps_health_dashboard.json`, `gps_map_dashboard.json`
- **Root cause**: Overview table had no visibility into file freshness or RINEX conversion status. Operators couldn't see which stations were missing downloads or conversions.
- **Fix**: Added "24h" and "1Hz Data" columns to overview table with color-coded status (green/yellow/red/grey). Added "Missing 24h RNX" count box to both health and map dashboards. Added `24hr_rinex_missing` and `1hr_rinex_missing` filter options.

### DASHBOARD-013: Station detail dashboard missing RINEX panel
- **Category**: DASHBOARD
- **Severity**: Medium
- **Found**: 2026-02-10
- **Resolved**: 2026-02-10
- **Files**: `gps_station_detail_dashboard.json`
- **Root cause**: Station detail showed File Status (raw file age) and Logging (active sessions) but no RINEX conversion status.
- **Fix**: Added "RINEX Status" panel (id:51) between File Status and Logging panels with 15s/1Hz columns and color-coded value mappings.

### DASHBOARD-015: File Status panel shows N/A for archived files
- **Category**: DASHBOARD
- **Severity**: High
- **Found**: 2026-02-12
- **Resolved**: 2026-02-12
- **Files**: `gps_station_detail_dashboard.json`
- **Root cause**: File Status panel SQL filtered `status='downloaded'` only. Files transition `downloaded → archived` after archiving, so once archived they disappeared from the panel — showing N/A even though tracking data existed. Meanwhile RINEX Status (using `station_data_flow_status` view) correctly queried `status IN ('downloaded','archived')`, creating a contradictory display: File Status N/A but RINEX OK.
- **Fix**: Changed File Status panel SQL to `status IN ('downloaded','archived')` for all three session types.
- **User report**: AUST station showed File Status N/A for all sessions while RINEX 15s showed OK — logically impossible since RINEX is derived from raw files.

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
- **Status**: Resolved
- **Resolution date**: 2026-02-10
- **Description**: PolaRX5 extractor didn't report logging session status to `block_logging_status`. Only Trimble (via activity CGI) and G10 (via XML) reported this.
- **Fix**: Added `getLogSession` TCP command to PolaRX5 TCP extractor. Parses response to detect active logging sessions (15s_24hr, 1Hz_1hr, status_1hr). Data written to `block_logging_status` via db_writer.
- **Files**: `health/polarx5_tcp_extractor.py`, `septentrio/polarx5.py`

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
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: Currently config is loaded fresh each health check (via `_ensure_station()`), which picks up `stations.cfg` changes within ~5 minutes. However, adding/removing stations requires scheduler restart. Consider file watching or periodic config diff.
- **Fix**: Added mtime-based config watcher (`_check_config_changes()`) as a 5-minute periodic scheduler job. Detects stations.cfg changes, reloads configs, syncs health_check values to DB, and logs meaningful changes (new/removed stations, health_check transitions).
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

### CONFIG-012: Passive/discontinued stations not filtered from scheduler/dashboard
- **Category**: CONFIG / SCHEDULER / DASHBOARD
- **Severity**: Low
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: Stations marked `health_check = passive` or `health_check = discontinued` appeared in dashboard as "unknown" and triggered scheduler config errors. Passive stations lacked required fields; discontinued stations (e.g., ASVE) had full config but should not be checked.
- **Fix**: Added `health_check` column to stations table (migration 015). Scheduler skips these stations from health monitoring and downloads. Dashboard hides them by default; new "Discontinued"/"Passive" filter options show them with dark-gray styling. Summary count panels also exclude them.
- **Files**: `migrations/015_health_check_status.sql`, `scheduling/bulk_scheduler.py`, `config_utils.py`, `gps_health_dashboard.json`

### PROTOCOL-012: HTTP port checks use full GET request causing false CRITICAL
- **Category**: PROTOCOL / EXTRACTOR
- **Severity**: High
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: `check_http_port()` and `check_http()` in connection_checker.py used `requests.get()` for port checks. Embedded receiver web servers (PolaRX5, Trimble) occasionally took >5s to serve HTTP responses even though the TCP port was open. This caused false CRITICAL status (e.g., SEY6 HTTP "down", ALFD "CRITICAL: HTTP down") even when all metrics were normal.
- **Fix**: Both methods now use raw TCP socket connect (like `check_ftp()` already did). Socket connect proves port is open in ~70ms. Actual HTTP protocol validation happens during data extraction in the extractors.
- **Files**: `health/connection_checker.py`

### SCHEDULER-012: Single-packet ping causes false offline on lossy links
- **Category**: SCHEDULER / PROTOCOL
- **Severity**: High
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: `check_all_levels()` called `check_ping(count=1, timeout=2)`. Stations with intermittent packet loss (GFUM 33%, FTEY 900ms latency, DYNC 33%, ELDC, HAHV) had high false-offline rate. Combined with the ping gate (SCHEDULER-011), one dropped packet skipped ALL extraction.
- **Fix**: Increased to `count=5` with automatic retry on failure. First try sends 5 ICMP packets; if all fail, retries with 5 more. False-offline rate dropped from 33% (count=1) to ~0.002% (count=5 + retry). Cost for truly offline stations: ~12s (vs ~2s before), but still saves 20s+ by skipping futile extraction.
- **Files**: `health/connection_checker.py`

### CONN-001: Redundant HTTP socket check causes false CRITICAL on lossy links
- **Category**: PROTOCOL / EXTRACTOR
- **Severity**: High
- **Found**: 2026-02-11
- **Resolved**: 2026-02-11
- **Files**: `health/connection_checker.py`
- **Root cause**: `check_all_levels()` performs 3 connection checks: `router_ping`, `http_port`, `protocol`. For HTTP-only receivers (NetR9, NetRS, G10), both `http_port` and `protocol` do identical TCP socket connects to the same port. On lossy 3G/4G links, the second connect can time out while the first succeeded — causing false CRITICAL (e.g., MOFC showing CRITICAL with 87% disk).
- **Fix**: When `protocol_type == "http"` and `protocol_port == http_port`, reuse the `http_port` result for `protocol` instead of making a redundant socket connect.
- **Deeper issue**: The 3-level model (ping/http/protocol) was designed for PolaRX5 which has distinct ports. For single-port receivers it creates unnecessary failure points.

### CONN-002: Status details missing http_port/protocol failure info
- **Category**: DB-WRITER
- **Severity**: Medium
- **Found**: 2026-02-11
- **Resolved**: 2026-02-11
- **Files**: `health/db_writer.py`
- **Root cause**: `_build_status_details()` only inspected `router_ping` from connection data. When `http_port` or `protocol` timed out (causing CRITICAL overall), the status_details string didn't mention it — showing only "Disk" while the real cause was a connection timeout. Operators saw "CRITICAL: Disk" for what was actually a connection problem.
- **Fix**: Added inspection of `http_port` and `protocol` connection levels. Now reports "HTTP port timeout", "Protocol refused", etc. alongside metric-based problems.

### CONN-003: False control port "refused" on lossy links (no self-correction)
- **Category**: EXTRACTOR / PROTOCOL
- **Severity**: High
- **Found**: 2026-02-11
- **Resolved**: 2026-02-11
- **Files**: `septentrio/polarx5.py`, `health/polarx5_tcp_extractor.py`
- **Root cause**: Two compounding issues:
  1. TCP extraction was gated behind `if control_ok:` — when the socket check returned "refused" (spurious RST from NAT on lossy link), extraction was skipped entirely and self-correction never fired.
  2. Port check retry only retried on "timeout", not "refused". On lossy links, NAT routers can send RST packets during packet loss, producing spurious "refused" results.
- **Fix**: (1) Always attempt TCP extraction regardless of port check result. Self-correction updates port status from refused/timeout → open when extraction succeeds. (2) Retry port check on "refused" too, not just timeout.
- **User report**: GEVK (PolaRX5) showed control port 28784 "refused" when it was actually open and responding.

### DISK-001: Inconsistent disk usage thresholds across extractors
- **Category**: EXTRACTOR / CONFIG
- **Severity**: Medium
- **Found**: 2026-02-11
- **Resolved**: 2026-02-11
- **Files**: `health/metrics.py`, `health/trimble_http_extractor.py`, `health/g10_http_extractor.py`, `health/rxtools_extractor.py`, `config/icinga_config.py`, `monitoring/icinga_client.py`, `monitoring/check_gps_receiver.py`
- **Root cause**: Four different disk threshold sets existed across 7 files:
  - `metrics.py`: 80/90
  - `trimble_http_extractor.py`: 85/95
  - `g10_http_extractor.py`: 85/95
  - `rxtools_extractor.py`: 80/90
  - `icinga_config.py` / `icinga_client.py` / `check_gps_receiver.py`: 80/90
- **Fix**: Unified all to `<90%` = green, `90-97%` = warning, `>97%` = critical. These thresholds are appropriate for GPS receivers where disk usage below 90% is normal, and critical alert is reserved for near-full disks.
- **Deeper issue**: Thresholds are hardcoded in each file. Should be centralized in one config/constants location.

### DASHBOARD-014: Added operational status filters (ping, ftp, disk, control)
- **Category**: DASHBOARD
- **Severity**: Medium
- **Found**: 2026-02-11
- **Resolved**: 2026-02-11
- **Files**: `gps_health_dashboard.json`, `gps_map_dashboard.json`
- **Description**: Added 3 new status filter options and renamed 1:
  - **"Ping Failed"** (`ping_failed`): matches `status_details LIKE '%Ping%'`
  - **"FTP Down"** (`ftp_down`): matches `ftp_open = false AND is_online = true`
  - **"Disk"** (`disk_warning`): matches `status_details LIKE '%Disk%'`
  - Renamed **"Ctrl Refused"** → **"Control Down"** (value unchanged: `ctrl_refused`)
- Updated WHERE clauses in map panel, table panel, and count box queries in both dashboards.

### CONFIG-013: Station lifecycle status (station_status + health_check) and filtering
- **Category**: CONFIG / SCHEDULER / DASHBOARD
- **Severity**: Medium
- **Status**: Resolved
- **Resolution date**: 2026-02-09
- **Description**: Stations without receivers, decommissioned stations, and passive stations needed lifecycle and monitoring mode fields. Initially conflated into a single field; separated into two orthogonal fields:
  - `station_status`: lifecycle state (discontinued, inactive, or NULL=active)
  - `health_check`: monitoring mode (passive, or NULL=active)
- **Fix**: Two fields in stations.cfg and DB. Auto-detect `inactive` from missing receiver_type. Scheduler filters on both (`station_status` in discontinued/inactive → skip entirely; `health_check` = passive → skip health monitoring). Config file watcher (5-min mtime check) syncs both fields. Dashboard shows clickable count boxes (Inactive, Discontinued, Passive) with dark-gray "N/A" styling for non-applicable columns.
- **Files**: `config_utils.py`, `scheduling/bulk_scheduler.py`, `migrations/015_health_check_status.sql`, `gps_health_dashboard.json`, `gps_map_dashboard.json`, `stations.cfg`

### CONFIG-014: Disk thresholds hardcoded in 7 files
- **Category**: CONFIG
- **Severity**: Low
- **Status**: Open
- **Description**: Even after unifying disk thresholds to 90/97 (DISK-001), the values are still hardcoded in 7 separate files. A single change requires editing `metrics.py`, 3 extractors, `icinga_config.py`, `icinga_client.py`, and `check_gps_receiver.py`.
- **Proposal**: Define `DISK_WARNING_THRESHOLD` and `DISK_CRITICAL_THRESHOLD` in one central location (e.g., `health/metrics.py` or a shared constants module) and import from all other files.
- **Files**: Same 7 files as DISK-001

### CONN-004: 3-level connection model overhead for single-port receivers
- **Category**: PROTOCOL
- **Severity**: Low
- **Status**: Open
- **Description**: The connection checker runs 3 levels: `router_ping`, `http_port`, `protocol`. For HTTP-only receivers (NetR9, NetRS, G10), `http_port` and `protocol` are identical. CONN-001 fixed the false CRITICAL by reusing the result, but the model still conceptually has 3 levels. Consider simplifying to 2 levels (ping + primary port) for non-PolaRX5 receivers, or making the number of levels dynamic based on receiver capabilities.
- **Files**: `health/connection_checker.py`

### SCHEDULER-013: Outage recovery should prioritize 24hr download + RINEX
- **Category**: SCHEDULER
- **Severity**: High
- **Status**: Open
- **Found**: 2026-02-12
- **Description**: When the scheduler restarts after an outage (laptop powered off overnight, server reboot), 15s_24hr daily data should be prioritized over 1Hz_1hr and status_1hr. Currently `_schedule_daily_catchup()` schedules one-shot downloads with `lookback_periods=1` (yesterday only), but after a multi-day outage the gap is larger. The backfill system eventually catches up, but it processes stations alphabetically and interleaves with other work — taking many hours. Meanwhile, 130+ stations show "Missing" in the dashboard.
- **Current behavior**:
  1. Daily catch-up fires with `lookback_periods=1` — only catches yesterday
  2. Gap detection runs at startup (60s delay) and identifies all missing files
  3. Backfill processes stations alphabetically in the :25-:55 window — very slow for 130+ stations
  4. 1Hz_1hr and status_1hr downloads compete for workers during catch-up
- **Proposal**: Outage-aware recovery mode:
  1. On startup, detect gap size (query `file_tracking` for latest date per session, compare to today)
  2. If gap > 1 day for 15s_24hr: increase lookback_periods to cover the gap
  3. Prioritize 15s_24hr catch-up over 1Hz_1hr/status_1hr (defer hourly sessions until daily is done)
  4. Run RINEX conversion immediately after each 24hr download (not waiting for reconciler)
  5. Consider dedicated "recovery executor" that doesn't compete with regular downloads
- **Impact**: After overnight outage, 130+ stations showed "Missing 24h" for hours. Daily data is the highest-value product and most time-sensitive (receivers may overwrite old files).
- **Files**: `scheduling/bulk_scheduler.py` (`_schedule_daily_catchup()`, `_schedule_gap_detection()`)

### DB-013: Ping packet loss severity threshold too aggressive
- **Category**: DB-WRITER
- **Severity**: Medium
- **Status**: Open
- **Description**: Ping packet loss of 20% currently triggers CRITICAL overall_status. On 3G/4G links, 20% packet loss is normal and should be WARNING at most. The status_details string shows "CRITICAL: Ping packet loss (20%) Disk" which misrepresents the station's actual health.
- **Proposal**: Adjust thresholds:
  - 0-10%: OK (normal for radio links)
  - 10-60%: WARNING
  - \>60%: CRITICAL
- **Files**: `health/db_writer.py` (`_build_status_details()` and/or `_calculate_overall_status()`), possibly `health/connection_checker.py` if thresholds are defined there

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
**Last updated**: 2026-02-12
**Purpose**: Track code quality issues for systematic review and optimization
