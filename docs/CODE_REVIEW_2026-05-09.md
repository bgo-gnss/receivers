# Code Review 2026-05-09 — Findings Tracker

5-pass thorough review run on `main` at `07a40ba`, covering **211 commits / 258 files / +36306/-15521** since the prior 5-pass review on 2026-03-02 (last commit of that round: `ce5de04`).

Five reviewer agents ran in parallel on:
1. **Data layer** — migrations 027+, db_writer, connectivity_writer, file_tracker, db/migrator, db/seeder
2. **Scheduler + cfg reconciliation** — bulk_scheduler, morning_recovery, cli/scheduler, cli/cfg, cfg/* package
3. **Download reliability + RINEX** — cli/parallel, polarx5, trimble, leica, rinex/async_converter, utils/file_archiver, utils/stall_timeout
4. **Extractors + monitoring** — health/{rxtools,polarx5_tcp,timeseries,trimble_http,connection_checker}_extractor, monitoring/icinga_client
5. **Dashboard / Grafana / view migrations** — docs/grafana/*.json, scripts/grafana_sync.py, view-related migrations

Severity buckets condensed below; each finding links back to the originating pass.

---

## Critical (10 findings — fix before any new feature work)

| # | Pass | Location | Issue | Fix direction |
|---|------|----------|-------|--------------|
| C1 | 1 | `migrations/033_satellite_status_coalesce.sql` | Missing `BEGIN`/`COMMIT` wrapper — only forward migration without one | Wrap in transaction |
| C2 | 1 | `migrations/046_is_file_missing_ttl_24h.sql` (+ 027, 028) | Missing `INSERT INTO schema_migrations` so manual `psql -f` re-runs them | Add `INSERT … ON CONFLICT DO NOTHING` |
| C3 | 2 | `bulk_scheduler.py:757-787` | Outer `except Exception` swallows hard failures; station never appears in batch summary as ok/fail/expected | Add `_record_batch_result(..., "fail", ...)` in the catch |
| C4 | 2 | `bulk_scheduler.py:778` | `if "audit_logger" in locals() and audit_logger:` — brittle if exception fires before `audit_logger` is bound | Init `audit_logger = None` before `try` |
| C5 | 2 | `morning_recovery.py:179` | Hardcoded `run_rinex = session == "15s_24hr"` when `_scheduler_instance` is None — silent policy divergence | Read from config or fail-loud |
| C6 | 3 | `leica/leica_ftp_download_client.py:212-328` | Same FTP zombie-socket bug PolaRX5 already fixed — `ftp.close()` not called in except handlers | Port the `ftp = None` / `if ftp: ftp.close()` pattern from `polarx5.py:1120-1144` |
| C7 | 3 | `cli/parallel.py:403-419` | Wall-clock timeout abandons daemon thread holding FTP socket + file handle + DB semaphore slot; no eviction, accumulates | Add abandoned-thread tracking + alert; long-running scheduler depletes the 20-slot DB pool over days |
| C8 | 4 | `icinga_client.py:1196` | Reads `data_quality.get("disk")` but PolaRX5 TCP writes to `metrics["disk"]` — every PolaRX5 station sends `disk_status=UNKNOWN` to Icinga, all the time | Read from the right key; add unit test |
| C9 | 4 | `polarx5_tcp_extractor.py:1042-1045` | `_parse_disk_header_only` fallback (bin2asc unavailable) silently downgrades `error` disk status to `mounted`/`unmounted` — broken disk like GJAC unflaggable | Map all bits including the error state |
| C10 | 5 | `gps_health_dashboard.json` panel 12 ("Critical & Offline") | `last_voltage` CTE on `block_power_status` has no time bound — full sequential scan every 10 s | Add `WHERE ts > NOW() - INTERVAL '7 days'` |

## High (16 findings)

| # | Pass | Location | Issue |
|---|------|----------|-------|
| H1 | 1 | `db_writer.py:234-296` | `model_mismatch` UPDATE has no savepoint — silently rolled back if any later write fails |
| H2 | 1 | `migrations/027` | `ping_with_debounced` CTE unbounded scan on `block_ping_status` (300k+ rows); fixed in 031/044/045 but the original went to prod |
| H3 | 1 | migrations 033/039/040/042/043/044/045 | Plain `INSERT INTO schema_migrations` without `ON CONFLICT DO NOTHING` — manual-run footgun |
| H4 | 1 | `db_writer.py:204-298` | Three sequential cursor opens in `_update_station_identity`, exceptions in 2nd/3rd swallowed silently at DEBUG |
| H5 | 2 | `bulk_scheduler.py:1893-1944` | Second-chance retry registration silently skipped if trigger keys differ from cron `hour`/`minute` (new flexible schedule format may break this guard) |
| H6 | 2 | `bulk_scheduler.py:162` & `morning_recovery.py:71-88` | `CURRENT_DATE - 1` is timezone-dependent; if PG session tz isn't UTC, retry/recovery date is wrong |
| H7 | 2 | `cfg/discrepancy_log.py:173-175` | TOCTOU race in 173-station × 5-min health-probe SELECT-then-INSERT; second writer's discrepancy silently lost |
| H8 | 2 | `cli/cfg.py:820-829` | `--push-tos` batch mode pushes without confirmation if user is interactive — guard logic is inverted |
| H9 | 2 | `bulk_scheduler.py:441` | Health gate failure silently swallowed with `pass` — no DEBUG log makes it invisible |
| H10 | 3 | `utils/archive_validator.py:14` | Tmp-flush 65,536-byte truncation fix lives in `tostools` (re-export shim here) — not directly verified |
| H11 | 3 | `trimble/{netr9,netrs}_http_download_client.py` | NetR9/NetRS HTTP download paths not confirmed to have size-mismatch / progress-extend / mode-switch parity with PolaRX5 |
| H12 | 3 | `utils/file_archiver.py:313-329` | `_archive_immediately` accepts size-equal archive as success without gzip integrity check on existing archive |
| H13 | 4 | `icinga_client.py:1151-1157` | `cpu_load = {"available": False}` always sends UNKNOWN to Icinga for Trimble/G10 — permanent UNKNOWN CPU alerts. Same pattern for disk. |
| H14 | 4 | `polarx5_tcp_extractor.py:1630, 1655` | QZSS SVID range — docstring says 181-202, code matches only 181-187, SVIDs 188-202 land in `Unknown_X` |
| H15 | 4 | `db_writer.py:815` | `"skipped" not in err.lower()` deduplication is fragile — an upstream message change breaks it silently |
| H16 | 5 | Map dashboard panels 4-5 | Direct `block_health_summary` scan — migration 030 already replaced this pattern in the health dashboard but not the map |
| H17 | 5 | Online count divergence | Health dashboard excludes any station with `health_check IS NOT NULL` (i.e. passive); map dashboard only excludes `discontinued`. Two different "Online" totals |
| H18 | 5 | `grafana_sync.py cmd_push` | No `libraryPanel.uid` remapping — silently deploys broken library panel placeholder if seed-library hasn't run |

## Medium (16 findings — opportunistic, no urgency)

Pass 1: `is_stale` column from migration 045 not surfaced in `station_dashboard_data`; `db_writer.py:519` `or` short-circuit hides 0-satellite reads; uptime-zero falsy bug; `041` lacks index check.

Pass 2: `_RETRY_MAX_WORKERS = 8` hardcoded module constant; morning-recovery target_date drifting under misfire; `compare_station` has DB side-effects unannounced; `_DEG_PER_M_LON` hardcoded for Iceland latitude.

Pass 3: `_archive_immediately` failure-handler comment doesn't match success-path logic; Leica FTP defaults to `active` not `passive` (NAT will fail on every download); watchdog 500ms detection latency at top-of-loop only; retry-counter off-by-one in log; `archive_size` fallback to `local_file_size` masks archive failure.

Pass 4: Trimble SNR regex case-sensitive; `_parse_log_session_response` redundant case-handling; Trimble disk thresholds hardcoded bypassing `MetricChecker`; `send_health_from_json` CLI default omits `volt`/`cpu` checks.

Pass 5: Migration 046 missing schema_migrations entry (overlap with C2 — same root cause).

## Low (8 findings — style)

Pass 1: 042/043 generated from `pg_dump` not hand-written. Pass 2: dead `_schedule_backfill` deprecation wrapper; help-text mismatch (`?` vs `help`); priority dead branch. Pass 3: bare `except:` in Leica; comment mismatch in defaults; heavy import in RINEX worker. Pass 4: `_DISK_STATUS_MAP` defined unused; perfdata thresholds hardcoded; Trimble `_fetch_activity_page` swallows all exceptions silently. Pass 5: `_save_rotated_cookie` non-atomic write (truncates if killed mid-write).

## Suspect — needs runtime verification (8)

- 045 `last_known_ping` CTE — index coverage on `block_ping_status(sid, ts DESC)`?
- 041 `download_history` CTE — index on `download_log(sid, ts)`?
- 030 `flow_health` CTE — does PG re-evaluate per caller post-045 or share materialisation? `EXPLAIN (ANALYZE, BUFFERS)` needed
- `db_writer.py:162` — DNS resolution per cycle for hostname-based stations (cache lifetime)
- NTRIP block status mapping (`status_byte=4` = "Sending" — confirm against Septentrio reference)
- `_find_sbf_block` advances by `max(length, 8)` — incomplete-block fragment risk
- `_parse_disk_via_bin2asc` aggregate `usage_percent` vs `worst` status disagreement when multi-disk
- PolaRX5 fallback `_download_with_progressbar` (no tqdm) — production has tqdm but minimal images may not

## Praise (selected — keep doing this)

- Migration 034's `IS NOT TRUE` / `IS TRUE` fix for the NULL-debounce bug is the textbook PostgreSQL fix and the comment explains why.
- `db_writer` SAVEPOINT pattern applied consistently across optional subsystem writes.
- `cfg_discrepancy` open-row idempotency (supersede-then-insert) preserves drift history vs clobbering — sound design.
- `cookie rotation get_all()` over `get()` — handles Grafana sending `grafana_session` and `grafana_session_expiry` as separate Set-Cookie headers.
- Migration 030 JIT-off rationale is reproducible from the commit message.
- PolaRX5 FTP zombie-socket fix at `polarx5.py:1120-1144` is the template for the Leica fix needed in C6.
- `_path_to_dt` dict in PolaRX5 cleanly resolves the historic zip-misalignment bug.
- Trimble TrackingStatus error-response fix is the right pattern (XML section markers, not substring); diagnostic 200-char debug log.

---

## Implementation order (proposed)

Mirror the prior round: one PR per pass, ordered by impact-density. Each PR fixes Critical + High + cherry-picked Medium for that pass.

| # | Branch | PR scope | Estimate |
|---|--------|----------|----------|
| 1 | `fix/code-review-pass2-scheduler` | C3, C4, C5, H5, H6, H7, H8, H9 | ~half day |
| 2 | `fix/code-review-pass4-extractors` | C8 (Icinga disk), C9 (disk header fallback), H13 (CPU UNKNOWN), H14 (QZSS) | ~half day |
| 3 | `fix/code-review-pass3-leica-wallclock` | C6 (Leica FTP), C7 (wall-clock thread tracking), H10 verification, H11 verification | ~full day |
| 4 | `fix/code-review-pass1-migrations` | C1, C2, H1, H3, H4 | ~quarter day |
| 5 | `fix/code-review-pass5-dashboards` | C10 (panel 12), H16-H18 | ~quarter day |

**Suspects** (`Suspect` section) get a separate brief investigation note rather than fix branches — answers may be "no problem, here's why".

**Praise** items stay in this doc as a reference of patterns to keep applying.

---

## Tracking

Each fix PR closes the relevant rows here by editing this file. Critical/High move to a "Fixed in PR #N" subsection at the bottom of their pass. Suspects get verdicts written in-place (`✓ verified`, `✗ confirmed bug → moved to High`, etc.).

### Fixed in PR #32 (`fix/code-review-pass1-migrations`)

- **C1** `migrations/033_satellite_status_coalesce.sql` — wrapped in `BEGIN`/`COMMIT`. Was the only forward migration without a transaction.
- **C2 + H3** — added `INSERT INTO schema_migrations` to migrations 027, 028, 046; added `ON CONFLICT DO NOTHING` to migrations 029, 033, 039, 040, 042-046. Manual `psql -f` re-runs are now safe across the entire 027-046 range.
- **H1** `db_writer.py` — `model_mismatch` UPDATE wrapped in its own `SAVEPOINT identity_mismatch`. A later block-table write rolling back no longer silently undoes the mismatch flag.
- **H4** `db_writer.py:_update_station_identity` — bumped log level from DEBUG to WARNING for both the receiver_type lookup failure and the model_mismatch UPDATE failure; each runs in its own SAVEPOINT for isolation. Operators can now see when these silently fail in production logs.

Verified locally: `receivers db setup` (drop + migrate + seed) applies all 47 migrations cleanly, and a follow-up `receivers db migrate` is a no-op (idempotent).

### Fixed in PR #31 (`fix/code-review-pass3-leica-wallclock`)

- **C6** `leica/leica_ftp_download_client.py` — ported the PolaRX5 zombie-socket fix to G10. Both the primary and the mode-switch fallback now `ftp = None` before `try`; the new `_safe_ftp_close()` static helper is called from every `except` path. Closes the leak that fired any time `connect()` succeeded but `login()`/`cwd()`/`retrbinary()` raised.
- **C7** `cli/parallel.py` — added `_record_abandoned_thread()` + `_abandoned_threads_count` module-level counter. Wall-clock-timeout abandonments now bump the counter, and once `≥10` threads are abandoned in the run we log a WARNING with the running total and a hint to restart the scheduler. `get_abandoned_thread_count()` exposes the count for future metric exporters / dashboards.
- **H10** verified — `tostools.utils.archive.ArchiveValidator._validate_tmp_file_integrity` does a full gzip read; smoke-tested with a 500 KB random `.sbf.gz` truncated to 65,536 bytes — returns `False` correctly. The original bug is fixed; no further action needed. Note: a *complete* small gz padded with zeros up to 65 K bytes returns `True` (gzip ignores trailing bytes after EOF marker), which is correct behaviour.
- **H11** verified — NetR9 (`http_download_client.py`) and NetRS (`netrs_http_download_client.py`) HTTP paths have stall timeout, size-mismatch detection, and post-download integrity validation. They lack PolaRX5's progress-aware 50% timeout extension because that's an FTP recv-watchdog concept; HTTP uses `requests` per-chunk timeouts with different mechanics. Cleanup applied: replaced misleading "Partial file kept for resume" log line with accurate "Partial kept on disk; next retry will start fresh" — HTTP doesn't support Range requests so the next call's `should_resume_download()` deletes the partial and starts over.

### Fixed in PR #30 (`fix/code-review-pass4-extractors`)

- **C8** `monitoring/icinga_client.py:1196` — disk read changed from `data_quality["disk"]` (always empty for PolaRX5/Trimble/G10) to `metrics["disk"]`. Every PolaRX5 station now reports real disk status to Icinga instead of permanent UNKNOWN.
- **C9** `health/polarx5_tcp_extractor.py:_parse_disk_header_only` — removed misleading dead `_DISK_STATUS_MAP`; documented explicitly that the header-only fallback can't detect full/error states; added `limited_check=True` flag so downstream knows the result isn't authoritative; logs a WARNING when the fallback fires.
- **H13** `monitoring/icinga_client.py` — added `_is_metric_available()` helper that recognises the `{"available": False}` sentinel from G10/Trimble extractors. Temp, CPU, and disk checks now skip cleanly instead of emitting permanent UNKNOWN to Icinga for receiver types that don't expose the metric. Removed unused `data_quality` local variable.
- **H14** `health/polarx5_tcp_extractor.py:_svid_to_constellation` — docstring corrected to match the non-overlapping SVID ranges actually used (QZSS 181-187, IRNSS 191-197); the prior docstring's "QZSS 181-202" was misleading. SVIDs 188-190/198-200 are reserved/unused on real receivers, correctly returning `Unknown_X`.

### Fixed in PR #29 (`fix/code-review-pass2-scheduler`)

- **C3** `bulk_scheduler.py:_download_station_data_job` — outer except now records `_record_batch_result(..., "fail", ...)` so hard exceptions appear in the periodic batch report.
- **C4** `bulk_scheduler.py` — `audit_logger = None` initialised before the try; outer except uses direct `if audit_logger:` (was `if "audit_logger" in locals()`).
- **C5** `morning_recovery.py` — silent fallback replaced with explicit `config_resolved` flag + WARNING log when scheduler instance / schedule_configs unreachable.
- **H5** `bulk_scheduler.py` — second-chance / batch-summary registration guard renamed to `is_daily_cron`; non-daily-cron sessions now emit DEBUG line documenting the intentional skip.
- **H6** `bulk_scheduler.py:_retry_failed_daily_job` — replaced session-tz-dependent `CURRENT_DATE - 1` with explicit `yesterday_utc` parameter computed in Python.
- **H7** `cfg/discrepancy_log.py:record_detection` — added `pg_advisory_xact_lock(hashtext(station_id), hashtext(cfg_key))` to serialise concurrent writers on the same key, avoiding partial-unique-index collisions. Tests updated for the new `execute()` call counts.
- **H8** `cli/cfg.py` — `--push-tos` batch mode now requires explicit `--yes` (or `--dry-run` to preview); interactive mode without consent prints a clear hint instead of silently writing to TOS.
- **H9** `bulk_scheduler.py` — health-gate `except` no longer silent; logs the failure category at DEBUG.

Side cleanups: removed two pre-existing unused locals (`job_id`, `recv_logger`) flagged by ruff once `audit_logger` started being directly referenced.
