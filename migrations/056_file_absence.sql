-- Migration 056: file_absence — the durable "don't re-fetch" ledger
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2). Records that a specific file slot is CONFIRMED ABSENT at a
-- source (the receiver, primarily) — a reachable-but-no-file result (FTP 550 /
-- HTTP 404 on a LIVE connection), never a connection error. This is the terminal
-- "missing on receiver" knowledge ask #3 needs so the scheduler stops re-fetching
-- a file that genuinely does not exist upstream.
--
-- Independent of file_tracking (survives its TRUNCATE) and of the 24 h transient
-- TTL. Two things gate a permanent skip:
--   * transient (mig 046): file_tracking.status='missing' within 24 h — retry
--     after that in case the receiver caught up (the HVSK "served later" case);
--   * terminal (this table): confirmed absent repeatedly ACROSS ENOUGH ELAPSED
--     TIME that "the receiver caught up later" is ruled out.
--
-- TERMINAL IS TIME-SPANNED, NOT COUNT-ONLY. A 1Hz station probed hourly would,
-- under a pure count threshold, mark a file that lands in hour 4 as terminal by
-- hour 3 and skip it forever — re-breaking mig 046. So terminal requires the
-- confirmations to span >= terminal_after_days of wall-clock, not merely N probes
-- in one day.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/056_file_absence.sql

BEGIN;

CREATE TABLE IF NOT EXISTS file_absence (
    id                 BIGSERIAL   PRIMARY KEY,
    -- where the file is absent. 'receiver' (upstream) is the ask-#3 case; other
    -- locations (imo_archive, epos_portal) reuse the same ledger later.
    source_location    VARCHAR(30) NOT NULL DEFAULT 'receiver',
    sid                VARCHAR(16) NOT NULL,
    -- the DOWNLOAD session_type (raw tier: '15s_24hr'/'1Hz_1hr'/'status_1hr') —
    -- receiver absence is about the raw file, never the derived rinex.
    session_type       VARCHAR(32) NOT NULL,
    file_date          DATE        NOT NULL,
    -- hour-of-day for hourly sessions; NULL for daily (NULL-safe via the two
    -- partial unique indexes below + IS NOT DISTINCT FROM in the functions).
    file_hour          SMALLINT,
    -- how many reachable-but-absent confirmations we have logged for this slot.
    confirmations      INTEGER     NOT NULL DEFAULT 1,
    first_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- terminal = ruled genuinely, permanently absent on the source. Only set once
    -- the confirmations span enough elapsed time (see record_file_absence).
    terminal           BOOLEAN     NOT NULL DEFAULT false
);

-- NULL-safe identity: a daily slot (file_hour NULL) and an hourly slot dedup
-- under separate partial unique indexes (mirrors file_tracking's pattern), so a
-- plain ON CONFLICT is avoided and NULL hours never collide/duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_absence_daily
    ON file_absence (source_location, sid, session_type, file_date)
    WHERE file_hour IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_absence_hourly
    ON file_absence (source_location, sid, session_type, file_date, file_hour)
    WHERE file_hour IS NOT NULL;

-- terminal-lookup path for is_file_missing (and future differential anti-joins).
CREATE INDEX IF NOT EXISTS idx_file_absence_terminal
    ON file_absence (source_location, sid, session_type, file_date, file_hour)
    WHERE terminal;

COMMENT ON TABLE file_absence IS
    'Durable confirmed-absent ledger (reachable-but-404/550). Terminal = ruled '
    'permanently absent on the source; independent of file_tracking + the 24h TTL.';

-- ---------------------------------------------------------------------------
-- record_file_absence: log one confirmed-absent observation, time-spanned terminal.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION record_file_absence(
    p_sid                VARCHAR,
    p_session_type       VARCHAR,
    p_date               DATE,
    p_hour               SMALLINT DEFAULT NULL,
    p_source             VARCHAR  DEFAULT 'receiver',
    p_terminal_after_days INTEGER DEFAULT 3,
    p_min_confirmations   INTEGER DEFAULT 3
) RETURNS VOID AS $$
BEGIN
    UPDATE file_absence
       SET confirmations     = confirmations + 1,
           last_confirmed_at  = now(),
           -- promote to terminal once confirmations span >= terminal_after_days
           -- of wall-clock AND we have >= min_confirmations. Never demote.
           terminal = terminal OR (
               now() - first_confirmed_at
                   >= (p_terminal_after_days * INTERVAL '1 day')
               AND confirmations + 1 >= p_min_confirmations
           )
     WHERE source_location = p_source
       AND sid = p_sid
       AND session_type = p_session_type
       AND file_date = p_date
       AND file_hour IS NOT DISTINCT FROM p_hour;
    IF NOT FOUND THEN
        BEGIN
            INSERT INTO file_absence
                (source_location, sid, session_type, file_date, file_hour)
            VALUES (p_source, p_sid, p_session_type, p_date, p_hour);
        EXCEPTION WHEN unique_violation THEN
            -- concurrent inserter won the race; count their row instead.
            UPDATE file_absence
               SET confirmations = confirmations + 1, last_confirmed_at = now()
             WHERE source_location = p_source AND sid = p_sid
               AND session_type = p_session_type AND file_date = p_date
               AND file_hour IS NOT DISTINCT FROM p_hour;
        END;
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION record_file_absence IS
    'Log one reachable-but-absent confirmation for a file slot; promote to '
    'terminal only once confirmations span >= terminal_after_days (time-spanned, '
    'not count-only, so a same-day-late hourly file is not frozen).';

-- ---------------------------------------------------------------------------
-- is_file_missing: reworked to consult file_absence (terminal → permanent skip)
-- plus the mig-046 24h transient TTL. NULL-safe via IS NOT DISTINCT FROM.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR,
    p_session_type VARCHAR,
    p_date DATE,
    p_hour SMALLINT DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    -- (1) terminal absence on the receiver → permanent skip (never re-fetch).
    IF EXISTS (
        SELECT 1 FROM file_absence
        WHERE source_location = 'receiver'
          AND sid = p_sid
          AND session_type = p_session_type
          AND file_date = p_date
          AND file_hour IS NOT DISTINCT FROM p_hour
          AND terminal
    ) THEN
        RETURN TRUE;
    END IF;
    -- (2) transient: a 404/550 within the last 24 h (mig 046) — retry after that
    -- in case the receiver caught up or the miss was transient.
    RETURN EXISTS (
        SELECT 1 FROM file_tracking
        WHERE sid = p_sid
          AND session_type = p_session_type
          AND file_date = p_date
          AND file_hour IS NOT DISTINCT FROM p_hour
          AND status = 'missing'
          AND last_checked > NOW() - INTERVAL '24 hours'
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION is_file_missing IS
    'Skip-download check: TRUE if the slot is terminally absent on the receiver '
    '(file_absence) or was 404/550 within the last 24 h (file_tracking). '
    'NULL-safe on file_hour (daily vs hourly).';

INSERT INTO schema_migrations (migration_name)
VALUES ('056_file_absence')
ON CONFLICT DO NOTHING;

COMMIT;
