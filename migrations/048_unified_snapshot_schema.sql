-- Migration 048: unified 60-s snapshot schema (TimescaleDB hypertables)
--
-- Purpose
-- -------
-- Introduces the new station-snapshot architecture agreed on 2026-05-28:
-- one row per (sid, ts) per logical domain, on 60-s ticks, NULL where source
-- data is missing. Populated by BOTH the SBF parser (status_1hr files) and
-- the live TCP probe — upsert semantics let both pathways write to the same
-- rows, eventually-consistent.
--
-- Tables created (all hypertables, partitioned by ts)
-- ---------------------------------------------------
--   station_health_60s        — receiver-side SBF (power, rx, position, disk, log, time, net)
--   station_network_60s       — scheduler-probed connectivity (ICMP, FTP/HTTP/CTRL, NTRIP)
--   station_sat_summary_60s   — per-constellation tracking summary (booleans + counts)
--   station_sat_signal_60s    — row-per-signal CNR detail (ChannelStatus / MeasEpoch)
--
-- Continuous aggregate
-- --------------------
--   station_health_1h         — hourly rollup for long-range history panels
--
-- Convenience view
-- ----------------
--   station_snapshot_60s      — FULL JOIN of the 3 dense tables on (sid, ts);
--                               sat_signal queried separately (different join shape)
--
-- Migration does NOT
-- ------------------
--   - Migrate data from the existing block_*_status tables (Phase 3 aggregator job)
--   - Drop existing block_*_status tables (Phase 7, after dashboard cutover)
--   - Modify Grafana dashboards
--   - Install the TimescaleDB extension itself
--
-- Preconditions
-- -------------
--   - PostgreSQL ≥ 17
--   - TimescaleDB extension installed and loaded
--     - apt:  postgresql-17-timescaledb (Community Edition / TSL — includes
--             hypertables, compression/Hypercore, continuous aggregates,
--             retention policies, hyperfunctions)
--     - postgresql.conf: shared_preload_libraries = 'timescaledb' (restart req'd)
--   - stations table exists (referenced by FKs)
--
-- Rollback companion: 048_unified_snapshot_schema_rollback.sql

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- Preflight: refuse to run without TimescaleDB
-- ────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE EXCEPTION
            'Migration 048 requires the timescaledb extension. '
            'Install postgresql-17-timescaledb and add `timescaledb` to '
            'shared_preload_libraries, then CREATE EXTENSION timescaledb.';
    END IF;
END $$;

-- ────────────────────────────────────────────────────────────────────────────
-- 1.  station_health_60s — SBF receiver data (status_1hr session blocks)
--     Populated by SBF parser AND live TCP probe; both UPSERT on (sid, ts).
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS station_health_60s (
    sid VARCHAR(4)  NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts  TIMESTAMPTZ NOT NULL,

    -- PowerStatus (block 4101)
    voltage         REAL,
    power_source    VARCHAR(10),

    -- ReceiverStatus (block 4014)
    cpu_load        REAL,
    temperature     REAL,
    uptime_seconds  INT,
    rx_status       INT,
    rx_error        INT,
    ext_error       INT,

    -- PVTGeodetic (block 4007) + PosCovGeodetic (4006)
    fix_type        VARCHAR(10),
    nr_sv           SMALLINT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    height          REAL,
    h_accuracy      REAL,
    v_accuracy      REAL,
    latency         REAL,
    raim_status     VARCHAR(20),

    -- DiskStatus (block 4059)
    disk_used_mb    INT,
    disk_total_mb   INT,
    disk_usage_pct  REAL,

    -- LogStatus (block 4102)
    active_sessions    INT,
    session_15s_24hr   BOOLEAN,
    session_1hz_1hr    BOOLEAN,
    session_status_1hr BOOLEAN,

    -- ReceiverTime (block 5914)
    time_sync_level    VARCHAR(20),
    delta_ls           INT,

    -- IPStatus / WiFiAPStatus (network identity)
    rx_ip_address      INET,
    wifi_clients       SMALLINT,

    -- Provenance — which pathway populated this row first
    source             VARCHAR(10),  -- 'tcp_probe' | 'sbf_parse' | 'merged'
    written_at         TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE station_health_60s IS
    'Unified 60-s receiver health snapshot. Both SBF parser and live TCP probe '
    'UPSERT into the same rows. Source column tracks the pathway that wrote first.';

SELECT create_hypertable(
    'station_health_60s', by_range('ts', INTERVAL '1 month'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_health_60s_ts
    ON station_health_60s (ts DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 2.  station_network_60s — scheduler-probed connectivity
--     Sparse at 60-s cadence (probes run every 5 min currently); ~80% NULL
--     ticks. Use locf() / interpolate() in dashboards for visual continuity.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS station_network_60s (
    sid VARCHAR(4)  NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts  TIMESTAMPTZ NOT NULL,

    -- ICMP
    ping_online         BOOLEAN,
    ping_ms             REAL,
    ping_loss_pct       REAL,

    -- 3-port TCP probe (FTP, HTTP, CTRL=28784)
    ftp_port_open       BOOLEAN,
    ftp_response_ms     REAL,
    http_port_open      BOOLEAN,
    http_response_ms    REAL,
    ctrl_port_open      BOOLEAN,
    ctrl_response_ms    REAL,    -- NULL until probe service is extended

    -- Composite + NTRIP
    overall_status      VARCHAR(20),
    ntrip_server_status VARCHAR(20),
    ntrip_error_code    VARCHAR(20),

    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE station_network_60s IS
    'Scheduler-probed network connectivity (rek-d01 → station). Sparse at 60-s '
    'ticks because probes run every 5 min; dashboards should locf() these columns.';

SELECT create_hypertable(
    'station_network_60s', by_range('ts', INTERVAL '1 month'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_network_60s_ts
    ON station_network_60s (ts DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 3.  station_sat_summary_60s — per-constellation summary
--     Sourced from QualityInd (4082) and ChannelStatus (4013) aggregation.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS station_sat_summary_60s (
    sid VARCHAR(4)  NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts  TIMESTAMPTZ NOT NULL,

    sats_total      SMALLINT,

    -- "is the receiver currently tracking this constellation?"
    has_gps         BOOLEAN,
    has_glonass     BOOLEAN,
    has_galileo     BOOLEAN,
    has_beidou      BOOLEAN,
    has_sbas        BOOLEAN,
    has_qzss        BOOLEAN,
    has_irnss       BOOLEAN,

    -- Counts (cheap; useful for trend panels — drop later if unused)
    sats_gps        SMALLINT,
    sats_glonass    SMALLINT,
    sats_galileo    SMALLINT,
    sats_beidou     SMALLINT,
    sats_sbas       SMALLINT,
    sats_qzss       SMALLINT,
    sats_irnss      SMALLINT,

    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE station_sat_summary_60s IS
    'Per-constellation tracking summary. Booleans for at-a-glance, counts for trend panels.';

SELECT create_hypertable(
    'station_sat_summary_60s', by_range('ts', INTERVAL '1 month'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_sat_summary_60s_ts
    ON station_sat_summary_60s (ts DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 4.  station_sat_signal_60s — row-per-signal CNR detail
--     Sourced from ChannelStatus (4013); MeasEpoch (4109) is a future option.
--
--     Storage shape:
--       Fleet signal universe: 32 codes (GPS L1CA/L1PY/L1C/L2C/L2PY/L5,
--         GLONASS L1CA/L1P/L2CA/L2P/L3, Galileo E1BC/E5/E5a/E5b/E6BC,
--         BeiDou B1I/B1C/B2I/B2a/B2b/B3I, NavIC L5, QZSS L1*/L2C/L5*, SBAS L1/L5)
--       Standard tier = 10 sigs (71% of fleet), Extended = 23 sigs (19%).
--       Row-per-signal handles tier variance without ALTER TABLE.
--
--     Volume: ~178 sta × ~30 sats × ~12 sigs avg × 1440 ticks/day ≈ 92 M rows/day.
--     Retention: drop_chunks at 7 days → ~640 M rows steady state → ~20-30 GB
--     under Hypercore compression. Daily chunks (not monthly) for compression
--     payoff.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS station_sat_signal_60s (
    sid VARCHAR(4)  NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts  TIMESTAMPTZ NOT NULL,
    svid VARCHAR(6) NOT NULL,           -- 'G04', 'R01', 'E10', 'C19', 'I05', …
    constellation CHAR(1) NOT NULL,     -- G / R / E / C / S / J / I

    -- Position-in-sky (same for all signals of one sat at one tick)
    elevation REAL,
    azimuth   REAL,

    -- Signal level
    signal_band VARCHAR(8) NOT NULL,    -- 'L1CA', 'L1PY', 'L2C', 'E5a', 'B1I', …
    cnr_db_hz   REAL,
    tracking_status TEXT,               -- 'Tracking' | 'Searching' | 'Unhealthy' | NULL

    PRIMARY KEY (sid, ts, svid, signal_band)
);

COMMENT ON TABLE station_sat_signal_60s IS
    'Row-per-signal CNR detail from ChannelStatus. 7-day retention. Query for '
    'current state via WHERE ts = (SELECT max(ts) FROM …); historical analysis '
    'via per-(sid, svid, signal_band) time ranges within the 7-day window.';

SELECT create_hypertable(
    'station_sat_signal_60s', by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

-- Lookup by (sid, ts) for snapshot reads; sat_signal_60s_pkey already covers
-- (sid, ts, svid, signal_band) so a separate ts-DESC index isn't necessary
-- for "latest tick" queries — chunk pruning + the PK handles it.

-- ────────────────────────────────────────────────────────────────────────────
-- Hypercore compression policies (TSL Community Edition)
--     compress_segmentby groups rows for the same logical entity into a single
--     compressed segment, dramatically improving compression ratio and
--     filtered-scan speed.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE station_health_60s
    SET (timescaledb.compress, timescaledb.compress_segmentby = 'sid');
ALTER TABLE station_network_60s
    SET (timescaledb.compress, timescaledb.compress_segmentby = 'sid');
ALTER TABLE station_sat_summary_60s
    SET (timescaledb.compress, timescaledb.compress_segmentby = 'sid');
ALTER TABLE station_sat_signal_60s
    SET (timescaledb.compress,
         timescaledb.compress_segmentby = 'sid, svid',
         timescaledb.compress_orderby   = 'ts DESC, signal_band');

-- Compress chunks once they're cold:
--   - dense tables: 30 days (recent month stays uncompressed for write speed)
--   - sat_signal:    1 day (high volume; compress aggressively)
SELECT add_compression_policy('station_health_60s',      INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('station_network_60s',     INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('station_sat_summary_60s', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('station_sat_signal_60s',  INTERVAL '1 day',   if_not_exists => TRUE);

-- ────────────────────────────────────────────────────────────────────────────
-- Retention policy: only sat_signal has a hard drop (volume-driven)
--     The 3 dense tables grow ~256 K rows/day combined → no urgency to drop.
--     Add a retention policy explicitly later if storage becomes a concern.
-- ────────────────────────────────────────────────────────────────────────────

SELECT add_retention_policy('station_sat_signal_60s', INTERVAL '7 days', if_not_exists => TRUE);

-- ────────────────────────────────────────────────────────────────────────────
-- Continuous aggregate — hourly rollup of station_health_60s
--     Powers long-range history panels (voltage trend, temperature trend,
--     disk-fill projection). Real-time aggregates keep the "tail" current.
-- ────────────────────────────────────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS station_health_1h
WITH (timescaledb.continuous) AS
SELECT sid,
       time_bucket('1 hour', ts) AS bucket,
       avg(voltage)        AS voltage_avg,
       min(voltage)        AS voltage_min,
       max(voltage)        AS voltage_max,
       avg(cpu_load)       AS cpu_load_avg,
       max(cpu_load)       AS cpu_load_max,
       avg(temperature)    AS temperature_avg,
       max(temperature)    AS temperature_max,
       max(disk_usage_pct) AS disk_pct_max,
       count(*) FILTER (WHERE rx_error  <> 0) AS rx_error_count,
       count(*) FILTER (WHERE ext_error <> 0) AS ext_error_count,
       count(*) AS samples
FROM station_health_60s
GROUP BY sid, time_bucket('1 hour', ts)
WITH NO DATA;  -- materialise on first refresh, not now (empty table)

SELECT add_continuous_aggregate_policy(
    'station_health_1h',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);

-- TimescaleDB continuous aggregates are exposed as regular views in the
-- catalog (the actual materialization lives in a hypertable underneath),
-- so COMMENT ON VIEW is correct here, not COMMENT ON MATERIALIZED VIEW.
COMMENT ON VIEW station_health_1h IS
    'Hourly rollup of station_health_60s for long-range trend panels. '
    'Real-time aggregates fill the last hour from the 60-s source.';

-- ────────────────────────────────────────────────────────────────────────────
-- Convenience view — 60-s snapshot row joined across the 3 dense tables
--     Dashboards select FROM station_snapshot_60s instead of writing the
--     FULL JOIN themselves. sat_signal is intentionally not joined — its
--     row-per-signal shape would explode the snapshot. Query separately.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW station_snapshot_60s AS
SELECT
    COALESCE(h.sid, n.sid, s.sid) AS sid,
    COALESCE(h.ts,  n.ts,  s.ts)  AS ts,
    -- station_health_60s columns
    h.voltage, h.power_source,
    h.cpu_load, h.temperature, h.uptime_seconds, h.rx_status, h.rx_error, h.ext_error,
    h.fix_type, h.nr_sv, h.latitude, h.longitude, h.height,
    h.h_accuracy, h.v_accuracy, h.latency, h.raim_status,
    h.disk_used_mb, h.disk_total_mb, h.disk_usage_pct,
    h.active_sessions, h.session_15s_24hr, h.session_1hz_1hr, h.session_status_1hr,
    h.time_sync_level, h.delta_ls,
    h.rx_ip_address, h.wifi_clients,
    h.source AS health_source,
    -- station_network_60s columns
    n.ping_online, n.ping_ms, n.ping_loss_pct,
    n.ftp_port_open, n.ftp_response_ms,
    n.http_port_open, n.http_response_ms,
    n.ctrl_port_open, n.ctrl_response_ms,
    n.overall_status, n.ntrip_server_status, n.ntrip_error_code,
    -- station_sat_summary_60s columns
    s.sats_total,
    s.has_gps, s.has_glonass, s.has_galileo, s.has_beidou,
    s.has_sbas, s.has_qzss, s.has_irnss,
    s.sats_gps, s.sats_glonass, s.sats_galileo, s.sats_beidou,
    s.sats_sbas, s.sats_qzss, s.sats_irnss
FROM      station_health_60s      h
FULL JOIN station_network_60s     n USING (sid, ts)
FULL JOIN station_sat_summary_60s s USING (sid, ts);

COMMENT ON VIEW station_snapshot_60s IS
    'Joined 60-s snapshot across the 3 dense unified tables. Use this in '
    'dashboards instead of writing the FULL JOIN by hand. sat_signal_60s '
    'queried separately (different row shape).';

COMMIT;

-- ════════════════════════════════════════════════════════════════════════════
-- Post-migration verification (run separately; not part of the transaction)
-- ════════════════════════════════════════════════════════════════════════════
-- SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';
-- SELECT * FROM timescaledb_information.hypertables WHERE hypertable_name LIKE 'station_%';
-- SELECT * FROM timescaledb_information.compression_settings WHERE hypertable_name LIKE 'station_%';
-- SELECT * FROM timescaledb_information.jobs WHERE proc_name IN
--   ('policy_compression', 'policy_retention', 'policy_refresh_continuous_aggregate');
