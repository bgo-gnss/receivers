# EPOS RINEX Dissemination — Implementation Plan (phase 1)

Tracer-bullet implementation plan for running EPOS dissemination entirely from
`receivers`, replacing the legacy `epos-gnss` swarm container. Aligned design
concept: `.interrogate-epos-dissemination.md` (repo root). Port analysis:
`epos-gnss-port-analysis.md`. Design context:
[[1781867391-data-dissemination-archive-sync-design]].

**Headline:** the riskiest end-to-end path is the **R2→R3 long-name convert chain**
(the doc puts conversion *in* phase-1 scope, overriding the port-analysis "ship `.Z`
verbatim" option). The tracer bullet must prove convert, not a verbatim copy. New
module: `src/receivers/dissemination/` (sibling to `src/receivers/archive/`).

---

## Ticket spine (thin → thick)

### T1 — Tracer bullet: archived R2 → R3 long name → staging push (gated off)
One hardcoded (station, date): archived `15s_24hr/rinex/*.D.Z` (Hatanaka R2) →
CRX2RNX → gfzrnx (R2→R3 + long name + header-from-TOS) → valid R3 → rsync to a
**staging dest**, `active:false`. No DB, no QC gate, no station filter.
- **New:** `dissemination/__init__.py`, `config.py` (`DisseminationTarget` extends the
  `SyncTarget` shape + `include_filter`, `convert_with`, `convert_cache_dir`,
  `country_code`), `convert.py` (the convert chain + cache), `engine.py`
  (`EposDisseminate` for one (station,date)), `cli/epos_disseminate.py`
  (`--station --date --dest-override --dry-run --force`).
- **Reuse:** rsync primitive (`archive/engine.py` `_rsync`/`_build_rsync_cmd`),
  `gtimes.rinex3_filename` / `rinex/rinex_namer.RinexNamer`, `converter_base`
  tool-resolution + subprocess, gps-tools `CRX2RNX`/`gfzrnx`, the gating pattern.
- **Net-new:** R2→R3 gfzrnx orchestration as a standalone (converter_base is
  raw→rinex oriented), cache write/lookup.
- **Cache key (settled, see below):** `hash(source content_sha256 + TOS-metadata
  fingerprint for that epoch)`, NOT source hash alone.
- **Verify:** `epos-disseminate --station REYK --date 2026-06-01 --dest-override
  /tmp/epos_stage [--dry-run] --force`; staged name == `reyk00ISL_R_..._01D_15S_MO.rnx`,
  R3 opens with `gfzrnx -finp`.
- **Risk:** gfzrnx R2→R3 + header-from-TOS flags; `.D.Z` Hatanaka round-trip casing.
- Deps: none.

### T2 — Header-QC gate ✅ BUILT (2026-06-28)
Verify R3 header vs TOS/site log before any push; mismatch → flag + skip.
- Built: `dissemination/qc_gate.py` (`qc_check`/`select_session`/`QCVerdict`),
  reuses `tostools.rinex.reader.read_rinex_header`/`extract_header_info` +
  `validator.compare_rinex_to_tos`. Wired into `engine.run_one` via an injectable
  `session_provider` (gate skipped when None). **Blocking fields = marker /
  antenna_height / coordinates only** — receiver/antenna are emitted unconditionally
  by the comparator (formatting noise, handled by set-header), so excluded.
  set-header-from-TOS in convert is deferred to T3/later (still a stub). Tests added.
- **New:** `dissemination/qc_gate.py`; wire into engine between convert and push.
- **Reuse:** `tostools.rinex.validator.compare_rinex_to_tos` + `generate_qc_report`,
  `tostools.rinex.reader.read_rinex_header`, `gps_rinex.compare_tos_to_rinex`.
- **Verify:** corrupt-header file → gate fails, nothing staged; clean → passes.
- Deps: T1. Risk: `compare_rinex_to_tos` input contract (rinex_dict + session shape).

### T3 — TOS include-filter (`in_network_epos=true` + min-requirements) ✅ BUILT (2026-06-28)
Target self-selects EPOS stations from TOS instead of a CLI arg.
- Built: `dissemination/tos_access.py` — `epos_stations`/`epos_markers`
  (in_network_epos + min-attrs, ported `getAttributeValue`/`checkMinimumRequirements`),
  bulk `list_geophysical_stations` via legacy bodyless GET routed through
  `canonical_tos_url` (**fixes dead `/tos/v1/`**), and `make_session_provider`
  (TOSClient.get_complete_station_metadata → select_session, marker injected,
  fail-safe None on TOS error). CLI gains `--list-stations` + `--no-qc`; the QC gate
  now runs by default with the live provider. Tests use mocked station lists/clients.
- **NOT yet verified against live TOS** (mocked in tests) — that's the post-T3
  testing pass. set-header-from-TOS (`correct_rinex_from_tos`) into convert + the
  convert-cache TOS fingerprint still to wire.
- **New:** `dissemination/station_filter.py` (port `getAttributeValue` +
  `checkMinimumRequirements` from legacy `tosToDatabase.py`).
- **Reuse:** `TOSClient.search_stations(domains="geophysical")` — **fixes the legacy
  dead `/tos/v1/` URLs** (now `/tos/internal`).
- **Verify:** `epos-disseminate --list-stations` count vs known TOS query.
- Deps: T1. Risk: `search_stations` body shape vs legacy GET; `attributes` array present.

### T4 — EPOS `rinex_file` md5 indexer ✅ BUILT (2026-06-28)
Built `dissemination/rinex_index.py` (`index_rinex_file`, `rinex_md5s`): upserts
data_center(IMO)/file_type/data_center_structure + `rinex_file` keyed on
(name, relative_path) via SELECT-then-write (no UNIQUE in schema); re-index stamps
`revision_date` (the retro re-push hook). `md5checksum`=file bytes, `md5uncompressed`
=decompressed+un-Hatanaka content (equal for our plain `.rnx`; differ once we ship
`.crx.gz`). FK-guarded (needs the station row first → T5). Tested for RHOF/FIHO
against `gnss_europe_local` (idempotent). Original spec:
Index the **pushed R3 artifact** with `md5checksum` (compressed) + `md5uncompressed`
(gunzip|CRX2RNX) into `rinex_file`. Re-index updates `revision_date` (retro re-push).
- **New:** `dissemination/rinex_index.py` (md5 pass — `archive.content_sha256` is a
  *different* algorithm and must NOT feed `rinex_file`), `dissemination/epos_db.py`
  (see decision #1).
- **Reuse:** `gtimes.parse_rinex3_filename` (replaces legacy's broken R2-only slicing),
  CRX2RNX. Upsert SQL ported from `rawdataToPortal.db_metadata` but **parameterized**.
- **Verify:** seeded test EPOS schema → one `rinex_file` row, both md5s. `--index-only`.
- Deps: T1; DB-order dep on T5 (marker→id_station FK) — seed station row or sequence T5.
- Risk: EPOS DB connection (decision #1); FK ordering.

### T5 — TOS→EPOS metadata ETL ✅ station-core BUILT; ⚠️ items → T5b (2026-06-28)
Built `dissemination/{epos_db,epos_etl}.py`. **epos_db** = dedicated connection
(decision #1): `[epos_db]` in database.cfg, `search_path` to the schema (public on
dev/local, `gnss-europe-v0-2-9` on prod), constraint-/sequence-agnostic
`insert_row`/`get_or_create`/`update_row` (explicit next-id — the dev schema lacks
the UNIQUE constraints the legacy ON CONFLICT assumed). **epos_etl** = per-station
transactional upsert (no global TRUNCATE), TOSClient reads, `pyproj.Transformer`
xyz, SAVEPOINT-guarded contact + items. Tested live (TOS) → `gnss_europe_local` for
RHOF/AKUR/FIHO: **station-core idempotent** (3 inserted → 3 updated, no dupes).

**⚠️ T5b — item/device-history ETL is NET-NEW, not a faithful port.** Primary-source
finding: the EPOS GNSS schema is newer than the legacy script. `attribute` is a
**controlled vocabulary** (fixed ids 1=antenna_type … 26) and a **trigger**
(`trg_set_antenna_filter`, on `id_attribute=1`) requires antenna/receiver/radome
attrs to resolve the model string → `*_type.id` in `value_numeric`
(e.g. "TRM57971.00"→301). Legacy `tosToDatabase.py` inserts `value_numeric=NULL` +
creates attribute rows from raw TOS codes → would FAIL here. So T5b must map TOS
device attrs → the EPOS vocab + resolve model→type-id. Items are currently
SAVEPOINT-skipped (station-core still commits), logged. **Open:** does prod match
this dev schema (is the legacy container actually writing items)? — needs prod read.

Original spec —
Populate station/coordinates/monument/bedrock/geological/contact/device-history into
`gnss-europe-v0-2-9` for EPOS-flagged stations.
- **New:** `dissemination/epos_etl.py` (port `tosToDatabase.run`).
- **Reuse:** `TOSClient` for all reads (`get_children`, `history/entity`,
  `entity_contacts`, `contact` — fixes 8 dead-URL sites); `geofunc`/`pyproj.Transformer`
  for ITRF2008 xyz (replaces deprecated pyproj-1 `proj.transform(+init=EPSG:4326)`).
- **Net-new/modernize:** parameterized SQL; **per-station upsert in a transaction**
  instead of `TRUNCATE item/contact CASCADE` (the destructive non-transactional rebuild
  can leave EPOS empty on crash).
- **Verify:** dry-run prints upsert plan; one station vs legacy-produced reference rows.
- Deps: T3, decision #1. Risk: id allocation change (legacy `setval` after truncate);
  device child-history shape via TOSClient.

### T6 — Reactive TOS-fingerprint-diff sync (on→backfill, off→stop-only, retro re-push)
Daily diff re-ETLs only changed stations (+ manual `--refresh-metadata`). `in_epos`
on → full-station backfill (floor = install date) + DB + site log; off → stop-only
(mark rows inactive, **no delete**). A **header-affecting** change → recompute affected
date range from the corrected attribute's `date_from`/`date_to`, re-convert (cache
auto-invalidated via metadata fingerprint), **overwrite** R3 on EPOS (dissemination
tier uses `--update`; archive stays `--ignore-existing`), update `rinex_file.revision_date`.
- **New:** `dissemination/reactive.py` (fingerprint store + diff + on/off state machine).
- **Reuse:** `archive/state.compute_floor` for the backfill/retro floor; `EposDisseminate`.
- **Verify:** flip a test fingerprint → only that station re-runs; flip off → rows
  inactive, no push; flip a header attr → only affected epochs re-push.
- Deps: T4, T5. Risk: defining the fingerprint (which attrs = "changed"); install-date floor.

### T7 — Per-station IGS site logs → `gps-sitelogs` repo
Generate + commit site logs on the same TOS-change trigger.
- **New:** `dissemination/sitelogs.py` (git commit to new `gps-sitelogs` repo).
- **Reuse:** `tos sitelog` (`core/site_log.generate_igs_site_log`,
  `generate_igs_sitelog_filename`, `export_site_log_to_file`).
- **Verify:** one station vs known-good IGS site log; commit lands in test repo.
- Deps: T6, repo bootstrap (decision #3). Risk: repo creds on rek-d01.

### T8 — Scheduled wiring, double-gated
Scheduled `epos-disseminate` at `:45` alongside archive sync; double-gated
(`scheduler.yaml enabled` × `sync.yaml active`), inert by default.
- **New:** `dissemination/job.py` (`run_epos_disseminate_job`).
- **Edit:** `scheduling/bulk_scheduler.py` (`_schedule_epos_disseminate`, clone
  `_schedule_archive_sync`, `executor="backfill"`), `scheduling/config_loader.py`
  (`epos_disseminate` default `enabled:false`), `config/defaults/scheduler.yaml`.
- **Verify:** job registered only when enabled; absent under default config.
- Deps: T1–T7.

---

## Format policy — declarative in sync.yaml (Model B, 2026-06-29)

The format/naming/compression/layout policy lives in the target's `format:` block
(`DisseminationFormat`), not in code:

- **Model B — preserve source version.** The source's RINEX version is shipped
  unchanged (never R2↔R3 converted); version is detected from the obs **content**
  (`detect_rinex_version`), not the filename. (Discovery: the local archive's
  legacy-short-named `.26d.gz` files are actually RINEX 3.04 content.)
- **Per-version policy** (`rinex2`/`rinex3` → `VersionPolicy{naming, hatanaka,
  compression}`): R3→long `.crx.gz`, R2→short `.YYd.Z` (legacy `compress`). EPOS
  accepts both, naming differs by version.
- **Pipeline:** convert → cached canonical *plain obs* (version-preserving; R3 via
  `gfzrnx -vo {version}`, R2 via rename-only, never up/down-converted) → set-header
  → QC on the obs → `package()` (Hatanaka `RNX2CRX` + `gzip`/`compress`) → push.
  `published_name()` derives the final name; md5checksum on the packaged file,
  md5uncompressed on the obs.
- **Layout:** `dir_template` + `filename_template` (gtimes datepathlist tokens +
  `{station}`), default mirrors the legacy tree `%Y/#b/{station}/15s_24hr/rinex/`.
  `engine.relative_dir()` renders it; the indexer stores `/files/<rel>`.
- Phase 1 = `15s_24hr` only; per-session format blocks are a later extension.

Validated live: RHOF raw→R3 `RHOF00ISL_R_..._MO.crx.gz` (valid CRINEX 3.0) under
`2026/may/RHOF/15s_24hr/rinex/`, header from TOS, QC pass, indexed. 54 tests.

## set-header-from-TOS ✅ (2026-06-28)

The converted R3 header is now rewritten from TOS before caching/QC, so the
disseminated file is TOS-authoritative (not just whatever the archive/raw baked in):
- `convert.set_header_from_tos()` delegates to `tostools.rinex.correct_rinex_from_tos`
  with `station_config=None` → **TOS is the authority for every epoch** (EPOS-canonical,
  and correct for historical re-pushes). Best-effort; the QC gate blocks anything still wrong.
- **Cache key now folds the TOS fingerprint.** The engine fetches the session ONCE
  (drives both fingerprint and QC), computes `tos_access.session_fingerprint()` over the
  header-relevant fields (marker/receiver/antenna/radome), and passes it as
  `tos_fingerprint`. A TOS header correction → new fingerprint → new cache slot →
  re-render — the retroactive header-correction re-push mechanism, now live.
- `EposDisseminate(set_header=True)` default; gated on having a session provider
  (TOS mode). `--no-qc` (no provider) ⇒ offline mode, no set-header.
- **Verified live (RHOF):** pushed file header = `TRIMBLE NETR9 NP 4.60`, antenna
  `TRM57971.00`, ARP height `1.0070`, all from TOS; QC pass; new fingerprint forced
  a fresh re-render.

## CLI wiring (2026-06-28) — one command drives the chain

`receivers epos-disseminate`:
- `--list-stations` — TOS EPOS filter (T3).
- `--refresh-metadata [--station S]` — TOS→EPOS station ETL (T5); all EPOS stations if no `--station`.
- `--station S --date D [--force]` — convert (rinex or raw→rinex) → QC vs TOS (T2) → push → **index** (T4).
  `--no-qc` skips the gate; `--no-index` skips the rinex_file write; index is best-effort (needs `[epos_db]`/`EPOS_DB_*`).
- EPOS DB resolved via `[epos_db]` in database.cfg or `EPOS_DB_*` env (the harness uses env → `gnss_europe_local`).
Validated end-to-end for RHOF against the harness: refresh-metadata → 1 inserted; disseminate → QC pass,
pushed, `rinex_file id=1` FK→station.

## Local test harness (2026-06-28)

A read-only sandbox to validate before touching live:
- **EPOS DB copy**: the dev GNSS EPOS DB is the database **`gnss-europe-v0-2-9`** on
  `pgdev.vedur.is` (read via `.pgpass` as bgo) — full real schema but near-empty (5
  stations, 0 rinex_files). `pg_dump --schema-only` → local DB **`gnss_europe_local`**
  on localhost (77 tables). NOTE: the `postgres-epos-readonly` MCP points at a
  *different* EPOS DB (volcano/hazard, `10.170.110.80`) — not the GNSS one. Prod
  (`psql.vedur.is:6432/epos`) needs creds we don't have → live-DB data comparison deferred.
- **Dummy file server**: `~/tmp/epos_harness/{fileserver,convert_cache,sync.yaml}`
  (gated dissemination target, `source_root=~/tmp/gpsdata`, local dest).
- **raw→rinex** uses the **production native Trimble path** (`TrimbleNativeConverter`,
  `trm2rinex` Docker image) — the legacy runpkr00+teqc (`TrimbleConverter`) returns
  rc=30 / no output on these T02s. `convert_raw_to_rinex3_long` decodes raw → RINEX,
  then the shared chain → canonical long `.rnx`.

**Validated live (2026-06-28):** T3 filter against real TOS = 444 geophysical → 62
EPOS-eligible (RHOF/AKUR/FIHO all flagged). RHOF + FIHO: raw `.T02.gz` → R3 long name,
**QC passed against live TOS**, pushed to the dummy file server. AKUR: clean "no
archived RINEX or raw" (no local data). 41 tests pass.

## Open architectural decisions

### #1 — EPOS DB connection — RESOLVED
**Decision: dedicated `src/receivers/dissemination/epos_db.py`, NOT a
`DatabaseConnectionFactory` override.**

Rationale:
- EPOS is a *different server/user/creds* (`psql.vedur.is:6432/epos`, user
  `importer_epos`, schema `gnss-europe-v0-2-9`). The factory only swaps the DB *name*
  via `database=`; host/port/user/password come from `[postgresql]`.
- The factory's only host override is mutating the global `POSTGRES_HOST` env var —
  **racy under the threaded scheduler**, and the reactive job touches *both* gps_health
  and EPOS in one process.
- A factory connection would also risk triggering the gps_health **mirror dual-write**;
  EPOS has no mirror.

Shape:
- New `[epos_db]` section in `database.cfg` (`host`, `port`, `database`, `user`,
  `password`, `schema`). Follows the existing `[tos]` external-creds precedent;
  `database.cfg` is **never synced** (local-creds rule) — the right home for the secret
  the legacy container kept in `/run/secrets/epos_db_password`. Password may instead
  live in `~/.pgpass`.
- Connect via **psycopg2 directly** (matches receivers' psycopg2-native codebase; drop
  legacy SQLAlchemy). `managed_connection`-style commit/rollback/close context manager.
- **All SQL parameterized.** Schema name has a hyphen → must be **double-quoted**
  (`"gnss-europe-v0-2-9".station`); provide a quoting helper.
- No mirror/dual-write.

### #2 — Convert-cache key & location — key SETTLED, location open
**Key (settled by the retro-re-push requirement):** `hash(source content_sha256 +
TOS-metadata fingerprint for that epoch)`. Source hash alone would leave the cache
"valid" after a header correction and the fix would never re-render.
**Open:** on-disk cache path on rek-d01 + eviction policy (size/age).

### #3 — `gps-sitelogs` repo bootstrap (open, ops)
Who creates/clones it; write path + commit credentials on rek-d01.

### #4 — EPOS files-server dest + secret model (open, ops)
Legacy `epos@epos-portal.vedur.is:/mnt/epos_01/gps/` with `sshpass -f`; receivers uses
SSH-key auth. Confirm real host/path + `/files`-prefixed relative-path convention.

### #5 — Precedence (confirmed)
Phase 1 uses simple prefer-rinex-else-raw, **not** #34. Noted so reviewers don't expect
the provenance engine.

---

*Cross-ref: `.interrogate-epos-dissemination.md`, `epos-gnss-port-analysis.md`,
[[1781867391-data-dissemination-archive-sync-design]]. Created: 2026-06-28.*
