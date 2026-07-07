# Unified File Index — Design & Migration Plan

**Status:** REVIEWED (adversarially critiqued — 6 blockers + majors resolved, see §11) · **Owner:** bgo · **Started:** 2026-07-07
**Scope:** `receivers` package + `gps_health` schema. Local-first now; rek-d01 + pgdev + Grafana rollout is a gated later phase.

> Unify the two parallel file indexes the receivers stack maintains today (sha256 `archive_catalog`
> for the IMO/ananas archive + md5 `gnss-europe.rinex_file` for the EPOS portal) into **one
> multi-server, sha256-based file index in `gps_health`**, and build a queryable "what's present /
> what's missing per source" layer so backfill never has to `ls` a directory again.

---

## 0. Goal (from the request)

1. **Index + checksum** files across many file servers — *rawdata* (ananas long-term archive),
   *local archive* (rek-d01 ring buffer: raw + rinex), *epos-portal* (disseminated subset) —
   **extensible to more servers**.
2. **Query the DB to drive backfill** (expected − present), never directory listings.
3. **Terminal "missing on receiver"** indicator so the scheduler stops re-fetching a file that
   genuinely does not exist upstream.
4. **Missing-file worklist per source** (missing on receiver / missing RINEX / missing on
   epos-portal / missing on any server).
5. **All checksums on sha256.**
6. Path to **migrate existing data** + **push to `gps_health` on rek-d01 + Grafana**.

---

## 1. Decisions locked

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **epos-portal truth = our own push events** → `storage_location='epos_portal'`. | `gnss-europe.rinex_file` is empty externally; our push is the only reliable record. |
| D2 | **Checksums = dual-hash.** Unified index is sha256; **md5 kept only on the EPOS `rinex_file`** (external EPOS/M3G contract). | Standardise internally without breaking portal ingest. |
| D3 | **`gps_health` is the single source of truth**; `gnss-europe.rinex_file` = derived export. But see D2-corollary in §3.4: md5 is **not derivable from sha256** — the epos_portal catalog row stores the md5s at push so the export is truly catalog-derived. | "merge **into** gps_health"; one query surface. |
| D4 | **`gnss_europe_local` added to nvim `connections.json`** (localhost, passwordless). | Done this session. |
| D5 | **Generalize `archive_catalog`** (durable, sha256-keyed, one-row-per-location) — do not invent a parallel table. | It already lives off the `file_tracking` TRUNCATE cascade. |
| **D6** | **Dual-host = ONE mechanism, not two.** Scheduler-on-rek-d01 uses the **existing best-effort mirror** (`database.cfg mirror_host=pgdev`) — made *non-silent for catalog writes* + a cross-host reconcile net. Laptop maintenance (reindex/archive-rm) uses the **explicit `catalog_hosts` fan-out** (no mirror on laptops). **Never stack a fan-out on top of the mirror** (double-writes pgdev). | Critique L3: the mirror already dual-writes; a naïve fan-out writes pgdev 2–3×. |
| **D7** | **Cross-location matching keys on a naming-independent LOGICAL key** `(station, session_type, file_category, file_date, file_hour)`, not `content_sha256`/`canonical_key`. | Critique L4: EPOS R3-long vs archive R2-short share neither hash nor basename — content-key joins can't correlate local↔portal. |

**Confirmed at handoff (2026-07-07):** (a) expected-set bounds **sync `data_start`/`data_end` from
TOS** (§3.6, accepted new coupling); (b) *"update repos with missing status"* = the `file_absence` DB
ledger **plus a generated known-missing report/manifest** (CSV/JSON) for out-of-DB visibility — **no
gps-config-data writes**; (c) scope = **full M1–M4**.

---

## 2. Current state — two parallel indexes

**Index A — IMO archive (sha256).** `archive_catalog` (migration 050): one row per
`(storage_location, session_type, file_category, canonical_key)`, single writer `upsert_catalog_row()`
(`catalog.py:26`); `content_sha256` over **decompressed** content (magic-byte detect, `.Z` via
`gzip -dc` — *truncation not caught for `.Z`*); soft link to `file_tracking.id` (no FK). `canonical_key`
folds compression + case but **not Hatanaka** (`.d`≠`.o`). Today only `storage_location='imo_archive'`
populated, at archive-sync push. **No `file_hour` column; `file_date` NULL for 15s/rinex** (verified: 126/126).
`content_sha256` sparsely filled (9/5886). **No compressed-byte hash exists.**

**Dual-host reality (corrected):** on rek-d01, `database.cfg mirror_host=pgdev` makes
`get_connection()` return a **best-effort `_DualConnection`** (`health/database_factory.py:243`, *"failures
logged but never break the primary"* :21) that mirrors *every* write — catalog upserts **and DDL via
`migrator.py:42`** — to pgdev. The reindex path *also* has an explicit `catalog_hosts` fan-out
(`reindex_files_multi`). So writes are **already dual**, silently and best-effort (with a mirror-failure
cooldown) — not single-host.

**Index B — EPOS portal (md5).** `dissemination/rinex_index.py` computes `md5checksum` (compressed
bytes) + `md5uncompressed` (gzip- *and* CRX2RNX-decompressed obs) and `job.py:52` writes a `rinex_file`
row per pushed file — on a **separate server** (`[epos_db]`, behind pgbouncer, deliberately isolated from
the gps_health mirror). Upsert is SELECT-then-INSERT keyed on `(name, relative_path)` with **no DB
UNIQUE** and `id = MAX(id)+1` (`epos_db.py:174`), safe only under a process-local `_INDEX_LOCK`. md5 is an
external contract (docstring: *"exactly as EPOS expects"*). `dissemination/__init__.py:11`: *"Phase 1 keeps
the existing EPOS gnss-europe DB (md5 index); a later phase migrates the index to `content_sha256` +
`gps_health`."* — **this plan is that phase.** Only one hardcoded data center (`IMO`,
`data.epos-iceland.is`).

**Operational tracker (ephemeral).** `file_tracking` — identity `(sid, session_type, file_date,
file_hour)` (two partial UNIQUE indexes). Status: **downloaded, archived, missing, suspect, removed**.
`status='missing'` set **only by PolaRx5** (FTP 550); Trimble/Leica/NetRS never mark it. `is_file_missing()`
= 24 h TTL, **no terminal state**. `import_checksum` = health-import JSON digest (populated for
`status_1hr`) — **not a file hash, out of scope**. `file_locations` = **dead code** (writer
`FormatResolver.record_file_location` has no caller). Backfill = `backfill_progress` cursor, refilled by
gap detection that **globs archive directories** (`ArchiveFileChecker`) — the `ls` we replace.

**Storage registry (thin).** `storage_location(location_id, name, base_path,
location_type CHECK IN ('local','nfs','server'), is_primary, enabled)` — **no host/protocol/retention**;
seeded `ON CONFLICT DO NOTHING`. Only `local_archive` locally.

### 2.1 What gpseurope contributes

`data_center` (host/protocol/root_path) → enrich `storage_location`. `rinex_file` (dual-hash, status,
relative_path) → shape for catalog rows (sha256 internally). `quality_file`/`other_files`/`sinex_files`
→ future `file_category` values.

---

## 3. Target architecture

Durable tables in `gps_health`, all off the `file_tracking` truncate cascade.

### 3.1 `storage_location` → file-server registry (extensible)

Additive columns: `host`, `protocol` (`local|nfs-ro|ssh|rsync|ftp|https` — widen/drop the current
`location_type` CHECK), `root_path`, `is_permanent BOOL`. Seed via **`ON CONFLICT DO UPDATE`** (current
`DO NOTHING` never backfills the new columns).

Retention is **per (location, session)** in a child table `storage_retention` — but it is a **derived
view of `scheduler.yaml [local_prune]`, not a second editable copy** (critique L1/L3-minor: two copies
drift; a re-fetch then gets silently skipped). The seeder either projects `[local_prune]` or asserts
equality at deploy and fails loudly. Model the **receiver buffer depth** explicitly per receiver type
(not "a placeholder"). Note prune can shorten the *effective* floor under low disk
(`emergency_retention_days`, `prune.py`) — worklists must treat the seeded value as the **normal** floor
and tolerate a shorter physical one (finding: catalog retraction on prune, §3.2, makes this safe).

Seed locations: `receiver` (logical, per-station upstream), `local_raw`, `local_rinex`, `imo_archive`
(permanent), `epos_portal`. **Adding a server = seed rows** — *provided it shares the fleet naming/
compression*; a server with a different convention (e.g. EPOS R3-long) additionally needs a naming→
logical-key mapping (§3.2/D7), not just a seed row. State this honestly; do not over-claim "no code".

### 3.2 Unified durable catalog (generalize `archive_catalog`)

Populate **every location**, all categories. Two identities per row:

- **Physical identity** (integrity/dedup, per naming convention): `canonical_key` + `content_sha256`
  (decompressed) + new **`compressed_sha256`** (on-disk bytes).
- **Logical identity** (D7, cross-location join grain): `(station, session_type, file_category,
  file_date, file_hour)`. **Add `file_hour SMALLINT NULL`** (critique L2 blocker — hourly worklists
  cannot name the missing hour without it; parse from `canonical_key`/path, populate on every write +
  in the §7 backfill). **Additive-first (M1 de-risk):** keep the existing `canonical_key` UNIQUE
  (`canonical_key` already distinguishes files within a location) and add a **non-unique logical
  index** on the tuple for the cross-location joins + hourly worklists. Only swap the UNIQUE to the
  logical tuple later if a proven duplicate-collision case demands it — swapping the UNIQUE on a live,
  durable integrity ledger with legacy NULL-station/date rows is delicate and unnecessary for M1.

**Lifecycle by location (critique L1 blocker — the big one):**
- `imo_archive` rows are **append-only** (permanent archive).
- `local_raw`/`local_rinex` rows are **lifecycle-managed** — **retracted on prune**: when `run_prune`
  unlinks a file, it must call the existing `remove_catalog_rows(conn, 'local_raw'/'local_rinex', [rel])`
  (`remove.py:196`) in the same pass (file first, then row). Otherwise a pruned file stays "present@local"
  forever → `missing_at_location` under-fetches, `missing_rinex` re-rinexes a gone file, `file_coverage`
  over-counts. `prune.py` is currently **absent from the code-change list** — it is now a §6 deliverable.

**`content_sha256` integrity caveat:** `.Z` truncation is not caught by the decompressor; it is only
caught by *comparison* against a known-good hash. For `.Z` products (EPOS `.d.Z`), integrity = cross-ref
comparison or size+md5, not the raw hash alone.

### 3.3 Durable absence ledger (the "don't re-fetch" knowledge)

New table `file_absence(source_location, sid, session_type, file_date, file_hour, confirmations,
first_confirmed_at, last_confirmed_at, terminal BOOL)`, PK on the identity tuple, **independent of
`file_tracking`**. A slot is confirmed `absent` only on a **reachable-but-no-file** result (FTP 550 /
HTTP 404 on a live connection) — never a connection error.

Hard rules (critique L2 blockers/majors):
- **Never record a slot whose period has not fully elapsed (UTC).** A 1Hz station probed at 10:00 UTC
  must not mark hours 11–23 absent — that would cross the terminal threshold and *permanently skip the
  real file when it lands* (silent data loss). Ceiling logic lives in §3.5.
- **NULL-safe joins:** `file_hour` is NULL for daily 15s; every anti-join uses `IS NOT DISTINCT FROM`
  (or branches NULL/day vs hourly like `is_file_missing()` migration 046) — a plain equi-join makes a
  terminal 15s day re-fetch forever.
- **Coverage across receiver types:** the reachable-but-404/550 → absence hook exists only in PolaRx5;
  it is a **hard M2 deliverable** to add it to Trimble/Leica/NetRS, else terminal-absence covers only
  Septentrio and ask #3 is unmet for the rest of the fleet.

`is_file_missing()` is reworked to consult `file_absence` (terminal → permanent skip) **plus** the 24 h
TTL (transient), NULL-safe.

**Known-missing report/manifest (the "update repos" ask, D-handoff):** beyond the DB ledger, a
`receivers missing`/absence **export** emits a versioned known-missing report/manifest (CSV/JSON) of
terminal-absent files per station/session — for out-of-DB visibility and review. It **does not** write
to `gps-config-data`; the DB is authoritative, the manifest is a generated artifact.

### 3.4 Checksums → sha256 (dual-hash for EPOS) — corrected counterpart mapping

Critique L4 blocker — the naïve "content_sha256 ↔ md5uncompressed" mapping is **wrong**:

| EPOS md5 | over | Correct sha256 counterpart |
|---|---|---|
| `md5checksum` | on-disk (compressed/published) bytes | **`compressed_sha256`** (same bytes) ✅ valid algorithm swap |
| `md5uncompressed` | gzip- **and** CRX2RNX-decompressed obs | **none of the existing hashes** — `content_sha256` folds *compression only, not Hatanaka* ❌ |

Consequences: (a) **md5 cannot be derived from any stored sha256** (one-way). So D3's "derived from
catalog" is only true if the **md5s are stored on the `epos_portal` catalog row at push time**
(computed by `rinex_md5s()` while the file is reachable). Do this — store `md5checksum` +
`md5uncompressed` as attributes on epos_portal rows — so the `rinex_file` export is genuinely
catalog-derived and works for aged-off files. (b) Do **not** claim `content_sha256` corresponds to
`md5uncompressed` anywhere. If a sha256 that *does* correspond is ever wanted, add a third
un-Hatanaka'd content hash — not needed for D1–D3.

Internal spine everywhere = **sha256** (`content_sha256` decompressed + `compressed_sha256` on-disk),
via `utils/content_hash.py`. Lazy hash-fill (integrity checker) fills both, newest-first. **EPOS portal
integrity** is the md5 contract verified by the portal ingest / push log — **not** the sha256 spine
(the epos_portal row's sha256 is the *local* hash at push, proving nothing about portal bytes).

### 3.5 Differential / missing-file model — reworked (query, don't `ls`)

`worklist = expected − present − absent`, but *rigorously bounded* (critique L2: this section was "not
sound" — the fixes below are mandatory):

**Expected set — per station, per session it actually runs, bounded both ends:**
```
date ∈ [ max(data_start, source_floor) , least(coalesce(data_end, last_complete_period), last_complete_period) ]
hour ∈ full 0..23 for hourly sessions, NULL for daily      -- one row per (sid, session, date, hour)
```
- **`data_start` / `data_end`** per station come from **TOS** (§3.6) — gps_health.stations has *no*
  install/discontinue date, so an unbounded `generate_series` would flag a station discontinued in 2024
  as missing every day forever, and a station with empty `config_valid_from` degenerates. Blocker.
- **Ceiling = last COMPLETE period in UTC** (yesterday for daily; last fully-elapsed hour for hourly).
  Never "today" (would mark not-yet-produced hours absent → data loss). Postgres session `TimeZone=UTC`
  (Iceland is UTC year-round — the only tz exposure).
- **Station → session map:** restrict the cross-product to sessions a station produces (NetRS = 15s
  only). Without it, BLEI flags 100% of 1Hz/status missing forever. Derive from `receiver_type`
  capability / scheduler session config.
- **Per-source, per-worklist floor** (a single floor cannot serve all):
  - `missing_on_receiver` floored by **receiver buffer depth** (not the local-ring value — else a
    100-day-old pruned 15s routes to a receiver that discarded it → 404 churn).
  - **Subtract `present@ANY-permanent-location` (imo_archive) from `missing_on_receiver`** and route
    those to a **`needs_repull_from_archive`** worklist instead — a file aged past the local ring but
    safe in the archive must never be re-fetched from the receiver.
  - `imo_archive`/permanent locations get a **hard history cap** per session (never generate hourly
    1Hz expected across 30 years — tens of millions of rows).
- **Split the RINEX floor:** `rinex_config_valid_from` bounds **only the rinex worklist** (below it,
  headers need TOS — annotate "needs TOS", never auto). Using it for the **raw** worklist *hides real
  raw gaps* below it (under-report). Floor raw by receiver retention / `data_start`.
- **Lifecycle filtering per view:** exclude `health_check='passive'` and
  `station_status IN ('inactive','discontinued')` from `missing_on_receiver`; decide case-by-case for
  `file_coverage` / `missing_at_location(imo_archive)`.

**Views (all project `(sid, session_type, file_date, file_hour)`, NULL-safe joins):**
`missing_on_receiver`, `missing_rinex` (raw present but rinex absent), `missing_on_epos_portal`
(joined on the **logical key** D7, not content hash), `missing_at_location(:loc)`,
`needs_repull_from_archive`, `file_coverage`.

**Performance (critique L3 — 2026-05-27-class risk):** the coverage/differential must **not** be a live
Grafana view over a multi-million-row `generate_series` anti-join on pgdev. **Materialize `file_coverage`**
(scheduled refresh, indexed); Grafana reads the materialized table; ad-hoc queries go through an
EXPLAIN/`statement_timeout` gate like `health-query`.

### 3.6 Station lifecycle date sync from TOS

Add `data_start` / `data_end` (and keep `station_status`/`health_check`) to `gps_health.stations`,
synced from TOS `date_start`/`date_end` by the existing cfg/TOS sync path. Required by §3.5's expected
bounds. This is the one **new cross-system dependency** the plan introduces.

### 3.7 Reconcilers (forward index is best-effort → needs two safety nets)

1. **Backward archive↔catalog reconciler** (critique L1 major): the forward write is best-effort
   (crash-after-transfer → on-archive-but-uncataloged; `rsync --ignore-existing` never revisits). Build
   a scheduled pass that lists the archive tree (find/rsync listing) and catalogs the residue. Until it
   exists **and** the §7 history backfill completes, gate any consumer that treats "absent@imo_archive"
   as actionable (dissemination re-push, `missing_rinex`) behind a **catalog-complete-for-window** flag,
   or worklists drown in false positives.
2. **Cross-host catalog divergence detector** (critique L3 major): `verify.py` only checks one host.
   Add a periodic reconcile comparing per-`(storage_location, session, category)` row counts + a
   `content_sha256` aggregate between hosts; alert (Icinga/log) on mismatch. The mirror's silent
   best-effort failures + up-to-cooldown gaps make this the only way a split is caught before a Grafana
   worklist goes wrong.

---

## 4. Requirements traceability

| Ask (§0) | Delivered by | Hard dependency |
|---|---|---|
| Index+checksum all servers, extensible | §3.1 registry + §3.2 catalog (all locations, file_hour) | naming-map for non-fleet conventions (D7) |
| Query DB to backfill, no `ls` | §3.5 views; retire `backfill_progress`/glob | §3.6 TOS dates; materialized coverage |
| Terminal "missing on receiver" | §3.3 `file_absence` + reworked `is_file_missing()` | non-Septentrio absence hook (M2) |
| Missing-file worklist per source | §3.5 `missing_*` + `needs_repull_from_archive` | file_hour, per-source floors, session-map |
| All checksums sha256 | §3.4 sha256 spine; md5 only on EPOS export (stored on epos_portal row) | — |
| Migrate + push rek-d01 + Grafana | §7 + §8 | dual-host mechanism (D6); rek-d01 listen_addresses |

---

## 5. Schema changes (migrations 054+)

Each with `_rollback.sql`; applied **local first**, then per-host explicitly (§8, not via the mirror).

- **054** — `storage_location` enrichment (host/protocol/root_path/is_permanent, widen CHECK, seed
  `DO UPDATE`) + `storage_retention` (derived from `scheduler.yaml`) + receiver-depth seeds.
- **055** — `archive_catalog`: add **`file_hour`**, **`compressed_sha256`**, **`md5checksum`/
  `md5uncompressed`** (epos_portal attrs); **keep the `canonical_key` UNIQUE, add a non-unique logical
  index** (additive; §3.2); **populate `file_hour` + `file_date` for all patterns and backfill** —
  sequenced *after* the verify.py cross-check fix ships (§8), or existing rows flip to false
  `local_divergent`.
- **056** — `file_absence` + reworked `is_file_missing()` (terminal + TTL, NULL-safe).
- **057** — `stations.data_start`/`data_end` + TOS sync wiring (§3.6).
- **058** — differential objects: **materialized** `file_coverage` + `missing_on_receiver`,
  `missing_rinex`, `missing_on_epos_portal`, `missing_at_location`, `needs_repull_from_archive`
  (bounded, NULL-safe, session-map-aware).
- **059** — deprecate `backfill_progress` (stop writing; cut readers to views).
- EPOS side (separate server, migration in the gnss-europe/GLASS schema): **`UNIQUE(name,
  relative_path)` + IDENTITY/sequence on `rinex_file.id`** before any second writer (critique L4 major).

## 6. Code changes by module

- `db/seeder.py` + `config/receivers_config.py` — richer `storage_location` + `storage_retention`
  (single-source from `scheduler.yaml`) + receiver-depth map.
- `archive/catalog.py`/`engine.py`/`reindex.py` — write **all** locations; populate `file_hour` +
  `compressed_sha256`; logical-key upsert.
- **`archive/prune.py`** — retract `local_*` catalog rows on unlink (`remove_catalog_rows`).
- **`archive/` (new)** — backward archive↔catalog reconciler + cross-host divergence detector (§3.7).
- `health/file_tracker.py` — write/increment `file_absence` on reachable-but-absent; drop reliance on
  `file_locations`.
- **download paths (Trimble/Leica/NetRS)** — add the reachable-but-404/550 → absence hook.
- `dissemination/job.py`/`rinex_index.py` — write the **unified catalog** (epos_portal, sha256 + stored
  md5s); derive the `rinex_file` export from it via an exporter that respects the new UNIQUE + an
  **advisory lock** (not the process-local `_INDEX_LOCK`) since a backfill exporter + live sweep now
  both write.
- `scheduling/backfill.py`/`gap_scheduler.py`/`archive_reconciler.py` — consume the differential views;
  drop directory globbing + the cursor.
- `db/migrator.py` / rollout tooling — **per-host apply with the mirror disabled** + a schema-parity
  check (§8).
- `cli/` — `receivers missing --location <loc> [--session] [--json]` worklist verb; a known-missing
  **report/manifest export** (CSV/JSON, D-handoff); reuse `archive-verify`.

## 7. Data migration / backfill

1. Apply 054–059 on **local** `gps_health` (apply+rollback+reapply). Seed registry + retention.
2. Backfill catalog from existing state: `file_tracking` (archived/downloaded) → `local_raw`/
   `local_rinex` rows (with `file_hour`, soft-link id); keep `imo_archive` rows, populate
   `file_hour`/`file_date`. Lazy sha256 + `compressed_sha256` fill (throttled, newest-first).
3. EPOS: import historical `gnss-europe.rinex_file` (or reactive_state) → `epos_portal` rows, carrying
   the md5s; sha256 filled where the file is reachable, else NULL.
4. Absence bootstrap: seed `file_absence` from terminal `missing` rows past TTL.
5. **Backward reconciler + history coverage backfill** (§3.7 + the ~39k pre-catalog 1Hz + 30 yr) run
   **throttled on rek-d01** (fast path to ananas), never the laptop. Riskiest step for pgdev — run
   off-peak, bounded batches, EXPLAIN-gated.

## 8. Rollout — local → rek-d01 + pgdev → Grafana (gated)

1. **Local** (now): migrations + code on the laptop; exercise every view + verb end-to-end.
2. **Prereq (user-owned):** rek-d01 postgres `listen_addresses=127.0.0.1` blocks **laptop→rek-d01**
   writes only (the on-host scheduler reaches pgdev via the mirror). Until opened, laptop maintenance
   uses `catalog_hosts=pgdev` only — and on open, **backfill/reconcile rek-d01-local from pgdev** for
   the interim maintenance edits before enabling rek-d01 in `catalog_hosts`.
3. **Migrations to rek-d01 + pgdev — apply per-host EXPLICITLY with the mirror OFF** (critique L3
   blocker): applying through the `_DualConnection` silently swallows a pgdev DDL failure yet records it
   applied on both → permanent undetected divergence. Use `Migrator(host_override=…)` per host with the
   mirror disabled, then a **schema-parity check** (`information_schema` + `schema_migrations` on both).
4. **Ship the verify.py cross-check fix BEFORE migration 055** (critique L3 major): 055 populates
   `file_date`, activating the previously-inert cross-check; if the old code is still live it emits a
   burst of false `local_divergent`. Deploy + restart scheduler, confirm live, *then* 055.
5. **Dual-host writes (D6):** rely on the mirror for the scheduler; make catalog writes non-silent
   (capture mirror failure → queue/retry) + the cross-host detector (§3.7). Do **not** add a fan-out on
   top of the mirror.
6. **Grafana:** point panels at the **materialized** `file_coverage` (not a live `generate_series`
   view); build the planned Data Delivery dashboard (coverage + missing-per-source). Edit JSON locally →
   `grafana_sync` push → commit.

## 9. Risks & open items

- **`file_date`/`file_hour` population (055)** is load-bearing and must follow the verify fix (§8.4).
- **Retention single-source**: `storage_retention` must derive from `scheduler.yaml [local_prune]` or
  drift causes silent skip/churn.
- **Mirror is best-effort + has a failure cooldown** — catalog correctness needs the non-silent path +
  cross-host detector, or the two DBs split undetected (happened before).
- **EPOS export cross-server**: exporter must not route through `DatabaseConnectionFactory` (would
  mutate `POSTGRES_HOST` / trigger the mirror); needs the new UNIQUE + advisory lock.
- **ananas 1Hz policy (bgo)**: archive fills in ~5–6 weeks at the current 1Hz rate; the differential
  surfaces this starkly — a storage decision, not a blocker.
- **TOS date sync (§3.6)** is a new dependency; without it the expected set cannot be bounded.

## 10. Milestones

- **M0 (this session):** connections.json; this plan; grounding + adversarial critique (§11).
- **M1 — Registry + catalog generalization (local): ✅ DONE (2026-07-07, local).** 054–055 + seed +
  all-location writes + `file_hour` + `compressed_sha256` + **prune retraction**. Exit met: every
  locally-known file has a catalog row that is *retracted on prune*.
  - **054** (`054_storage_location_registry.sql`) — enrich `storage_location`
    (protocol/host/root_path/is_permanent, CHECK dropped) + `storage_retention` (derived from
    `scheduler.yaml [local_prune]` by the seeder) + `receiver_buffer_depth` (conservative seeds).
    Seeder (`receivers_config.seed_storage_locations`) widened (Python type-guard relaxed, `DO
    UPDATE`, well-known registry rows `local_raw`/`local_rinex`/`imo_archive`/`epos_portal`/`receiver`).
  - **055** (`055_archive_catalog_file_index.sql`) — additive `file_hour`/`compressed_sha256`/
    `md5checksum`/`md5uncompressed` + non-unique logical index. **DDL-only: mutates NO existing
    `imo_archive` rows** (keeps verify.py cross-check inert — the §8.4 gate stays a later step).
    `utils/content_hash.compressed_sha256()` added; `upsert_catalog_row` carries `file_hour` +
    `compressed_sha256` (COALESCE-on-update so lazy-fill isn't wiped); engine.py/reindex.py pass
    `file_hour`.
  - **Local writer** — forward hook in `file_tracker.mark_file_archived` →
    `archive.catalog.catalog_local_file` (hashes DEFERRED, best-effort, isolated txn) + backfill
    `archive.catalog.backfill_local_catalog` behind the `receivers catalog-backfill-local` CLI verb
    (pages by id, copies existing `file_tracking.content_sha256`, `verify_exists` skips phantom rows).
    Local run: 253 real rows cataloged of 5886 tracked (5633 phantom file_tracking rows correctly
    skipped).
  - **Prune retraction** — `run_prune` calls `remove_catalog_rows('local_raw'|'local_rinex', rels)`
    after unlink; the imo_archive deletion GATE is untouched (verified: gate row survives, local row
    retracts). `PruneStats.catalog_retracted` added.
  - Verified: 054/055 apply→rollback→reapply clean; ruff/black clean; no new mypy errors; 103 archive
    tests pass (1 pre-existing env failure `test_raw_immutable_rinex_updates`, compress(1) LZW).
  - **Not committed to git yet** (working tree only; migrations applied to local gps_health).
  - **Left for later (M4 gate):** imo_archive `file_date` back-population + the verify.py cross-check
    fix must ship together (§8.4); rek-d01/pgdev per-host apply.
- **M2 — Absence + differential (local):** 056–058 + TOS date sync + reworked `is_file_missing()` +
  **non-Septentrio absence hook** + bounded/materialized views + `receivers missing`. Exit: worklists
  query-only, no `ls`, no false-missing on the failure cases in §11.
- **M3 — EPOS convergence:** unified-catalog writes at push (sha256 + stored md5s); `rinex_file` becomes
  a derived export (new UNIQUE + advisory lock). Exit: one write path.
- **M4 — Migrate + prod rollout:** backfill (§7) + backward + cross-host reconcilers + per-host migrate
  + Grafana (§8), gated on the rek-d01 listen-address prereq.

---

## 11. Adversarial critique — resolution log

Four-lens adversarial review (2026-07-07). 6 blockers + 12 majors; all folded in above.

| Sev | Lens | Finding | Resolved in |
|---|---|---|---|
| BLOCKER | EPOS | `content_sha256` ≠ `md5uncompressed`; md5 not derivable from sha256 | §3.4 (store md5 on epos_portal row; only `compressed_sha256↔md5checksum` valid) |
| BLOCKER | Retention | catalog has no `file_hour` → can't name missing hour | §3.2, 055 |
| BLOCKER | Retention | no per-station install/discontinue dates → unbounded series | §3.6 TOS `data_start`/`data_end` |
| BLOCKER | Retention | undefined ceiling → not-yet-produced hours marked terminal absent (data loss) | §3.3/§3.5 last-complete-period UTC |
| BLOCKER | Dual-host | migrating through the mirror silently drops pgdev DDL, marks applied | §8.3 per-host apply, mirror off, parity check |
| BLOCKER | Dual-host | forward writer already mirrors; fan-out double-writes; real gap = cooldown | D6, §3.7 non-silent + detector |
| MAJOR | Durability | prune never retracts `local_*` rows → stale present forever | §3.2 retraction via `remove_catalog_rows` |
| MAJOR | Durability | no backward archive↔catalog reconciler → permanent holes | §3.7 |
| MAJOR | EPOS | derived exporter races no-UNIQUE `MAX(id)+1` `rinex_file` | §5 EPOS UNIQUE+IDENTITY, §6 advisory lock |
| MAJOR | EPOS | "one seed row" breaks under R3-long vs R2-short naming | D7 logical key |
| MAJOR | Dual-host | no cross-host divergence detection | §3.7 detector |
| MAJOR | Dual-host | 055 activates verify cross-check before code fix | §8.4 ordering |
| MAJOR | Dual-host | live coverage view = 2026-05-27-class heavy scan on pgdev | §3.5 materialized + gate |
| MAJOR | Retention | NULL-hour equi-join never matches → terminal 15s re-fetched | §3.3 `IS NOT DISTINCT FROM` |
| MAJOR | Retention | single floor can't serve receiver/ring/archive; never subtracts present@archive | §3.5 per-source floors + `needs_repull_from_archive` |
| MAJOR | Retention | no station→session map → non-PolaRX5 flagged 100% missing | §3.5 session-map |
| MAJOR | Retention | `rinex_config_valid_from` as raw floor hides real raw gaps | §3.5 split rinex/raw floor |
| MINOR/NIT | mixed | retention drift; passive stations; `.Z` truncation; non-Septentrio absence; interim rek-d01 catch-up | §3.1/§3.5/§3.4/§3.3/§8.2 |
