-- latency_report.sql — download & RINEX-conversion latency / consistency report
--
-- Analyses per-file timestamps already recorded in file_tracking (created_at is
-- effectively the download/conversion time; file_date + file_hour give the data
-- period). No new collection needed — this runs over history that accumulates on
-- its own. Use it to find WHAT is delayed and WHERE, and to track whether tuning
-- actually moves p90/p99 over time.
--
-- Usage (localhost gps_health on rek-d01; gssencmode=disable avoids the slow KDC):
--   psql 'host=localhost dbname=gps_health user=gpsops gssencmode=disable' \
--        -P pager=off -v days=14 -f sql/latency_report.sql
--   (default window is 14 days if -v days=... is omitted)
--
-- Latency definitions:
--   download   = created_at - period_close   (period_close = end of the data hour/day)
--   conversion = rinex.created_at - raw.created_at   (same sid/date/hour)
-- "live" = download latency in [0, 360] min; rows above that are backfilled /
-- recovered later and are counted separately (late_or_backfilled) rather than
-- polluting the live percentiles.

\if :{?days}
\else
  \set days 14
\endif
\echo ==================================================================
\echo  Latency report — last :days days   (localhost gps_health)
\echo ==================================================================

-- Download latency per file, joined to receiver type, for the window.
CREATE TEMP VIEW ft_lat AS
SELECT ft.sid, ft.session_type, ft.file_date, ft.file_hour, s.receiver_type,
       EXTRACT(EPOCH FROM (ft.created_at -
         (ft.file_date + CASE WHEN ft.file_hour IS NULL THEN interval '1 day'
                              ELSE (ft.file_hour + 1) * interval '1 hour' END)))/60 AS m
FROM file_tracking ft
LEFT JOIN stations s USING (sid)
WHERE ft.session_type IN ('15s_24hr','1Hz_1hr','status_1hr')
  AND ft.status IN ('downloaded','archived')
  AND ft.file_date >= CURRENT_DATE - (:days)::int
  AND ft.created_at IS NOT NULL;

\echo
\echo --- [1] DOWNLOAD latency per session (min after period close) ---
SELECT session_type,
       count(*) FILTER (WHERE m BETWEEN 0 AND 360) AS live_files,
       round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 360))::numeric,1) AS p50,
       round((percentile_cont(0.9)  WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 360))::numeric,1) AS p90,
       round((percentile_cont(0.99) WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 360))::numeric,1) AS p99,
       count(*) FILTER (WHERE m > 360) AS late_or_backfilled
FROM ft_lat GROUP BY 1 ORDER BY 1;

\echo
\echo --- [2] CONVERSION latency raw->rinex per session (min) ---
WITH conv AS (
  SELECT r.session_type,
         EXTRACT(EPOCH FROM (x.created_at - r.created_at))/60 AS m
  FROM file_tracking r
  JOIN file_tracking x
    ON x.sid = r.sid AND x.file_date = r.file_date
   AND x.file_hour IS NOT DISTINCT FROM r.file_hour
   AND x.session_type = r.session_type || '_rinex'
  WHERE r.session_type IN ('15s_24hr','1Hz_1hr')
    AND r.file_date >= CURRENT_DATE - (:days)::int
    AND r.created_at IS NOT NULL AND x.created_at IS NOT NULL
)
SELECT session_type, count(*) AS pairs,
       round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 1440))::numeric,1) AS p50,
       round((percentile_cont(0.9)  WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 1440))::numeric,1) AS p90,
       round((percentile_cont(0.99) WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 1440))::numeric,1) AS p99
FROM conv GROUP BY 1 ORDER BY 1;

\echo
\echo --- [3] 1Hz download latency by HOUR-OF-DAY (spot midnight-batch collision) ---
SELECT file_hour AS hr, count(*) AS n,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY m))::numeric,1) AS p50,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY m))::numeric,1) AS p90
FROM ft_lat WHERE session_type='1Hz_1hr' AND m BETWEEN 0 AND 360
GROUP BY 1 ORDER BY 1;

\echo
\echo --- [4] Download latency by RECEIVER TYPE x session ---
SELECT coalesce(receiver_type,'?') AS rx, session_type, count(*) AS n,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY m))::numeric,1) AS p50,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY m))::numeric,1) AS p90
FROM ft_lat WHERE m BETWEEN 0 AND 360
GROUP BY 1,2 ORDER BY 2, p90 DESC;

\echo
\echo --- [5] WORST 15 stations by 1Hz download p90 (>=50 files) ---
SELECT sid, coalesce(receiver_type,'?') AS rx, count(*) AS n,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY m))::numeric,1) AS p50,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY m))::numeric,1) AS p90
FROM ft_lat WHERE session_type='1Hz_1hr' AND m BETWEEN 0 AND 360
GROUP BY 1,2 HAVING count(*)>=50 ORDER BY p90 DESC LIMIT 15;

\echo
\echo --- [6] WEEKLY TREND: 1Hz download p50/p90 (is tuning helping?) ---
SELECT date_trunc('week', file_date)::date AS week, session_type,
       count(*) FILTER (WHERE m BETWEEN 0 AND 360) AS live,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 360))::numeric,1) AS p50,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY m) FILTER (WHERE m BETWEEN 0 AND 360))::numeric,1) AS p90
FROM ft_lat GROUP BY 1,2 ORDER BY 2,1;
