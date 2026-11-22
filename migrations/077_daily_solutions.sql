-- Migration 077: daily GNSS POS solution monitoring (slice 2)
--
-- Plan: docs/architecture/daily-solutions-implementation-plan.md (§3, §5 slice 2).
--
-- Tables
--   solution_stream           per-stream config: lag, deadline, freshness TTL, window
--   solution_roster           the CURRENT roster snapshot per stream (version, reviewed)
--   solution_roster_station   stations in that snapshot
--   solution_roster_interval  expected / excluded intervals in that snapshot
--   solution_check_run        one row per check run (outcome, coverage, inventory)
--   solution_fact             last known fact per (stream, station, day)
--   solution_transition       presence/validity changes only (not every revision)
--   solution_completion(_item) producer completion records (filled from slice 4)
--
-- Functions
--   record_solution_check(payload jsonb)  installs ONE complete validated snapshot
--       or nothing. SECURITY DEFINER, so the checker needs EXECUTE only.
--       Every timestamp and id comes from the payload (the application), so
--       rek-d01 and pgdev, written as two explicit destinations (G8), cannot
--       diverge on generated values.
--   solution_day_grid(stream, day_from, day_to, now)  THE single authority for
--       the six display states (§3). Plain inlinable SQL: `now` is a parameter
--       and the body never calls now(); the EXPLAIN gate sees the real plan.
--
-- No block_* table is touched. Transition retention is not implemented yet: it
-- is set from the G7 churn measurement.
--
-- Apply as the OWNER (bgo), on each host (migration 073's lesson):
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/077_daily_solutions.sql
-- Reversible: 077_daily_solutions_rollback.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS solution_stream (
    stream              TEXT PRIMARY KEY,
    expected_lag_days   SMALLINT NOT NULL CHECK (expected_lag_days BETWEEN 0 AND 30),
    deadline_time       TIME     NOT NULL,       -- UTC, on day + expected_lag_days
    -- a check older than this turns its in-window cells unknown/check_stale;
    -- 3 h = three missed runs of the hourly gps-solution-health.timer
    freshness_ttl       INTERVAL NOT NULL CHECK (freshness_ttl > interval '0'),
    window_days         SMALLINT NOT NULL CHECK (window_days BETWEEN 1 AND 366)
);

-- G4: lag D-1 and the 09:15 UTC deadline were measured on yang (plan §2)
INSERT INTO solution_stream (stream, expected_lag_days, deadline_time, freshness_ttl, window_days)
VALUES ('imo.24hr_itrf14', 1, '09:15', interval '3 hours', 45)
ON CONFLICT (stream) DO NOTHING;

CREATE TABLE IF NOT EXISTS solution_roster (
    stream              TEXT PRIMARY KEY REFERENCES solution_stream (stream),
    version             CHAR(64)    NOT NULL,    -- sha256 of the roster file
    reviewed_from       DATE        NOT NULL,
    reviewed_to         DATE,                    -- inclusive; NULL = open
    installed_at        TIMESTAMPTZ NOT NULL,    -- the installing run's checked_at
    installed_run_id    UUID        NOT NULL,
    CHECK (reviewed_to IS NULL OR reviewed_to >= reviewed_from)
);

CREATE TABLE IF NOT EXISTS solution_roster_station (
    stream              TEXT NOT NULL REFERENCES solution_roster (stream) ON DELETE CASCADE,
    station             VARCHAR(4) NOT NULL CHECK (station ~ '^[A-Z0-9]{4}$'),
    PRIMARY KEY (stream, station)
);

CREATE TABLE IF NOT EXISTS solution_roster_interval (
    stream              TEXT NOT NULL,
    station             VARCHAR(4) NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('expected', 'excluded')),
    day_from            DATE NOT NULL,
    day_to              DATE,                    -- inclusive; NULL = open
    reason              TEXT,
    CHECK (day_to IS NULL OR day_to >= day_from),
    FOREIGN KEY (stream, station)
        REFERENCES solution_roster_station (stream, station) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_solution_roster_interval
    ON solution_roster_interval (stream, station);

CREATE TABLE IF NOT EXISTS solution_check_run (
    run_id              UUID PRIMARY KEY,
    stream              TEXT        NOT NULL REFERENCES solution_stream (stream),
    checked_at          TIMESTAMPTZ NOT NULL,
    window_from         DATE        NOT NULL,
    window_to           DATE        NOT NULL,
    outcome             TEXT        NOT NULL
        CHECK (outcome IN ('ok', 'source_unavailable', 'failed')),
    error               TEXT,
    source              TEXT        NOT NULL,
    roster_version      CHAR(64)    NOT NULL,
    cell_count          INTEGER     NOT NULL CHECK (cell_count >= 0),
    files               JSONB       NOT NULL DEFAULT '[]',  -- inventory: path, sha256, size
    unexpected_files    JSONB       NOT NULL DEFAULT '[]',
    other_products      JSONB       NOT NULL DEFAULT '{}',
    completion_id       TEXT,                    -- slice 4
    payload_sha256      CHAR(64)    NOT NULL,    -- a replayed run_id must match
    CHECK (window_to >= window_from)
);
CREATE INDEX IF NOT EXISTS idx_solution_check_run_stream_time
    ON solution_check_run (stream, checked_at DESC);

CREATE TABLE IF NOT EXISTS solution_fact (
    stream              TEXT       NOT NULL REFERENCES solution_stream (stream),
    station             VARCHAR(4) NOT NULL,
    day                 DATE       NOT NULL,
    -- last KNOWN fact; NULL while the day has only ever been unverifiable
    fact                TEXT CHECK (fact IN ('valid', 'invalid', 'absent')),
    code                TEXT,
    fingerprint         CHAR(64),
    first_seen_at       TIMESTAMPTZ,             -- first run that saw a row (valid/invalid)
    last_verified_at    TIMESTAMPTZ,             -- last run that observed `fact`
    last_revised_at     TIMESTAMPTZ,             -- last fingerprint change of a valid row
    revision_count      INTEGER NOT NULL DEFAULT 0,
    -- an unverifiable observation never overwrites the last known fact
    -- (history is not rewritten); it is recorded here instead
    unverifiable_code   TEXT,
    unverifiable_at     TIMESTAMPTZ,
    last_run_id         UUID NOT NULL REFERENCES solution_check_run (run_id),
    PRIMARY KEY (stream, station, day),
    CHECK (fact IS NOT NULL OR unverifiable_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS solution_transition (
    stream              TEXT       NOT NULL,
    station             VARCHAR(4) NOT NULL,
    day                 DATE       NOT NULL,
    run_id              UUID       NOT NULL REFERENCES solution_check_run (run_id),
    at                  TIMESTAMPTZ NOT NULL,
    from_fact           TEXT NOT NULL,
    from_code           TEXT,
    to_fact             TEXT NOT NULL,
    to_code             TEXT,
    PRIMARY KEY (stream, station, day, run_id)
);

CREATE TABLE IF NOT EXISTS solution_completion (
    completion_id       TEXT PRIMARY KEY,
    stream              TEXT        NOT NULL REFERENCES solution_stream (stream),
    completed_at        TIMESTAMPTZ NOT NULL,
    day_from            DATE        NOT NULL,
    day_to              DATE        NOT NULL,
    CHECK (day_to >= day_from)
);
CREATE INDEX IF NOT EXISTS idx_solution_completion_stream_time
    ON solution_completion (stream, completed_at DESC);

CREATE TABLE IF NOT EXISTS solution_completion_item (
    completion_id       TEXT       NOT NULL REFERENCES solution_completion ON DELETE CASCADE,
    station             VARCHAR(4) NOT NULL,
    day                 DATE       NOT NULL,
    outcome             TEXT       NOT NULL CHECK (outcome IN ('rejected', 'failed')),
    PRIMARY KEY (completion_id, station, day)
);

-- ------------------------------------------------------------------ writer

CREATE OR REPLACE FUNCTION record_solution_check(payload jsonb)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_run_id      uuid;
    v_stream      text;
    v_checked_at  timestamptz;
    v_from        date;
    v_to          date;
    v_outcome     text;
    v_version     text;
    v_sha         text := encode(sha256(convert_to(payload::text, 'UTF8')), 'hex');
    v_existing    text;
    v_n_stations  integer;
    v_n_days      integer;
    v_n_cells     integer;
    v_bad         text;
    v_roster      jsonb := payload -> 'roster';
BEGIN
    -- ---- validate everything before writing anything
    IF payload ->> 'schema' IS DISTINCT FROM 'solution-check-facts/2' THEN
        RAISE EXCEPTION 'record_solution_check: schema must be solution-check-facts/2, got %',
            coalesce(payload ->> 'schema', 'NULL');
    END IF;
    v_run_id     := (payload ->> 'run_id')::uuid;
    v_stream     := payload ->> 'stream';
    v_checked_at := (payload ->> 'checked_at')::timestamptz;
    v_from       := (payload -> 'window' ->> 'from')::date;
    v_to         := (payload -> 'window' ->> 'to')::date;
    v_outcome    := payload ->> 'outcome';
    v_version    := v_roster ->> 'version';
    IF v_run_id IS NULL OR v_checked_at IS NULL OR v_from IS NULL OR v_to IS NULL
       OR v_outcome IS NULL OR v_version IS NULL THEN
        RAISE EXCEPTION 'record_solution_check: run_id, checked_at, window, outcome and roster.version are required';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM solution_stream WHERE stream = v_stream) THEN
        RAISE EXCEPTION 'record_solution_check: unknown stream %', v_stream;
    END IF;
    IF v_outcome NOT IN ('ok', 'source_unavailable', 'failed') THEN
        RAISE EXCEPTION 'record_solution_check: bad outcome %', v_outcome;
    END IF;
    IF v_to < v_from THEN
        RAISE EXCEPTION 'record_solution_check: window to % before from %', v_to, v_from;
    END IF;
    IF v_version !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'record_solution_check: roster.version is not a sha256';
    END IF;

    SELECT payload_sha256 INTO v_existing FROM solution_check_run WHERE run_id = v_run_id;
    IF FOUND THEN
        IF v_existing = v_sha THEN
            RETURN 'duplicate';
        END IF;
        RAISE EXCEPTION 'record_solution_check: run % already recorded with a different payload', v_run_id;
    END IF;

    CREATE TEMP TABLE IF NOT EXISTS _sol_cells (
        station text, day date, fact text, code text, fingerprint text
    ) ON COMMIT DROP;
    TRUNCATE _sol_cells;
    INSERT INTO _sol_cells
    SELECT c.station, c.day, c.fact, c.code, c.fingerprint
    FROM jsonb_to_recordset(coalesce(payload -> 'cells', '[]'))
         AS c(station text, day date, fact text, code text, fingerprint text);
    SELECT count(*) INTO v_n_cells FROM _sol_cells;

    IF v_outcome = 'ok' THEN
        IF v_roster -> 'stations' IS NULL OR jsonb_typeof(v_roster -> 'stations') <> 'array'
           OR jsonb_array_length(v_roster -> 'stations') = 0 THEN
            RAISE EXCEPTION 'record_solution_check: an ok run needs the roster snapshot';
        END IF;
        IF (v_roster ->> 'reviewed_from') IS NULL THEN
            RAISE EXCEPTION 'record_solution_check: roster.reviewed_from is required';
        END IF;
        SELECT count(DISTINCT s) INTO v_n_stations
        FROM jsonb_array_elements_text(v_roster -> 'stations') s;
        v_n_days := v_to - v_from + 1;
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM _sol_cells WHERE station IS NULL OR day IS NULL OR fact IS NULL)
                THEN 'a cell lacks station, day or fact'
            WHEN EXISTS (SELECT 1 FROM _sol_cells
                         WHERE fact NOT IN ('valid', 'invalid', 'absent', 'unverifiable'))
                THEN 'a cell has an unknown fact'
            WHEN EXISTS (SELECT 1 FROM _sol_cells WHERE day < v_from OR day > v_to)
                THEN 'a cell lies outside the run window'
            WHEN EXISTS (SELECT 1 FROM _sol_cells c
                         WHERE NOT (v_roster -> 'stations') ? c.station)
                THEN 'a cell names a station outside the roster snapshot'
            WHEN EXISTS (SELECT 1 FROM _sol_cells GROUP BY station, day HAVING count(*) > 1)
                THEN 'duplicate (station, day) cells'
            WHEN EXISTS (SELECT 1 FROM _sol_cells
                         WHERE (fact = 'valid' AND fingerprint IS NULL)
                            OR (fingerprint IS NOT NULL AND fingerprint !~ '^[0-9a-f]{64}$'))
                THEN 'a valid cell needs a sha256 fingerprint, and any fingerprint must be one'
            WHEN v_n_cells <> v_n_stations * v_n_days
                THEN format('incomplete snapshot: %s cells for %s stations x %s days',
                            v_n_cells, v_n_stations, v_n_days)
        END INTO v_bad;
        IF v_bad IS NOT NULL THEN
            RAISE EXCEPTION 'record_solution_check: %', v_bad;
        END IF;
    ELSIF v_n_cells > 0 THEN
        RAISE EXCEPTION 'record_solution_check: a % run must carry no cells', v_outcome;
    END IF;

    -- ---- write
    INSERT INTO solution_check_run (run_id, stream, checked_at, window_from, window_to,
        outcome, error, source, roster_version, cell_count, files, unexpected_files,
        other_products, completion_id, payload_sha256)
    VALUES (v_run_id, v_stream, v_checked_at, v_from, v_to, v_outcome,
        payload ->> 'error', coalesce(payload ->> 'source', ''), v_version, v_n_cells,
        coalesce(payload -> 'files', '[]'), coalesce(payload -> 'unexpected_files', '[]'),
        coalesce(payload -> 'other_products', '{}'), payload ->> 'completion_id', v_sha);

    IF v_outcome <> 'ok' THEN
        RETURN 'recorded:' || v_outcome;
    END IF;

    -- roster: install this snapshot if it differs from the current one and this
    -- run is newer than the one that installed the current one (S2)
    IF NOT EXISTS (SELECT 1 FROM solution_roster r
                   WHERE r.stream = v_stream
                     AND (r.version = v_version OR r.installed_at >= v_checked_at)) THEN
        DELETE FROM solution_roster WHERE stream = v_stream;   -- cascades
        INSERT INTO solution_roster (stream, version, reviewed_from, reviewed_to,
            installed_at, installed_run_id)
        VALUES (v_stream, v_version, (v_roster ->> 'reviewed_from')::date,
            (v_roster ->> 'reviewed_to')::date, v_checked_at, v_run_id);
        INSERT INTO solution_roster_station (stream, station)
        SELECT DISTINCT v_stream, s FROM jsonb_array_elements_text(v_roster -> 'stations') s;
        INSERT INTO solution_roster_interval (stream, station, kind, day_from, day_to, reason)
        SELECT v_stream, i.station, i.kind, i.day_from, i.day_to, i.reason
        FROM jsonb_to_recordset(coalesce(v_roster -> 'intervals', '[]'))
             AS i(station text, kind text, day_from date, day_to date, reason text);
    END IF;

    -- transitions: a known fact changed (presence or validity). Computed
    -- against the OLD rows, so it must run before the upsert below.
    INSERT INTO solution_transition (stream, station, day, run_id, at,
        from_fact, from_code, to_fact, to_code)
    SELECT v_stream, c.station, c.day, v_run_id, v_checked_at, f.fact, f.code, c.fact, c.code
    FROM _sol_cells c
    JOIN solution_fact f ON f.stream = v_stream AND f.station = c.station AND f.day = c.day
    WHERE c.fact <> 'unverifiable'
      AND f.fact IS NOT NULL
      AND (f.fact, coalesce(f.code, '')) IS DISTINCT FROM (c.fact, coalesce(c.code, ''))
      AND greatest(f.last_verified_at, f.unverifiable_at) < v_checked_at;

    -- known facts
    INSERT INTO solution_fact AS f (stream, station, day, fact, code, fingerprint,
        first_seen_at, last_verified_at, last_revised_at, revision_count, last_run_id)
    SELECT v_stream, c.station, c.day, c.fact, c.code, c.fingerprint,
        CASE WHEN c.fact IN ('valid', 'invalid') THEN v_checked_at END,
        v_checked_at, NULL, 0, v_run_id
    FROM _sol_cells c
    WHERE c.fact <> 'unverifiable'
    ON CONFLICT (stream, station, day) DO UPDATE SET
        fact             = EXCLUDED.fact,
        code             = EXCLUDED.code,
        fingerprint      = EXCLUDED.fingerprint,
        first_seen_at    = coalesce(f.first_seen_at, EXCLUDED.first_seen_at),
        last_verified_at = EXCLUDED.last_verified_at,
        revision_count   = f.revision_count + CASE
            WHEN f.fact = 'valid' AND EXCLUDED.fact = 'valid'
                 AND f.fingerprint IS DISTINCT FROM EXCLUDED.fingerprint THEN 1 ELSE 0 END,
        last_revised_at  = CASE
            WHEN f.fact = 'valid' AND EXCLUDED.fact = 'valid'
                 AND f.fingerprint IS DISTINCT FROM EXCLUDED.fingerprint
            THEN EXCLUDED.last_verified_at ELSE f.last_revised_at END,
        last_run_id      = EXCLUDED.last_run_id
    -- an older snapshot (pgdev catching up, a delayed retry) never moves a fact back
    WHERE greatest(f.last_verified_at, f.unverifiable_at) < EXCLUDED.last_verified_at;

    -- unverifiable observations: recorded beside the last known fact
    INSERT INTO solution_fact AS f (stream, station, day, unverifiable_code,
        unverifiable_at, last_run_id)
    SELECT v_stream, c.station, c.day, c.code, v_checked_at, v_run_id
    FROM _sol_cells c
    WHERE c.fact = 'unverifiable'
    ON CONFLICT (stream, station, day) DO UPDATE SET
        unverifiable_code = EXCLUDED.unverifiable_code,
        unverifiable_at   = EXCLUDED.unverifiable_at,
        last_run_id       = EXCLUDED.last_run_id
    WHERE greatest(f.last_verified_at, f.unverifiable_at) < EXCLUDED.unverifiable_at;

    RETURN 'recorded:ok';
END
$fn$;

REVOKE ALL ON FUNCTION record_solution_check(jsonb) FROM PUBLIC;

-- ------------------------------------------------------------------ the grid

-- §3 in one place. LANGUAGE sql, STABLE, not STRICT, no SET, no SECURITY
-- DEFINER, one SELECT: the planner inlines it, so EXPLAIN (and the
-- `receivers health-query` gate) sees the real scans. The range is clamped to
-- 400 days because inlined SQL cannot RAISE.
CREATE OR REPLACE FUNCTION solution_day_grid(
    p_stream text, p_day_from date, p_day_to date, p_now timestamptz)
RETURNS TABLE (
    station text, day date, state text, reason text,
    fact text, code text, last_verified_at timestamptz)
LANGUAGE sql
STABLE
AS $fn$
WITH p AS (
    SELECT s.stream, s.expected_lag_days, s.deadline_time, s.freshness_ttl,
           s.window_days, (p_now AT TIME ZONE 'UTC')::date AS today,
           p_day_from AS d0, least(p_day_to, p_day_from + 399) AS d1
    FROM public.solution_stream s
    WHERE s.stream = p_stream
),
days AS (
    SELECT g::date AS day, p.*
    FROM p, generate_series(p.d0, p.d1, interval '1 day') g
),
run AS (            -- the latest check run (any outcome) covering each day
    SELECT DISTINCT ON (d.day) d.day, r.outcome, r.checked_at
    FROM days d
    JOIN public.solution_check_run r
      ON r.stream = d.stream AND d.day BETWEEN r.window_from AND r.window_to
     AND r.checked_at <= p_now
    ORDER BY d.day, r.checked_at DESC
),
comp AS (           -- the latest producer completion record covering each day
    SELECT DISTINCT ON (d.day) d.day, c.completion_id
    FROM days d
    JOIN public.solution_completion c
      ON c.stream = d.stream AND d.day BETWEEN c.day_from AND c.day_to
     AND c.completed_at <= p_now
    ORDER BY d.day, c.completed_at DESC
),
cells AS (
    SELECT st.station::text AS station, d.day, d.today, d.window_days, d.freshness_ttl,
           ((d.day + d.expected_lag_days) + d.deadline_time) AT TIME ZONE 'UTC' AS deadline,
           (d.day BETWEEN d.today - d.window_days AND d.today - 1) AS in_window,
           ro.reviewed_from, ro.reviewed_to,
           EXISTS (SELECT 1 FROM public.solution_roster_interval i
                   WHERE i.stream = d.stream AND i.station = st.station AND i.kind = 'excluded'
                     AND d.day >= i.day_from AND (i.day_to IS NULL OR d.day <= i.day_to)) AS excluded,
           EXISTS (SELECT 1 FROM public.solution_roster_interval i
                   WHERE i.stream = d.stream AND i.station = st.station AND i.kind = 'expected'
                     AND d.day >= i.day_from AND (i.day_to IS NULL OR d.day <= i.day_to)) AS expected,
           f.fact, f.code, f.last_verified_at, f.unverifiable_at,
           run.outcome AS run_outcome, run.checked_at AS run_at,
           comp.completion_id,
           ci.outcome AS item_outcome
    FROM days d
    JOIN public.solution_roster ro ON ro.stream = d.stream
    JOIN public.solution_roster_station st ON st.stream = d.stream
    LEFT JOIN public.solution_fact f
      ON f.stream = d.stream AND f.station = st.station AND f.day = d.day
    LEFT JOIN run ON run.day = d.day
    LEFT JOIN comp ON comp.day = d.day
    LEFT JOIN public.solution_completion_item ci
      ON ci.completion_id = comp.completion_id AND ci.station = st.station AND ci.day = d.day
)
SELECT c.station, c.day, s.state, s.reason, c.fact, c.code, c.last_verified_at
FROM cells c
CROSS JOIN LATERAL (
    SELECT
        -- "absent-like": observed absent, or not observable yet (today or later)
        (c.fact = 'absent' OR (c.fact IS NULL AND c.day >= c.today)) AS absentish
) a
CROSS JOIN LATERAL (
    -- ONE case, so a state and its reason can never disagree. Each branch is
    -- 'state|reason'; the first match wins (plan §3, rule numbers on the right).
    SELECT CASE
        WHEN NOT (c.day >= c.reviewed_from AND (c.reviewed_to IS NULL OR c.day <= c.reviewed_to))
            THEN 'unknown|roster_unreviewed'                                     -- 1
        WHEN c.excluded THEN 'not_expected|roster_excluded'                      -- 2
        WHEN NOT c.expected THEN 'not_expected|roster_not_expected'              -- 2b
        WHEN c.in_window AND c.run_outcome IS NULL THEN 'unknown|no_check_run'   -- 3
        WHEN c.in_window AND c.run_outcome = 'source_unavailable'
            THEN 'unknown|source_unavailable'
        WHEN c.in_window AND c.run_outcome <> 'ok' THEN 'unknown|check_failed'
        WHEN c.in_window AND c.run_at < p_now - c.freshness_ttl THEN 'unknown|check_stale'
        WHEN c.in_window AND c.unverifiable_at = c.run_at THEN 'unknown|file_unverifiable'
        WHEN c.in_window AND c.last_verified_at IS DISTINCT FROM c.run_at
            THEN 'unknown|not_in_last_check'
        WHEN c.fact = 'invalid' THEN 'invalid|' || coalesce(c.code, 'invalid')  -- 4
        WHEN c.fact = 'valid' AND c.in_window THEN 'available|verified'          -- 5
        WHEN c.fact = 'valid' THEN 'available|frozen'
        WHEN a.absentish AND p_now < c.deadline THEN 'pending|before_deadline'   -- 6
        -- the absence was seen BEFORE the deadline (e.g. a 07:00 check, file at
        -- 08:46): late is not established until a check after the deadline
        WHEN a.absentish AND (c.last_verified_at IS NULL OR c.last_verified_at < c.deadline)
            THEN 'pending|awaiting_recheck'                                      -- 6b
        WHEN a.absentish AND c.item_outcome = 'rejected' THEN 'overdue|producer_rejected'  -- 7
        WHEN a.absentish AND c.item_outcome = 'failed' THEN 'overdue|producer_failed'
        WHEN a.absentish AND c.completion_id IS NOT NULL
            THEN 'overdue|absent_after_completion'                               -- 8
        WHEN a.absentish THEN 'overdue|no_completion_record'                     -- 9
        WHEN c.unverifiable_at IS NOT NULL THEN 'unknown|never_verified'         -- 10
        ELSE 'unknown|never_checked'
    END AS sr
) v
CROSS JOIN LATERAL (
    SELECT split_part(v.sr, '|', 1) AS state, split_part(v.sr, '|', 2) AS reason
) s
$fn$;

-- Readers. Roles differ per host (rek-d01 has gpsops, pgdev has grafana_read),
-- so every grant is conditional.
DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gpsops') THEN
        GRANT EXECUTE ON FUNCTION record_solution_check(jsonb) TO gpsops;
        GRANT SELECT ON solution_stream, solution_roster, solution_roster_station,
            solution_roster_interval, solution_check_run, solution_fact,
            solution_transition, solution_completion, solution_completion_item TO gpsops;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_read') THEN
        GRANT SELECT ON solution_stream, solution_roster, solution_roster_station,
            solution_roster_interval, solution_check_run, solution_fact,
            solution_transition, solution_completion, solution_completion_item TO grafana_read;
    END IF;
END
$grants$;

INSERT INTO schema_migrations (migration_name)
VALUES ('077_daily_solutions')
ON CONFLICT DO NOTHING;

COMMIT;
