## Plan: Daily GNSS POS solution monitoring

**Status:** plan only, revision 2 (2026-11-20). Implementation and deployment are not
authorised. The contract is `.interrogate-gps-daily-solution-health.md` (design
approved). Revision 1 was reviewed by Fable on 2026-11-20 and found not ready. This
revision answers every blocking finding (B1–B5) and every should-fix (S1–S10), and
replaces revision 1's measured guesses with the measurements in §2.

Green means a readable, correctly identified station/day record for
`imo.24hr_itrf14` with finite XYZ at the source. It does not mean scientific quality,
and it does not mean a downstream consumer ingested it.

---

### 1. Verified foundations (source code)

The Fable review spot-checked each of these against the source:

- `geo_dataread/pos.py::read_pos()` falls back to the filename for station identity
  (`header.get("4-character ID", path.name[:4])`). `pos_path()` falls back to
  `LEGACY_IDS` (`imo.24hr_itrf14` → `imo.daily_igb14`). Neither is strict enough for
  this monitor.
- `realign.py::_safe()` swallows per-day failures, and `main()` still returns 0
  (`realign.py:190`, `:374`). An exit code of zero does not certify a batch.
- `merge_pos()` drops rows and can unlink a whole file (`pos.py:694-731`). Removals
  are real and must be detected.
- The producer wrapper lives in `gps_plot-yang/deployment/yang/`, a worktree on branch
  `feat/yang-deployment`, not on `main`. `yang-plots.sh` keeps plotting after a POS
  failure (line 23).
- `DatabaseConnectionFactory`'s mirror leg is best-effort: `_DualCursor.execute` and
  `commit` swallow failures. Production Grafana reads pgdev.
- Migration 076 is the latest; 077 is free (reconfirm before slice 2).
- `receivers/pyproject.toml` has no `geo_dataread` dependency.
- Config reaches rek-d01 through `deployment/server/sync-config.sh`, a hard-coded
  `SYNC_FILES` list, plus the install.sh Phase 5 list. **A new file is not delivered
  automatically** (§5, S1).

### 2. Measured on yang-d01 (read-only, 2026-11-20)

| Fact | Evidence | Consequence |
|---|---|---|
| POS dir `/mnt/data/gpsops/pos` is **local ext4** on yang (`/dev/mapper/data--vg-data`); `/etc/exports` is empty | `findmnt`, `/etc/exports` | Not NFS today. Gate G1 is open: an export, or another read path, is needed before slice 5 |
| **Lag is one day.** The 07:45 run logs `D-1 incomplete (… not finished on okada; out until it is)`; the 08:45 run has D-1 complete. Seen on 11-16, 11-17 and 11-18 | `journalctl --user -u gpsplot-run-update` | `expected_lag_days = 1` is measured, not assumed. Matches the 08:30–09:00 arrival and the 09:15 lateness |
| **About seven publications a day:** 02:00, 07:45, 08:45, 09:45, 10:45, 12:00, 16:45, plus ad-hoc runs (e.g. 20:50 on 11-16) | timers, journal | There is no single morning batch. A completion record has to be per run |
| Each run re-makes **39–40 days** and logs "270 stations updated" even when the content is unchanged | journal | mtime is useless. Fingerprints must be content hashes, and revision churn must be measured (G7) |
| **D-1 completeness flaps:** 2026-11-16 was complete at 08:46 and 09:45, then logged *incomplete* again at 10:45 on 11-17 | journal | A day can legitimately go available → absent → available. The 45-day recheck must record these transitions and not alarm on them |
| 385 `*.imo.24hr_itrf14.pos` files, but only 270 stations are updated per run | `ls`, journal | About 115 files are not maintained. The roster must never be inferred from files present (the design already says so; this is the evidence) |
| Sibling products sit in the same directory: `imo.24hr_itrf08`, `imo.24hr_itrf20`, `imo.08hr_itrf20` | `ls` | Substituting one product for another is a concrete risk. Match the exact name only (S9) |
| The producer's own log label says `realign igb14`; files and `days.*` CSV say `imo.24hr_itrf14` | journal | Confirm the product id with the owner in G6 |

### 3. Cell state function (B1)

There is exactly one authority: the SQL function
`solution_day_grid(stream, day_from, day_to, now)`. `now` is **always a parameter**;
the body never calls `now()`. The function returns, for each (station, day), one of the
**six design states** plus a `reason` code. There is no Python copy of this logic
(S3).

Inputs:
- `roster(station, day)`: covered and expected, covered and excluded, or not covered.
- `fact(station, day)`: valid, invalid or absent, with `last_verified_at`.
- `completion(day)`: the latest completion record whose coverage includes the day,
  and that day's outcome in it.
- `check_ok(day)`: whether the latest check covering the day succeeded and is within
  `freshness_ttl`.
- `deadline(d) = d + expected_lag_days + 09:15 UTC`.

Rules, applied in order (the first match wins):

| # | Condition | State | reason |
|---|---|---|---|
| 1 | Not in roster-reviewed coverage | **unknown** | `roster_unreviewed` |
| 2 | Covered, explicitly excluded | **not expected** | `roster_excluded` |
| 3 | Day inside the 45-day window and `check_ok` false (failed, stale, unreadable, mount lost, hash mismatch against the completion inventory) | **unknown** | `check_failed` / `check_stale` / `source_unavailable` / `inventory_mismatch` |
| 4 | Fact invalid (malformed, duplicate, non-finite XYZ, wrong station/frame/session/product) | **invalid** | the validation code |
| 5 | Fact valid | **available** | `verified` (in window) / `frozen` (older than 45 days, last verified at `last_verified_at`) |
| 6 | Absent, `now < deadline(d)` | **pending** | `before_deadline` |
| 7 | Absent, deadline passed, the completion record covering the day lists it as `rejected` or `failed` | **overdue** | `producer_rejected` / `producer_failed` |
| 8 | Absent, deadline passed, a completion record covers the day and the station is missing | **overdue** | `absent_after_completion` |
| 9 | Absent, deadline passed, no completion record covers the day | **overdue** | `no_completion_record` |
| 10 | Day older than 45 days, never observed (before the first run) | **unknown** | `never_checked` |

Consequences:
- A **valid row is positive evidence** without a completion record (rule 5 comes
  before every completion-dependent rule). Slices before the producer record can
  still show green.
- An **unfinished delivery** after 09:15 is `overdue/no_completion_record`, which is
  late and not confirmed absent. Only `absent_after_completion` is a confirmed
  absence. Grafana colours the two reasons differently.
- A **stale check** turns only the cells inside the 45-day window into unknown. Cells
  outside the window keep their last-known fact as `frozen`, drawn as a distinct
  shade. History is never rewritten by newer results.
- **Retained-by-guard** (`--min-keep`) days keep their old row, so they appear as
  available or `frozen`. The completion record notes them, and they are listed in the
  exception summary.
- **Files for stations outside the roster** are not cells. They are listed in the
  exception summary as `unexpected_file`, together with any other-product files found
  in the configured location.

**Owner gate G3:** confirm that `overdue` covers both late delivery (rule 9) and
confirmed absence (rule 8), separated by reason, rather than introducing a seventh
state.

### 4. Owner gates → blocked slices (B5)

| Gate | Question | Status 2026-11-20 | Blocks | Evidence artefact |
|---|---|---|---|---|
| G1 | Read path from rek-d01 to the POS directory (an NFS export from yang, or another approved path) and gpsops readability | **Open**: local ext4, not exported | slice 5 | `findmnt` + a test read as gpsops on rek-d01 |
| G2 | Roster source, curator, and historical expectation intervals | Open | slices 2, 3 (real roster); slice 1 runs on a synthetic roster | reviewed `daily_solutions.yaml` |
| G3 | Overdue semantics (§3) | Open | slice 2 | owner note in this doc |
| G4 | Lag and deadline | **Measured** D-1 / 09:15 (§2); owner confirms | slice 2 | §2 table |
| G5 | Canonical producer branch and every writer entry point (timers, ad-hoc runs, laptop) | Partial: the timers are known; the branch is `feat/yang-deployment` | slice 4 | list in the slice-4 MR |
| G6 | Owner-approved real `imo.24hr_itrf14` sample; product id (`itrf14` vs the `igb14` label) | Open | slice 1 against real data (synthetic first) | fixture under `tests/data/` |
| G7 | Revision churn over 40 re-made days × 270 stations | Open | slice 2 schema retention | slice-1 report run on two consecutive days |
| G8 | pgdev write path (§5, S5) | Open | slice 2 | decision recorded here |

### 5. Slices (B4): each one mergeable on its own, default-off

**Slice 0: gate resolution (read-only).** Close G1–G8 and record the evidence above.
No code.

**Slice 1: pure checker and CLI dry-run, no database.** This is the smallest useful
slice. It answers "which station-days in the last 45 days are absent or invalid"
against a directory and a roster file given on the command line.
- Code:
  - `src/receivers/monitoring/solutions/pos_reader.py::inspect_pos()` does strict
    parsing:
    - exact `<ID4>.imo.24hr_itrf14.pos` only; reject `LEGACY_IDS`
      (`imo.daily_igb14`), `.pos.gz` and sibling frames unless configured;
    - the header station ID is required and must match the filename;
    - pin the frame label, the `24hrs` session, 25 columns and `11:59:00`;
    - `*` overflow is tolerated only in non-XYZ fields;
    - duplicate dates, including identical duplicates, are invalid;
    - an undatable malformed row makes that file's coverage unverifiable.
  - `window.py`: the 45 completed UTC days `[today−45, today−1]`, by date and never
    by row count; files may be sparse or unsorted.
  - `fingerprint`: sha256 of each normalised row, plus a sha256 of the file.
  - `receivers solution-health check --source DIR --roster FILE --json` produces
    facts only, not states.
- Drift guard (S8): a contract fixture produced by `geo_dataread.pos.write_pos` and
  pinned byte-for-byte in a `geo_dataread` test. The receivers parser is deliberately
  separate because it is stricter.
- Tests: `tests/test_solution_pos_reader.py`, `tests/test_solution_window.py` (UTC
  boundaries, leap day, exactly 45 dates, sparse, unsorted, duplicates, NaN/Inf/XYZ
  overflow, wrong station/frame/product, the legacy alias, `.gz`, a missing middle
  day). Run under `-p no_network_plugin`.
- G7 evidence: run it on two consecutive days on a copy of the real directory and
  count rows whose fingerprint changed.

**Slice 2: schema and state function (disposable database only).**
- `migrations/077_daily_solutions.sql`, with rollback and verify companions:
  - `solution_fact(stream, station, day)` PK: validity, fingerprint,
    `first_seen_at`, `last_verified_at`, `last_revised_at`, `revision_count`.
  - `solution_transition`: presence and validity changes only, not one row per
    revision (S4). Retention is set from the G7 measurement.
  - `solution_check_run`: application-generated UUID and timestamps, coverage,
    outcome, error, roster version, completion-record id.
  - `solution_roster_interval`: written **inside the same snapshot** (S2). A roster
    edit re-evaluates history retroactively, and the roster version is stored on
    each check run.
  - `solution_completion` (populated from slice 4).
- `record_solution_check(payload jsonb)`: one call installs a complete validated
  snapshot or nothing. Every timestamp and UUID comes from the application (S5), so
  the primary and pgdev cannot diverge on generated values.
- `solution_day_grid(stream, day_from, day_to, now)`: §3, with bounded date
  arguments, and it must pass the `receivers health-query` EXPLAIN gate. It does not
  join `block_*` tables.
- Owner, grants and EXECUTE are stated in the migration. Migration 073's lesson:
  apply as the owner (bgo), not gpsops.
- Tests (S7):
  - the store takes a DSN from `RECEIVERS_TEST_PG_DSN` only, refuses any host that
    isn't localhost, and never resolves `database.cfg` or the config singleton;
  - tests are skip-marked when no local Postgres is available;
  - pytest drives the SQL function with an injected `now`, one case per §3 rule;
  - a wiring mutation check: revert the CLI → `record_solution_check` call site and
    assert a test goes red.
- G8, the pgdev write path. Recommended option: write to pgdev as an **explicit
  second destination**, with its own result check and its own `solution_check_run`
  row, instead of relying on `_DualCursor`'s silent mirror. If the owner prefers the
  factory mirror, add these tables to the `gps-mirror-parity` check. Checker reads
  use `connection(single_host=True)`.

**Slice 3: Grafana dashboard.** `docs/grafana/gps_daily_solutions_dashboard.json`,
fed by `solution_day_grid` only:
- a station × UTC-day grid using Grafana's table grouping-to-matrix transform, with
  numeric state mapping and distinct shades for `frozen` and for the two overdue
  reasons;
- an exception summary: counts by state and reason, `unexpected_file`, retained
  days, last successful check, latest error;
- station links to the existing receiver, raw and RINEX dashboards, as context only.

Verify on the installed Grafana version with the real roster size and 45 columns.
Unknown, stale and frozen cells must never render as plain green. Check-time history
is not part of this slice.

**Slice 4: producer completion record (other repos: geo_dataread and gps_plot-yang).**
Replaces revision 1's generation and reflink design (B3).
- Each run writes one completion JSON to a temporary file and atomically renames it
  to `completion/<run_id>.json`. A `latest` copy is updated by a rename as well.
- Fields:
  - stream, run_id, started_at, completed_at;
  - per-day outcomes: processed / retained / rejected / failed / unattempted;
  - an inventory: file → size + sha256.
- Written only after the POS stage finishes: not after plots, and not after the outer
  service.
- Each POS file is already replaced atomically (`tmp.replace`). The reader hashes
  what it reads; a mismatch with the inventory is `inventory_mismatch` → unknown, and
  the reader retries next cycle.
- Generation copies are a **fallback only**, if measurement shows hash mismatches
  are frequent.
- `realign` gains an optional `--report` flag. Default behaviour is unchanged.
- Tests in `geo_dataread/tests/test_realign.py`:
  - a `_safe()` failure appears as `failed`;
  - a crash mid-run leaves no new record;
  - concurrent runs are serialised by the existing flock.
- G5 must be closed first. A producer move to a GAMIT server keeps the same record
  format.

**Slice 5: transport, timer, config propagation, rollout.**
- Transport: `source.py::FilesystemSolutionSource`, read-only, over the G1 path.
  Preflight checks:
  - the mount identity is correct (an empty directory is not proof of a mount);
  - a stream sentinel is present;
  - gpsops can read.

  SSH stays a deferred fallback, used only if G1 refuses NFS.
- Scheduling (S6): a standalone systemd user timer `gps-solution-health.timer`,
  following the precedent of `gps-health-freshness-alert.timer`. It is not a
  scheduler `_*_job`, and never `ScheduledTask` or `TaskFactory`.
  - Every 5 minutes it stats `completion/latest`; it runs a full 45-day check when
    that changes, and in any case every 30 minutes, so deadlines still progress when
    the producer is silent.
  - A `oneshot` unit plus `flock` prevents overlap.
  - `TimeoutStartSec`, plus `timeout(1)` around the run, because a hard-mounted
    `stat` hangs inside Python.
- Config (S1): a plain `daily_solutions.yaml` in gps-config-data, not a `.template`.
  It is added to `sync-config.sh` `SYNC_FILES`, to install.sh Phase 5, and to
  `deploy.py`'s laptop env. This is a receivers code change and needs a deploy.
- Rollout, each step separately approved:
  1. disabled merge;
  2. a disposable end-to-end demonstration;
  3. migration hand-applied on rek-d01 (as bgo) and on pgdev;
  4. check long-running jobs before the deploy, since `install.sh` stops the
     scheduler;
  5. timer enabled in shadow mode;
  6. dashboard published.

  The first rollback step is disabling the timer. Tables are never dropped
  automatically.

### 6. Acceptance (Definition of Done)

One test or demonstration covers each §3 rule. In addition:
- Real POS samples (G6) show available rows. Controlled cases show:
  - a missing station-day → pending, then overdue at 09:15 the next day;
  - an unreadable file → unknown;
  - a failed check → unknown inside the window, while older cells stay `frozen`;
  - a removed row → a transition recorded, and the cell overdue after the deadline;
  - a sibling-product or legacy file → never counted; listed as `unexpected_file`;
  - a lost mirror write on pgdev → that leg's freshness does not advance.
- A gap older than the newest results stays visible.

### 7. Out of scope

Scientific quality thresholds, downstream-ingestion certification, more than one
stream, notifications, automatic recovery, reprocessing and repair, and changes to
reconcile or intake.

### 8. Docs

The operator and runbook doc goes in `docs/monitoring/daily-solutions.md` (there is
no `docs/operations/`).
