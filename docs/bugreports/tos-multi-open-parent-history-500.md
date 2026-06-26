# Bug report — TOS web UI: entity-history view returns HTTP 500 for a device with two open parent joins

**To:** IMO-IT (TOS maintainers)
**From:** Benedikt Gunnar Ófeigsson — GPS/GNSS team, Veðurstofa Íslands
**Date:** 2026-06-26
**Component:** TOS web UI — entity history view (the device/station "history" / timeline page)
**Severity:** Medium — hard 500 on a valid backend state; blocks viewing history for affected entities

## Summary

When a device entity has **two (or more) simultaneously open parent joins** (two
`entity_connection` rows with `time_to = NULL` to two different parent entities), the
TOS **REST backend accepts and stores it**, but the **web UI history view returns
HTTP 500** and the page cannot be opened.

The REST API itself is fine — `GET /tos/internal/entity/parent_history/{id_child}`
returns both open joins without error. Only the web UI rendering of the history 500s.

## Why we hit this — legitimate use case: shared telemetry at joint sites

Several of our sites are **joint GPS + seismic (SIL) stations**: a single physical
telemetry box (a Teltonika router and its SIM) serves *both* co-located stations. The
natural TOS model is one device entity with an open join to **each** station it serves.
The backend stored this cleanly; the UI is the only thing that breaks.

## Reproduction (observed live)

Site: Krísuvík — GPS station `KRIV` (`id_entity = 4378`) and seismic station `kri`
(`id_entity = 5469`), co-located, sharing one router.

1. Device: `modem_gsm` `id_entity = 21528` (Teltonika RUT200, serial `6003840342`),
   already open under the seismic station via join `28838`
   (`id_entity_parent = 5469`, `time_from = 2026-06-03T11:46:00`, `time_to = NULL`).
2. Added a second open join to the GPS station: `POST /tos/internal/joins`
   `{id_entity_parent: 4378, id_entity_child: 21528, time_from: 2026-06-03T11:46:00,
   time_to: null}` → **accepted (join `29026` created)**.
3. State after step 2 — device `21528` had **two open parent joins** (`28838 → 5469`
   and `29026 → 4378`); `GET /entity/parent_history/21528` returned both correctly.
4. **Opening the history view in the web UI for the affected entity → HTTP 500.**
5. Deleting one of the two open joins (`DELETE /admin_entity_connection_row/29026`,
   back to a single open parent) → **the 500 cleared**, history view works again.

So the 500 correlates exactly with the presence of ≥2 concurrently-open parent joins.

## Expected behaviour

The history view should render a device that has more than one open parent join
(showing it under each parent / on each timeline) rather than 500, OR — if multiple
open parents are considered invalid by design — the **backend** should reject the
second open join at write time with a clear 4xx, instead of accepting it and letting
the UI fail later.

Either resolution unblocks us; we'd prefer the UI tolerating ≥2 open parents, since the
joint-site sharing is a real and recurring data shape for us.

## Diagnostic details to help locate it

- The failing layer is UI/rendering, not storage: the underlying `parent_history`
  REST read succeeds with both joins present.
- Likely cause: the history renderer assumes a single "current parent / current
  location" (e.g. takes the one open join, or indexes `[0]`, or a query that returns
  >1 row where one is expected) and throws when it finds two.
- The relevant server-side log entry / stack trace at the time of a 500 on the history
  endpoint for entity `21528` (or `4378`/`5469`) would pinpoint it. We can reproduce
  on request in a test entity if useful.

## Questions for IT

1. Is a device with multiple simultaneously-open parent joins **intended** to be valid
   in the TOS model (shared device at a joint site), or should it be disallowed?
2. If valid → can the history view be made to tolerate it?
3. If invalid → can the **backend** reject the second open join at write time so the
   bad state never gets stored?

## Our interim workaround

We are **not** using the two-open-join model. The shared router stays a single-owner
device (under the seismic station), and we attach the relationship to the GPS station
via a maintenance record (vitjun) plus local config — no second open join.

---
*Drafted by the GPS team for review before sending to IT. Internal ref:
`reference_cfg_shared_device_join`; tooling: `receivers cfg replace-modem/replace-sim
--shared` (held, not in production use).*
