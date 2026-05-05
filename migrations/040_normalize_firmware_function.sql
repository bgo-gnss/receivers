-- Migration 040: normalize_firmware_version() SQL function
--
-- Mirrors the Python _normalize_firmware_version() in field_manifest.py so the
-- Grafana dashboard can compare firmware versions across format families:
--
--   NP/SP prefix (Trimble cfg notation):
--     "NP 4.81 / SP 4.81" → "4.81"
--
--   Two-digit minor+patch compact form (both Trimble and Septentrio):
--     "4.81" → "4.8.1"    "5.22" → "5.2.2"    "4.60" → "4.6.0"
--
--   Three-part form already canonical:
--     "5.5.0", "1.3-2", "5.61" → unchanged (lowercased)
--
-- The function is IMMUTABLE so it can be used safely in view definitions
-- and is inlined by the planner.

BEGIN;

CREATE OR REPLACE FUNCTION normalize_firmware_version(v text)
RETURNS text
LANGUAGE sql
IMMUTABLE
CALLED ON NULL INPUT
AS $$
SELECT
    CASE WHEN v IS NULL THEN NULL
    ELSE (
        WITH
        -- Step 1: strip "NP X / SP X" → X
        stripped(s) AS (
            SELECT CASE
                WHEN v ~* '^NP\s+[\d]'
                    THEN (regexp_match(v, '^NP\s+([\d][^\s/]*)', 'i'))[1]
                ELSE v
            END
        ),
        -- Step 2: expand two-digit minor "X.YZ" → "X.Y.Z"
        -- only when the second part is exactly two ASCII digits
        parts(p) AS (
            SELECT string_to_array(s, '.') FROM stripped
        )
        SELECT CASE
            WHEN array_length(p, 1) = 2
                 AND p[2] ~ '^\d{2}$'
                 AND p[1] ~ '^\d+$'
            THEN lower(p[1] || '.' || left(p[2], 1) || '.' || right(p[2], 1))
            ELSE lower((SELECT s FROM stripped))
        END
        FROM parts
    )
    END
$$;

COMMENT ON FUNCTION normalize_firmware_version(text) IS
    'Normalise firmware version strings for cross-format comparison. '
    'Strips Trimble NP/SP prefix and expands two-digit minor+patch notation. '
    'Mirrors Python _normalize_firmware_version() in cfg/field_manifest.py.';

INSERT INTO schema_migrations (migration_name) VALUES ('040_normalize_firmware_function');

COMMIT;
