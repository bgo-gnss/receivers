# GAMIT station.info (+ lfile.) under version control in gps-config-data — handoff

*Handed over 2026-10-13 from the gpslibrary/SINEX session to the receivers project.
Status: **not started** — analysis done, nothing moved, nothing written on okada or gamit-p01.*

## Decision (BGÓ, 2026-10-13)

- The GAMIT `station.info.*` file moves into **gps-config-data** and is maintained
  there, like every other station config (receivers rule 2026-07-06: all config
  changes are made in gps-config-data and propagate outward; never edit a deployed
  file). The receivers project owns the move.
- `lfile.` (GAMIT a-priori coordinates) goes there too. GAMIT maintains it, but its
  history is worth keeping under version control.
- gamit-p01 is being prepared to take over from okada as the operational GAMIT
  machine. gamit-p01 is updated **from the gps-config-data files once they are
  reviewed** — not before, and only with BGÓ's go.
- **First step: reconcile, don't copy.** Neither host's station.info is a superset
  (below), so the first committed file is a reviewed merge.

## Current state (2026-10-13)

| | okada (`/D/DATABASE/GAMIT/`) | gamit-p01 (`/D/DATABASE/GAMIT/`) |
|---|---|---|
| `station.info.sopac.apr05` | 3.81 MB, last edit 2026-10-11 | 4.11 MB, last edit 2026-10-08 |
| `lfile.` | 589 kB, 2026-10-03 | 599 kB, 2026-09-22 |
| history | ~25 `.backup.<ts>` / `.bak-<ts>-<what>` copies | ~20 `.bak_<what>_<ts>` copies + `.json` audit records (gonh, …) |
| writers | `tosGPS syncMeta --type gamit-station-info --update` (tostools, ssh `gpsops@okada`), hand edits, `cfg import-campaigns` reads it | hand edits (global SOPAC/IGS refresh 2026-09-11, 6 h/campaign aliases, fixes) |

Both read-only on the laptop: `/mnt_data/okada/D/DATABASE/GAMIT`,
`/mnt_data/gamit-p01/data/DATABASE/GAMIT`. A third copy is tostools'
`data/station_config/station.info.sopac.apr05` (fetched by `tosGPS`).

## Evidence already gathered (read these first)

All under `~/gps-data/curation/metadata_divergence/` (gps-data repo):

- `2026-10-12/NOTES.md` — full audit: which source holds which field and which is
  authoritative, counts, top-15 divergences, okada-vs-p01 verdicts (§4), the
  recommended procedure (§5). `divergence_windows.csv` — 1574 rows (station,
  window, field, okada, p01, TOS, suspected correct value, evidence, confidence,
  impact). `rinex_evidence.csv` — RINEX header ANT TYPE/SN/DELTA H probes.
- `2026-10-13-p01-edits/README.md` + `p01_edits.py` — gamit-p01's backup chain:
  which of its edits since June okada lacks.

Key findings:

- **Neither file is a superset.** okada has the IMO operational updates (ARHO,
  HRAC, EYVI, NPSK, VFLN/VFLS, VOTT 2026, SAUD/SAVI receivers, STOR 2015+, MYVA
  2018, the 2025-09 batch incl. the GUSK fix). gamit-p01 has the global refresh
  (okada is stale for 92 of 141 global sites, 51 with a different antenna/radome),
  plus MANA/SVIN serials, NAMC start, SAVI 2007–2016 heights, SAUD radome SCIS,
  VOTT campaign details, HOFN/REYK from the IGS logs.
- **TOS is not automatically right.** 168 TOS session gaps (60 stations), invalid
  HtCod (`-----`, `BPA`), composite-height errors (TOS splits monument + antenna
  height; the GUSK pattern recurs). The RINEX header is the adjudicator for devices.
- **Errors in both files** that change GAMIT results: HVAS antenna type since
  2021-05, ISAF radome `TGZD` (typo for TZGD), KALT column-shifted rows, BARC/BAC1-3
  unparseable dates (`2014 244200 00 00`), MYVA SN-column spill, JOKU dome shift.
- **Need BGÓ's decision** (datum conventions, a step in the series if changed):
  HAFS height 0.0 vs 1.078 m, GOLA, MOHA, LISF; HRSC +1 m TOS edit 2026-09-01
  (field team: was it re-mounted?); KAST start 10-01 vs 10-24; SAVI 2016→ antenna;
  TPSPG_A1 vs TPSPG_A1+GP (KVSK/EYVI/ALFD/DYNG, needs photos).

## Proposed shape (to be designed here)

1. **Layout** in gps-config-data, e.g. `gamit/station.info.sopac.apr05`,
   `gamit/lfile.`, `gamit/README.md` (what, who writes, how deployed). Plain text,
   4 MB — fine for git; diffs are line-based and readable.
2. **Seed commit = the reviewed merge**: start from gamit-p01 (global sites right),
   apply okada's IMO operational updates, fix the structural defects, apply the
   decided HIGH items; each correction also recorded in `gps-tos-corrections` so TOS
   is fixed in the same pass. Commit the two raw host copies first (as
   `…okada-2026-10-xx` / `…p01-2026-10-xx` snapshots, or in history) so the merge
   is a reviewable diff.
3. **Direction of truth per field** (NOTES §5): IGS site logs for global sites;
   TOS for IMO devices only after RINEX/visit-log agreement; station.info for
   heights/eccentricities where TOS monument data is incomplete; HtCod always DHARP.
4. **Writers change target**: `tosGPS syncMeta … --update` and hand edits write the
   gps-config-data file (commit + push), not okada. Deployment pushes it to the
   GAMIT host(s) — okada until it retires, gamit-p01 after BGÓ's go.
5. **lfile. is machine-written**: GAMIT adds/updates coordinates during processing,
   so a blind repo→host deploy would drop them. Treat it as host→repo (scheduled
   snapshot commit from the master host, so history is kept) unless a deliberate
   edit is pushed. Check the same for station.info: whether the GAMIT set-up on
   either host lets `sh_gamit`/`xstinfo` append entries from RINEX headers
   (`sites.defaults` / `process.defaults` on each host) — if so, that path must be
   turned off or captured the same way.
6. **Guard on every change** (CI or a pre-commit check): fixed column widths parse
   back identically (tostools `standards.gamit_station_info`), dates ordered and
   non-overlapping per site, antenna+radome present in the ANTEX in use
   (`igs20_2415`), receiver names in `rcvant.dat`, HtCod valid; plus a nightly
   `tosGPS syncMeta --verify-only` diff against TOS.

## Related, but not this task

- 8 IMO stations are missing or partial in gamit-p01's processing (HLID, HRAG,
  NORS, NPSK, SEY1 none; DYNG, MANA, SEY2 partial) — gamit-p01's subnet lists, to
  fix when processing moves there. Those lists (and `EQ`/`sittbl.`) may belong in
  gps-config-data too.
- gamit-p01 h-files carry whatever station.info held when they were made (finals
  2018–2025 made Jul–Oct 2026). Corrections only reach the IGb20 series after
  gamit-p01 reprocesses the affected stations/days; the laptop's
  `~/gps-data/curation/sinex/update_igb20.sh` then remakes those days by itself.

## Don'ts

- No writes on gamit-p01 (BGÓ's go needed). On okada only the established tostools
  path until the gps-config-data file takes over.
- No blind TOS → station.info sync (it would write invalid HtCod and composite-height
  errors).
- Don't read `stations.cfg` credential keys (receiver passwords) when comparing.
