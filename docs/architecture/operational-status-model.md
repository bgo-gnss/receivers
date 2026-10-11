# Operational status model — stations.cfg ↔ TOS consolidation

Status: **proposed → attributes created 2026-08-19** (cfg + receivers wiring pending)

## Problem

`stations.cfg` carries seven operational-ish per-station fields that overlap
and are inconsistently consumed. Audited 2026-08-19:

| Field | Values | Code semantics | Consumed? |
|---|---|---|---|
| `station_role` | active/passive | `passive` = data-source-only (GLOBK reference ties), not operated | ✅ skip in bootstrap/seeder/reconcile/scheduler |
| `health_check` | NULL/active/passive | `passive` = operated but don't poll health | ✅ skip in bootstrap/gap/backfill/horizon/icinga |
| `station_status` | NULL/active/inactive/discontinued/suppressed | lifecycle; auto-`inactive` when `receiver_type` empty; `discontinued` = has end date | ✅ skip + DB sync |
| `acquisition_mode` | download/stream | transport (direct pull vs RTCM stream→RINEX) | ✅ stream scheduler |
| `download_method` | external/rinex_external | third-party pull | ❌ **dead — zero consumers** |
| `is_reference_site` | true/false | reference flag | ❌ parsed, never read |
| `is_in_iceland` | true/false | in-Iceland flag | ❌ parsed, never read |

Two words are overloaded (`passive` means different things in `station_role`
vs `health_check`), and three fields are dead/parsed-but-unused. The goal is a
single canonical source (TOS) from which `stations.cfg` can eventually be
*generated*.

## The model — two orthogonal axes

| Axis | Question | Values | Home |
|---|---|---|---|
| **operational_status** | who runs it / how data reaches us | `operated` · `external` · `reference` | TOS (new, id 189) |
| **lifecycle** | is it running | active · inactive · discontinued | TOS (existing: `date_end` + open receiver join) |

Lifecycle is deliberately **not** folded into `operational_status`:
`discontinued` ≡ `date_end` set, and `inactive` ≡ no open receiver join (or
`continuity = campaign`). Both are already derivable from TOS.

## New TOS attributes

### `operational_status` (station, text enum — id 189)

| Value | Icelandic | Meaning |
|---|---|---|
| `operated` | Innri stöð | IMO operates it: direct download + health monitoring (default) |
| `external` | Ytri stöð | third party operates it; we pull files from their server, no health polling |
| `reference` | Viðmiðunarstöð | data-source-only reference station (GLOBK ties); not operated, no monitoring |

`python_constraint = ^(operated|external|reference)$`

### `external_data_type` (station, text enum — id 190)

| Value | Meaning |
|---|---|
| `rinex` | we pull RINEX from the external source (common) |
| `raw` | we pull raw/SBF from the external source (rare) |

Only meaningful when `operational_status = external`.
`python_constraint = ^(rinex|raw)$`

`stream` is intentionally **not** a value — a stream station (e.g. SEY9) is
still `operated`; transport is `acquisition_mode`.

## Mapping (old cfg fields → new model)

| stations.cfg today | After |
|---|---|
| `health_check = passive` | → `operational_status = external` (or `reference`); "operated ⇒ monitor" becomes the rule |
| `download_method = external` / `rinex_external` | → `operational_status = external` + `external_data_type = raw` / `rinex` |
| `station_role = passive` | → `operational_status = reference` |
| `is_reference_site = true` | → `operational_status = reference` |
| `is_in_iceland` | keep (geographic, orthogonal) |
| `acquisition_mode = stream` | keep (transport, orthogonal to ownership) |
| `station_status = inactive` | derived (no open receiver join / campaign) |
| `station_status = discontinued` | derived (`date_end` set) |

Net: four scattered fields collapse to **one TOS attribute**, the two genuinely
orthogonal fields stay, and lifecycle becomes derived rather than hand-kept.

## Backfill mapping (per-station, from today's stations.cfg)

| old `download_method` | example | new |
|---|---|---|
| `rinex_external` | MYVA (NATT) | `operational_status=external`, `external_data_type=rinex` |
| `external` | KRAC, THRC, TORK (UI) | `operational_status=external`, `external_data_type=raw` — **confirm per station** (raw vs rinex is unverified; the dead field never distinguished it reliably) |

## Rollout

1. ✅ Create `operational_status` (id 189) + `external_data_type` (id 190) in TOS.
2. ✅ Add `operational_status` to `stations.cfg`; keep old fields as read-only
   aliases for one transition window, then drop them. Landed 2026-10-11 as a
   `FieldSpec` with `sync_from_tos=True` and **no** `tos_attribute_code` — cfg
   mirrors TOS one-way and can never push it, because *who runs a station* is an
   organisational fact only TOS owns (same rule as `rinex_marker_number`).
3. ✅ One-time backfill. Verified live 2026-10-11: **215 operated / 4 external /
   0 missing** across the 219 GPS stations. `VFLN` and `VFLS` lacked it entirely
   and were backfilled to `operated` from their own station `date_start`.
4. ✅/− Wire receivers to read `operational_status` … **but see the correction
   below: `station_role` must NOT be removed.**
   The `health_check == "passive"` skip checks were replaced by
   `receivers.operational_scope.is_operated()` at 8 call sites
   (config_utils, cli/scheduler, bootstrap, bulk_scheduler,
   long_term_backfill, receiver_horizon_probe, cli/cfg).
5. ❌ **REVISED — "generate `stations.cfg` from TOS" was the wrong frame, and
   the step is already satisfied.** See below.

### Correction 1 — `station_role` is load-bearing, not a legacy alias

The list of old fields is NOT uniform. `stations.cfg` holds **two different
populations** that share the file by historical accident:

| population | sections | unique keys | in TOS? |
|---|---|---|---|
| IMO stations | 199 | 44 (receiver/antenna/router/ports/credentials/acquisition) | ✅ yes |
| global reference sites | 147 | 4 (`station_role`, `is_reference_site`, `is_in_iceland`, `sitelog_id`) | ❌ **no TOS entity at all** |

`station_role = passive` is the **only** marker identifying the 147. They are
IGS/EPN stations (ALGO, ALIC, AUCK, BRUX, CEDU, CHUR, DAEJ, DARW, …) whose data
we consume for **GLOBK network ties**; none has a `receiver_type`, and none
exists as a TOS station. Removing that marker would make them indistinguishable
from IMO stations — which are then expected to have a router, ports and a
receiver — and they would be silently lost from `stations.cfg`.

**So:** keep `station_role`. Only `health_check`, `download_method` and
`is_reference_site` are retirement candidates. Because `station_role` is the
marker for a population with no TOS record, it can never converge to
`operational_status` — that axis stays cfg-local by necessity.

### Correction 2 — step 5 needs no new mechanism; two of its three named fields are done

Step 5 named three things. Checked on 2026-10-11:

| named field | status |
|---|---|
| `operational_status` | ✅ already synced (step 2) |
| "device metadata" (receiver/antenna type, serial, firmware, radome) | ✅ already synced |
| `acquisition_mode` | ❌ **not a TOS attribute** — TOS reports *"not a geophysical station attribute — no station carries it and the catalog does not list it"* |

`acquisition_mode` is a **cfg-only implementation detail** of our own pipeline
(does the receivers scheduler download directly, or does BNC stream capture
provide the session?). Exactly 3 stations carry it (GONH, HRIC, SEY9 — all
`stream`). It describes how *we* acquire data, not a property TOS could know, so
it can never be generated from TOS.

Therefore the existing **field-level** `cfg sync-from-tos` mechanism already
delivers step 5's intent, and a wholesale file-rewrite was never required. A
rewrite would additionally be unsafe: the IMO sections carry host-specific keys
TOS does not hold (`router_ip`, `receiver_ftpport`/`httpport`/`controlport`,
`tmp_dir`, `data_prepath`, credentials, `remote_*`, `base_path`) which a
generator would have to preserve perfectly or destroy.

The 147 are structurally safe under the field-level approach: they have no TOS
record, so a TOS→cfg sync has nothing to write and cannot touch them.

---

*Created 2026-08-19 (bgo, with pi). Attributes live in TOS; cfg + receivers
wiring are follow-ups.*

*Rollout status reviewed and corrected 2026-10-11 (bgo, with pi) after landing
steps 2–4. Corrections 1 and 2 above supersede the original step 4 ("remove the
`station_role`/`health_check` skip logic") and step 5 ("generate `stations.cfg`
from TOS").*
