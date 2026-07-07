-- Migration 059: is_file_missing — make terminal-absence skip ADVISORY (opt-in)
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2). SAFETY fix over mig 056.
--
-- The problem: mig 056 made is_file_missing() return TRUE on a terminal
-- file_absence, and that result already drives the download paths to SKIP. But
-- terminal absence is earned by repeated reachable-but-404/550 over >= 3 days —
-- which a PERSISTENT CONFIG ERROR also satisfies. The known failure mode is the
-- all-files-404 Trimble (a wrong receiver_base_path makes EVERY file 404): after
-- 3 days the whole station goes terminal and is skipped FOREVER, even once the
-- config is fixed. Silent data loss. The time-spanned criterion does not help —
-- a config error is genuinely multi-day.
--
-- The guard that makes terminal absence trustworthy is the "served-gate" (only
-- confirm absence when the station demonstrably served >= 1 OTHER file, proving
-- it is per-file absence, not a blanket failure). That gate lives in the download
-- path and is a later slice. Until it exists, terminal absence must be
-- ADVISORY: the ledger keeps building (record_file_absence unchanged), but a
-- terminal row does NOT cause a skip.
--
-- Mechanism: add p_use_terminal (DEFAULT FALSE). The 4-arg callers resolve to
-- this via the default and get the mig-046 behaviour (24 h TTL only) — terminal
-- is recorded but not honoured. When the served-gate ships and is validated,
-- callers pass p_use_terminal => TRUE (driven by config). DROP the old 4-arg
-- signature first so the new default-bearing overload is the one that resolves.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/059_is_file_missing_advisory_terminal.sql

BEGIN;

-- Drop the mig-056 4-arg signature so the 4-arg call sites resolve to the new
-- 5-arg overload (via its default) rather than to a stale terminal-honouring one.
DROP FUNCTION IF EXISTS is_file_missing(VARCHAR, VARCHAR, DATE, SMALLINT);

CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR,
    p_session_type VARCHAR,
    p_date DATE,
    p_hour SMALLINT DEFAULT NULL,
    p_use_terminal BOOLEAN DEFAULT FALSE
) RETURNS BOOLEAN AS $$
BEGIN
    -- (1) terminal absence on the receiver → permanent skip — ONLY when the
    -- caller opts in (p_use_terminal). Off by default until the served-gate
    -- guarantees a terminal row is genuine per-file absence, not a config error.
    IF p_use_terminal AND EXISTS (
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
    -- (2) transient: a 404/550 within the last 24 h (mig 046) — retry after that.
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
    'Skip-download check. Default (p_use_terminal=FALSE): 24 h transient TTL only '
    '(mig 046) — terminal file_absence is RECORDED but ADVISORY (not skipped) '
    'until the served-gate protects against config-error poisoning. Opt in with '
    'p_use_terminal => TRUE once that gate ships. NULL-safe on file_hour.';

INSERT INTO schema_migrations (migration_name)
VALUES ('059_is_file_missing_advisory_terminal')
ON CONFLICT DO NOTHING;

COMMIT;
