# epos-gnss → receivers: Port Analysis

Analysis of `git.vedur.is/aut/ut-dev/dp/deliverables/epos-gnss` (cloned to
`../epos-gnss`) in terms of running it inside the `receivers` package using the
tooling we've already built.

**TL;DR:** epos-gnss is the legacy **EPOS dissemination** pipeline — exactly the
`tier: dissemination` target already scoped in
[[1781867391-data-dissemination-archive-sync-design]]. It is *not* the archive-write
path; our `receivers.archive` module replaces a different (legacy rek→rawdata)
pipeline. Porting it means **reusing the archive engine as a transport mechanism**
and **building the EPOS-specific behaviour that does not yet exist** (station
filtering by `in_network_epos`, a metadata ETL into a third database, RINEX
md5 indexing, and — for the modern path — gfzrnx format conversion).

---

## 1. What epos-gnss actually does

A 4-script Docker container, run daily (`schedule.ini` → `08:30`) on
`swarm-internal-p01`, NFS-mounting `ananas:/gps` read-only:

| Script | Job |
|--------|-----|
| `scheduler.py` | Generic `schedule`-based loop that shells `python -m scripts.<name>`. Pure infra. |
| `gnssEpos.py` | Entrypoint: reads `default.conf`, parses `-start/-end/-s/-d`, runs the two stages below. |
| `tosToDatabase.py` | **TOS → EPOS DB metadata ETL.** Pulls geophysical stations from TOS, keeps `in_network_epos=true`, validates required attributes, TRUNCATEs `item`/`contact`, repopulates station/coordinates/monument/bedrock/geological/contact + full device item-history into the `gnss-europe-v0-2-9` schema. |
| `rawdataToPortal.py` | **Archive → epos-portal RINEX push + index.** For each EPOS station+month, rsync-dry-runs `…/15s_24hr/rinex/*.Z` to find changed files, indexes their RINEX metadata (`indexGeodeticData.py`), upserts into the EPOS `rinex_file` table, then rsyncs the files to `epos@epos-portal.vedur.is`. |
| `indexGeodeticData.py` | RINEX filename/mask parsing + `md5checksum` (compressed) and `md5uncompressed` (gunzip\|CRX2RNX). |

**Both halves are dissemination.** It *reads from* the archive
(`/net/rawdata/exports/gpsdata/`) and *writes to* epos-portal + the EPOS DB.
Neither half is the archive-write path that `receivers.archive` handles.

---

## 2. Mapping to tooling we already have

| epos-gnss concern | Our tool | Reuse verdict |
|---|---|---|
| Scheduling (`scheduler.py` + Docker swarm) | APScheduler `bulk_scheduler` + gpsops `systemctl --user` unit | **Replace.** It becomes one more scheduled job on the `backfill`/dissemination executor; no separate container. |
| rsync delta + `--files-from` + watermark | `receivers.archive.engine.ArchiveSync` | **Reuse the mechanism.** find-by-mtime → `rsync --files-from` → catalog → advance watermark is already generic. |
| Declarative target config | `config/defaults/sync.yaml` (`SyncTarget`) | **Extend.** The design note already sketches an `epos_dissemination` target (`tier: dissemination`, `active:false`). |
| TOS reads (`requests.get` hardcoded `/tos/v1/...`) | `tostools.api.tos_client.TOSClient` | **Reuse — and this is urgent.** See §3. |
| RINEX naming / path templating | `gtimes.datepathlist` (via `FormatResolver`) | **Reuse** for the dissemination layout/rename. |
| Format conversion (R2↔R3, short↔long, Hatanaka) | `gfzrnx` / `CRX2RNX` in `gps-tools` | **Reuse** (modern path only; legacy ships `.Z` as-is). |
| RINEX header/metadata extraction | `receivers.rinex`, `tostools` RINEX QC | **Reuse/adapt** for the indexer (but hashing differs — §4). |

---

## 3. The hardcoded TOS URLs are stale — strongest concrete win

epos-gnss calls `https://vi-api.vedur.is/tos/v1/entity/...` directly with raw
`requests.get` (≈8 call sites across the two scripts). **Verified:** tostools now
uses `https://vi-api.vedur.is/tos/internal` via `canonical_tos_url()`
(`DEFAULT_TOS_URL`, `tos_client.py:17`); the old `/tos/v1/` paths 308-redirect on
the revised backend (tostools PR #63, ~2026-06-24).

So a faithful copy-paste port **would point at dead URLs**. Every raw request
(`entity/search/station/geophysical`, `get_children`, `history/entity`,
`entity_contacts`, `contact`) must route through `TOSClient`, which already handles
the URL migration, `None`/`Null` segment guards, and trailing-slash 308 noise.
This alone justifies not running epos-gnss as-is.

---

## 4. What genuinely does NOT exist yet (net-new work)

The advisor's key correction: **do not assume "half is already built."** The
`archive/` engine gives us *transport*, not EPOS behaviour. Missing pieces:

1. **EPOS station filter.** `SyncTarget` today has only `exclude_stations` +
   `sessions`. EPOS needs **include-by-attribute** (`in_network_epos=true`, plus
   `checkMinimumRequirements`). New filter dimension, sourced from TOS via `TOSClient`.

2. **The TOS → EPOS-DB metadata ETL (`tosToDatabase.py`) — the larger half.**
   This populates a **third database** (`gnss-europe-v0-2-9`), separate from both
   `gps_health` (where `archive_catalog` lives) and TOS. **There is no receivers
   equivalent.** It is a full station/coordinates/monument/bedrock/geological/
   contact/item-history upsert with a TRUNCATE-and-repopulate pattern. Porting =
   net-new, reusing `TOSClient` for reads + `geofunc`/`pyproj` for the ITRF2008
   xyz transform (the script's `proj.transform` + `+init=EPSG:4326` is deprecated
   pyproj-1 API — modernize to `pyproj.Transformer`).

3. **EPOS `rinex_file` indexer with md5 — not content_sha256.** EPOS wants
   `md5checksum` (compressed) **and** `md5uncompressed` (gunzip|CRX2RNX). Our
   `archive.content_sha256` is a *different algorithm and different semantics*
   (decompressed-content sha256 for integrity/dedup). **You cannot feed the
   archive integrity hash to the EPOS table** — the dissemination indexer needs
   its own md5 pass. This is a distinct artifact from `archive_catalog`.

4. **epos-portal destination + secrets.** New `SyncTarget` (`epos@epos-portal`,
   `/mnt/epos_01/gps/`, `/files`-prefixed relative paths). `sshpass`-from-file →
   our SSH-key model.

5. **gfzrnx conversion stage** (modern path only): the design note's
   `convert_with: gfzrnx` + content-hash-keyed conversion cache. Legacy ships
   `.Z` verbatim, so a *faithful* port skips this.

---

## 5. #34 (precedence) is a soft dependency, not a blocker

The design note gates dissemination behind #34 (the provenance/precedence
engine). But legacy epos-gnss just globs `15s_24hr/rinex/*.Z` blindly — a
**faithful** port needs nothing from #34. Two honest options:

- **Faithful-port-now:** replicate the blind glob; ship EPOS dissemination as a
  `sync.yaml` target immediately. Lower correctness, unblocked today.
- **Principled-port-post-#34:** dissemination consumes #34 to pick the
  *authoritative* RINEX (raw-derived vs stream-derived, original vs regenerable)
  before converting/pushing. Higher correctness, sequenced after #34.

Recommendation: faithful-port-now for raw `.Z` 15s_24hr (matches legacy exactly,
retires the swarm container), then layer gfzrnx conversion + #34 precedence as
phase 2 — consistent with the existing phasing in 1781867391.

---

## 6. Other gotchas worth flagging

- **TRUNCATE-and-repopulate** of `item`/`contact` each run is destructive and
  non-transactional in the script — a crash mid-run leaves EPOS metadata empty.
  A receivers port should wrap the rebuild in a transaction or switch to upsert.
- **SQL string-formatting** throughout both scripts is injection-prone
  (`.format()` into raw SQL). Use parameterized queries (SQLAlchemy `text()` binds).
- **Brittle filename slicing** in `indexGeodeticData.py` (`abs_path[-14:-10]`,
  `strptime(rinex_date, "%j0.%yD.Z")`) is RINEX2-short-name only and breaks on
  `.D.Z` casing / RINEX3 long names. Replace with `gtimes` parsing.
- **Scheduler infra** (`scheduler.py` + swarm placement) is fully redundant with
  our APScheduler/systemd stack — drop it entirely.

---

## 7. Recommended shape inside receivers

```
receivers.dissemination/            # new sibling to receivers.archive
  config.py        # DisseminationTarget (extends SyncTarget: include-filter, convert spec)
  engine.py        # reuse ArchiveSync transport; add include-by-TOS-attribute
  epos_db.py       # TOS→EPOS gnss-europe schema ETL (uses tostools TOSClient + geofunc)
  rinex_index.py   # md5/md5uncompressed indexer → EPOS rinex_file (gtimes parsing)
  convert.py       # gfzrnx wrapper (phase 2), content-hash-keyed cache
```

Wired as a `tier: dissemination` target in `sync.yaml`, scheduled alongside the
`:45` archive sync, double-gated `active:false` until validated — mirroring how
the archive-sync MVP was shipped.

**Bottom line:** the *transport* and *TOS-read* and *naming* tooling exists and
should be reused; the *EPOS metadata ETL* and *md5 RINEX index* are net-new and
are the bulk of the work. Running epos-gnss verbatim is not viable (dead TOS URLs,
redundant scheduler, injection/robustness debt) — it's a port, not a lift-and-shift.

---

*Cross-ref: [[1781867391-data-dissemination-archive-sync-design]] (§"dissemination
layer"), receivers todo #34 (precedence), `docs/architecture/config-data-flow.md`.*
*Created: 2026-06-28.*
