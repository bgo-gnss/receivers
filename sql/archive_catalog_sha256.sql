-- archive_catalog: sha256 + file info lookups
-- ---------------------------------------------------------------------------
-- Run against gps_health (production = pgdev.vedur.is). Read-only.
--   receivers health-query -f sql/archive_catalog_sha256.sql --host pgdev.vedur.is
--   psql -h pgdev.vedur.is gps_health -f sql/archive_catalog_sha256.sql
--
-- GOTCHAS (bite every time):
--   * canonical_key is LOWERCASE and includes the .26d extension form
--     ('rhof1720.26d'), NOT the on-disk 'RHOF1720.26D'. Filter case-insensitively.
--   * file_date is NULL for session_type='15s_24hr'/file_category='rinex' rows
--     (a parser gap — parse_archive_path doesn't populate it for that pattern).
--     Do NOT filter these by file_date; use canonical_key / indexed_at instead.
--   * content_sha256 is over DECOMPRESSED content (a .d.Z hashes == its .d twin),
--     so a header rewrite changes the hash.

-- (1) One station's RINEX rows: hash + size + path + freshness -----------------
\set station 'RHOF'
\set session '15s_24hr'

SELECT canonical_key,
       compression,
       file_size,
       content_sha256,
       indexed_at,
       last_verified_at,
       file_path
FROM archive_catalog
WHERE station = :'station'                         -- e.g. 'RHOF'
  AND session_type = :'session'                    -- e.g. '15s_24hr'
  AND file_category = 'rinex'
ORDER BY canonical_key;

-- (2) Cross-check archive vs source hash (detects local<->archive divergence) --
--     file_tracking = the source/local copy; archive_catalog = the archive copy.
--     A mismatch means the two storages disagree (e.g. after a --fix-headers
--     --push that corrected the archive but not source_root). NOTE the join is
--     inert while archive_catalog.file_date is NULL (see gotcha above).
SELECT c.station,
       c.canonical_key,
       left(c.content_sha256, 12) AS archive_sha,
       left(t.content_sha256, 12) AS source_sha,
       (c.content_sha256 IS DISTINCT FROM t.content_sha256) AS divergent
FROM archive_catalog c
LEFT JOIN file_tracking t
       ON t.sid = c.station
      AND t.session_type = c.session_type || '_rinex'   -- ft suffixes rinex
      AND t.file_date = c.file_date                       -- NULL-inert today
WHERE c.station = :'station'
  AND c.session_type = :'session'
  AND c.file_category = 'rinex'
ORDER BY c.canonical_key;

-- (3) Coverage + verification summary per storage/category/session ------------
SELECT storage_location, file_category, session_type,
       count(*)                                            AS rows,
       count(*) FILTER (WHERE content_sha256 IS NOT NULL)  AS with_sha,
       count(*) FILTER (WHERE last_verified_at IS NOT NULL) AS verified,
       max(indexed_at)                                     AS newest_index
FROM archive_catalog
GROUP BY storage_location, file_category, session_type
ORDER BY storage_location, file_category, session_type;

