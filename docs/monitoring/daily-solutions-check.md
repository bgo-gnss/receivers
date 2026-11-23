# Daily POS solution check (slices 1–2)

`receivers solution-health check` reads a directory of daily PBO `.pos` files and an
explicit roster. For each roster station it reports one **fact** per day for the last
45 completed UTC days. It is read-only: it uses no database and writes nothing.

The plan is `docs/architecture/daily-solutions-implementation-plan.md` (on the
`docs/daily-solutions-plan` branch until that is merged). This slice reports facts
only. The six display states (available, pending, overdue, invalid, not expected,
unknown) come later, from one SQL function in slice 2; they are not computed here.

```bash
receivers solution-health check --source /mnt/data/gpsops/pos \
    --roster ~/.config/gpsconfig/daily_solutions.yaml            # human summary
receivers solution-health check --source DIR --roster FILE --json   # all facts
receivers solution-health check ... --now 2026-11-21T10:00:00Z      # as of an instant
```

Exit codes: `0` means the check ran, whatever it found. `2` means it could not run
(the roster is invalid, or the source directory can't be read). An exit code of 0
says nothing about completeness, so read the report.

## Roster

The schema is in `src/receivers/monitoring/solutions/roster.py`. The roster is
written by hand and never inferred from the files present: yang holds 385
`imo.24hr_itrf14` files, but only about 270 stations are maintained. For each day a
station is `unreviewed` (outside `reviewed`), `excluded`, `expected` or
`not_expected`. Who curates the real roster is owner gate G2.

## Facts and codes

| fact | meaning |
|---|---|
| `valid` | exactly one well-formed row for that date |
| `invalid` | a bad row, or a bad file (then every day in the window carries the file code) |
| `absent` | no row for the date, or no file |
| `unverifiable` | the file could not be read, or a row could not be dated |

File codes:
- `not_pbo`
- `not_ascii`
- `no_column_header`
- `frame_mismatch`: the header's `Reference Frame` is not the roster's `frame_label`, default `IGb14`
- `missing_station_id`: the station id is never taken from the file name
- `station_mismatch`
- `unreadable` → unverifiable
- `undatable_row` → unverifiable

Row codes:
- `duplicate_date`: applies even to identical duplicates
- `bad_column_count`
- `bad_epoch_time`
- `unexpected_epoch_time`: anything other than `115900`
- `nonfinite_xyz`: `*`, nan or inf in X/Y/Z
- `nonfinite_value`
- `bad_number`
- `mjd_mismatch`
- `session_mismatch`: anything other than `24hrs`

Fortran `*` overflow is tolerated outside X/Y/Z.

Files the check does not read are listed as unexpected files:
- a legacy alias (`imo.daily_igb14`), which is never read in place of the real name;
- a `.pos.gz` file;
- a station that is not in the roster;
- a lower-case name.

Sibling products in the same directory are only counted.

Each valid row carries a `fingerprint`: the sha256 of the row with whitespace
collapsed, so re-padding a row is not a revision.

## Drift guard

`tests/data/solution_contract/AFST.imo.24hr_itrf14.pos` is a byte-for-byte copy of
the file written by `geo_dataread.pos.write_pos`. That file is pinned by
`geo_dataread/tests/test_pos_monitor_contract.py`. If the geo_dataread test fails,
copy its new fixture here and run `tests/test_solution_contract.py`.

This is the production writer path. yang's `update-pos-yang.sh` runs
`gps-realign --frame igb14 --update`, and `realign.py` calls
`write_pos(frame="IGb14", soln="24hrs")`, then `merge_pos`. `merge_pos` keeps the
bytes of rows that were not re-made. The fixture is written by `write_pos` from three
positions. Two of them are AFST's X/Y/Z for 2026-11-19 and -20, taken from yang. The
third is made up, and the sigmas and correlations are simplified. So the fixture is
not a copy of a real file. The owner-approved real sample is still
gate G6.

## First real-data run (2026-11-21, a copy of yang's directory)

The roster was provisional (all 385 files, expected since 2000). Window 2026-10-07 ..
2026-11-20:
- all 385 files parsed `ok`;
- 11,622 station-days were `valid` and 5,703 `absent`;
- there were no invalid rows;
- 202 stations had all 45 days;
- 113 stations had none, which matches the roughly 115 files nobody maintains.

The run took 13 s.

## Recording runs and the grid (slice 2)

Migration `077_daily_solutions.sql` adds the store, `record_solution_check(payload)`
and `solution_day_grid(stream, day_from, day_to, now)`. The grid is the only place
the six states are decided; see plan §3 for the rules and reasons.

```bash
# record into a disposable local database (slice 2 refuses anything else)
receivers solution-health check --source DIR --roster FILE \
    --record-dsn "host=127.0.0.1 port=54329 dbname=sol_scratch"
psql ... -c "SELECT * FROM solution_day_grid('imo.24hr_itrf14', '2026-10-01', '2026-11-20', now())"
```

- **One write per destination.** Each `--record-dsn` is written in its own
  connection and its own transaction (owner decision G8). If one fails it is
  reported and the exit code is 3; the others are still written.
- **A source-unavailable run is recorded too.** That record is what turns the
  window's cells unknown.
- **All or nothing.** `record_solution_check` installs a complete snapshot (every
  roster station × every window day) or nothing.
- **Replays.** A replayed `run_id` is a no-op when the payload is identical, and an
  error when it differs.
- **Late arrivals.** An older snapshot that arrives late never moves a fact back.
- **Speed.** Measured on a laptop cluster with 385 stations × 45 days: 0.4 s to
  record, 0.01 s for the grid.

Tests: set `RECEIVERS_TEST_PG_DSN` to an admin DSN on a disposable local cluster.
Without it, `tests/test_solution_store.py` is skipped. Each test creates and drops
its own database, and the guard refuses non-local hosts and `gps_health`.

```bash
initdb -D /tmp/pg -A trust -U $USER
pg_ctl -D /tmp/pg -o "-p 54329 -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" start
RECEIVERS_TEST_PG_DSN="host=127.0.0.1 port=54329 user=$USER dbname=postgres" \
  PYTHONPATH=scripts/dev pytest tests/test_solution_store.py -p no_network_plugin
```

## Production on rek-d01 (slice 5)

```
yang-d01:/mnt/data/gpsops/pos ──NFS ro──> rek-d01:/mnt/yang_pos
gps-config-data daily_solutions.yaml ──sync timer──> ~gpsops/.config/gpsconfig/
gps-solution-health.timer (gpsops, hourly :25)
  └─ receivers solution-health check --production
       --record-dsn "host=/var/run/postgresql dbname=gps_health"
rek-d01 Grafana (:3000) ── "GPS Daily Solutions" board
```

- **The mount.** yang exports `/mnt/data/gpsops/pos` read-only to rek-d01
  (10.170.80.15) only; rek-d01 mounts it at `/mnt/yang_pos` with `soft`, so a
  stalled yang gives the check an error (recorded as `source_unavailable`, shown
  as "POS directory unreadable") instead of a hung run. Set up 2026-11-22.
- **The roster** lives in gps-config-data as `daily_solutions.yaml` and is
  deployed like `stations.cfg`. Stations without solutions are kept there
  commented out, by reason; enabling one is removing a `#`.
- **`--production`** lets `--record-dsn` name exactly `gps_health` on this machine
  or on pgdev.vedur.is (G8), one host each, and nothing else. Without it the store
  still writes only to local disposable databases.
- **The timer** (`deployment/systemd/gps-solution-health.{service,timer}`, a gpsops
  user unit) runs hourly at :25. 09:25 is the first run after the 09:15 deadline,
  so a missing day turns overdue then. `freshness_ttl` is 3 h: three missed runs
  turn the window's cells `unknown/check_stale`.
- **Migration 077** is applied as bgo, the owner. It grants `gpsops` EXECUTE on
  `record_solution_check` and SELECT on the tables (the board's datasource user).

pgdev, for grafana.vedur.is (2026-11-23): 077 is applied there as bgo, and the
service has a second `--record-dsn "host=pgdev.vedur.is dbname=gps_health user=bgo"`
(gpsops' `~/.pgpass` covers it). rek-d01 writes to pgdev as `mirror_user = bgo`; pgdev is a shared IMO
server with personal LDAP roles, so it has no `gpsops`, by design, and bgo owns
`record_solution_check` there, so no grant is needed.

**Local stations and agency (2026-11-23, migration 078).** yang's directory also
holds files for the global reference stations and for stations out of service. A
station is **local** when stations.cfg places it in Iceland (by `latitude`/`longitude`;
`is_in_iceland` only when the coordinates are missing) and it is not
inactive/discontinued. Local is a place, not an owner: HOFN and REYK (IGS) and BJAC
(not run by IMO) are local. A file of a station outside the roster is
`station_not_in_roster` only when the station is local; otherwise it is recorded as
`not_local` (runs before 078: `not_in_fleet`) and not counted (summary
`ignored_files`). `--stations-cfg PATH` names the file (default
`$GPS_CONFIG_PATH/stations.cfg`); `--no-ignore` counts everything again.

Every run also sends `station_info` (local + agency, the `station_owner`) for each
roster station; `record_solution_stations` writes it to `solution_station` in the same
transaction, never letting a late older run overwrite a newer fact. It is a table of
its own because the roster snapshot is replaced only when the roster file changes,
while these facts follow stations.cfg. The board's **Stations: local / all** option
(list, grid, map, counts and the unexpected-file table) and its **Agency** filter read
it. Apply 078 on a host before the code that sends `station_info` reaches it.
Migration 080 adds the **operator** (`station_operator`, who runs the station, apart
from the owner) for the board's **Operator** filter; 078's writer ignores the new key,
so only the board has to wait for 080.

**Completion inferred from the files (2026-11-23, migration 079).** Slice 4 planned a
completion record written by yang's processing. Until one exists, the check infers it:
the run for day D is done when at least half (`COMPLETE_SHARE`) of the stations expected
on D have a valid solution for D. Measured on 2026-11-23: a processed day sits at
89–91 %, an unprocessed one at 0 %, so the threshold is far from both. Each run sends
one record per contiguous range of processed days (`files:<run_id>:<day_from>`), so a
day the processing never ran stays uncovered and its gaps stay `late`. On a covered day,
a station still absent after the deadline is `overdue/absent_after_completion` (red),
not `overdue/no_completion_record` (orange). `record_solution_completions` writes them
into `solution_completion` in the same transaction; `solution_day_grid` is unchanged.
A late arrival still turns the cell green (a valid row comes before every completion
rule). A producer-written record can replace this later without a board change.

**Churn (G7, measured 2026-11-22).** Comparing two days of yang's files: on the
44 shared days, 43 were byte-identical for every station; only the newest day
(11-20) changed, for all 250 stations, because each day is re-estimated once in
the next run. One station-day arrived late (absent → valid).
