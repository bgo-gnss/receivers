# EPOS OBSERVER/AGENCY-from-TOS + site-log completeness — design / investigation

Status: **investigation + plan only (not implemented)**. Captured 2026-07-01 during the
May+June full-fleet dissemination validation sweep. Two related issues surfaced; both
resolve to the same root — the disseminated products currently hard-code a single
agency (IMO), but the fleet spans **three** owner agencies, and that agency identity
must come from TOS.

## Problem

### Issue 1 — site logs are missing fields
Our generated `RHOF00ISL.log` vs the M3G reference
(`https://gnss-metadata.eu/sitelog/exportlog?station=RHOF00ISL`):

| Field | M3G reference (v2.0) | Ours | Root cause (tostools `core/site_log.py`) |
|---|---|---|---|
| Title 9-char | `RHOF00ISL … (site log v2.0)` | `RHOFISL00 … (site log)` | line 54 hard-codes `{marker}ISL00` — country/monument **swapped**, no `v2.0` tag |
| §1 ID label | `Nine Character ID: RHOF00ISL` | `Four Character ID: RHOF` | line 126 is the old v1.0 label |
| Instructions URL | `https://files.igs.org/pub/station/general/sitelog_instr.txt` | `ftp://igs.ign.fr/…/sitelog_instr.txt` | outdated link (dead FTP) |
| §0 Prepared by | `GNSS Operator (gnss-epos@vedur.is)` | `GNSS Operator` | line 62 hard-coded, no email |
| §0 Previous Site Log | `rhof00isl_20240827.log` | empty | not emitted |
| **§11 On-Site Point of Contact Agency** | Agency, abbrev, address, e-mail | **absent** | generator stops ~§8; §11-13 never built |
| **§12 Responsible Agency** | (present) | **absent** | " |
| **§13 More Information** | Data Center, URL | **absent** | " |

The filename is correct (`RHOF00ISL.log`); only the in-file **title** has the 9-char
in the wrong order. Note the composite ARP height (1.014 m) already matches the
reference — the `_monument_height_for_period` fix is good.

### Issue 2 — RINEX header OBSERVER/AGENCY is hard-coded
`config.py` (`observer="GNSSatIMO"`, `agency="Vedurstofa Islands"`) + `sync.yaml`
apply one agency to **every** station. `convert.finalize_epos_header` writes it
verbatim into `OBSERVER / AGENCY`. So the 17 non-IMO stations get the wrong agency.

## TOS data model (the source of truth)

Each station carries a `contact` dict with `owner` and `operator` roles, each with an
`organization`. **Owner organization** is the clean signal:

| TOS `contact.owner.organization` (Icelandic) | # EPOS stations | stations |
|---|---|---|
| **Veðurstofa Íslands** | 44 | RHOF, ELDC, … |
| **Landmælingar Íslands** → now **NATT** | 14 | AKUR, ALHV, BJTV, BLON, FIHO, GJFV, GUSK, HEID, ISAF, LAVI, MYVA, RHOL, SKHA, VOFJ |
| **Jarðvísindastofnun Háskóla Íslands** (Univ. of Iceland, Inst. of Earth Sciences) | 3 | GRIV, TKJ2, TKJS |

⚠️ **Do not use `operator`** — fleet survey (61 EPOS markers) shows it is unreliable:
**47/61 have no operator at all**; 13 duplicate the owner org (Veðurstofa Íslands ×8,
Landmælingar ×3, Jarðvísindastofnun ×2); and **RHOF's operator is a *personal name***
("Benedikt G. Ófeigsson / AUT") on a 25-minute artifact period
`2024-11-07T13:19:27 → 13:44:48` (`id_contact_entity_relationship=4987`,
`id_contact=2483`) — a test write, and EPOS forbids personal names. Use
`owner.organization`.

**TOS hygiene todo (separate from dissemination):** remove/correct the RHOF personal-name
operator relationship (single closed 25-min period → clean delete or Pattern-4 correction
to the real operating agency, IMO). Draft the tostools write dry-run before applying;
needs user go-ahead (TOS write). Nothing in dissemination depends on it.

Landmælingar Íslands merged into a new institution in 2024 (the user's "NATT"); the
**new English name, address, and e-mail are pending** (user is contacting them).

## Agency mapping (the shared table)

A single canonical map keyed by the Icelandic TOS `owner.organization`, providing
**English** names for EPOS/global distribution. **Home: `gps-config-data/agencies.yaml`**
— a *deployed config file* under version control, synced to `~/.config/gpsconfig/`
(`GPS_CONFIG_PATH`) exactly like `stations.cfg` / `sync.yaml` (user directive 2026-07-01),
**not** a static tostools package resource. Loaded by `receivers.dissemination.agencies`
(**built — commit 843a37f**); consumed by the receivers header finalizer and passed into
the tostools site-log generator.

| owner.organization | abbrev (EN/IS) | English name | RINEX OBSERVER | RINEX AGENCY | email |
|---|---|---|---|---|---|
| Veðurstofa Íslands | IMO / VÍ | Icelandic Meteorological Office | `GNSSatIMO` | `Vedurstofa Islands` | gnss-epos@vedur.is |
| Landmælingar Íslands → **Náttúrufræðistofnun** | **NSII / NATT** | **Natural Science Institute of Iceland** (dept: Land Survey / Landmælingar; addr Smiðjuvellir 28, 300 Akranes) | `GNSSatNATT` | `NATT` | **gnss@natt.is** |
| Jarðvísindastofnun Háskóla Íslands | *(IES? / JHÍ?)* | Institute of Earth Sciences, Univ. of Iceland | `GNSSatIES?` | `IES?` | *(TBD)* |

Site-log §11/§13 use the **English** name + `abbrev` (IMO / NSII); RINEX OBSERVER/AGENCY
use the IS-abbrev form (`GNSSatNATT` / `NATT`) per user directive. Both languages stored.

**User-confirmed semantics for a NATT-owned station** (generalize per role):
- §11 On-Site Point of Contact = **IMO** (IMO physically operates + maintains it),
- §12 Responsible Agency = **NATT** (the owner),
- §13 Data Center = **IMO** (IMO hosts/disseminates the data),
- RINEX header: `OBSERVER = GNSSatNATT`, `AGENCY = NATT` (the responsible agency).

So the per-station identity is **two-layered**: the *responsible/owner* agency drives
the RINEX AGENCY + sitelog §12, while the *operator/data-center* (IMO for the whole
IMO-run fleet) drives §11 + §13. For the 44 IMO-owned stations all three collapse to
IMO. This needs to be represented in the map (owner→responsible; a separate
operator/data-center field, defaulting to IMO).

## Design

### TOS contacts survey (2026-07-01) — why the agency identity is curated, not from TOS
Authoritative pull via `get_contacts(entity_id)` over the 44 IMO-owned stations (not just
a keyword scan of the 1267-row flat list). **Only 3 distinct contacts are linked to IMO
GNSS stations:** the canonical org **`id=1256` "Veðurstofa Íslands"** (holds owner +
operator + data-owner roles fleet-wide; phone `5226000`, address `Bústaðarvegur 7-9, 105
Reykjavík`, **email empty**); `id=2489` Jarðvísindastofnun (a shared station);
`id=2483` Benedikt/AUT (RHOF operator artifact). The broader flat-list "IMO-ish" 19
(Starfsmenn Veðurstofunnar, snow observers, meteorologists) are on **non-GNSS entities**.
**No generic GNSS-team contact and no `gnss-epos@vedur.is` exists anywhere in TOS**, and
the English agency name + `IMO` abbreviation are not in TOS either.

⇒ The §11/§13 contact **identity** (English agency name, abbreviation, generic GNSS-team
name + email) must come from the curated `agencies.yaml`, **not** TOS contacts. TOS can
only *enrich* address/phone from the owner org record. Two options for the generic
contact source:
1. **Curated in `agencies.yaml`** (recommended interim) — no TOS write, single file.
2. **Add the GNSS-team email to TOS** — the minimal single-source fix is `PATCH contact
   #1256` (Veðurstofa Íslands) to set `email = gnss-epos@vedur.is` (currently empty); or
   create a dedicated `GNSS Operator / gnss-epos@vedur.is` contact + POC role join.
   Cleaner long-term, but a TOS write. Blocked/optional (user go-ahead).

### `role` is a UI-only enum — the API does NOT enforce it (probed live 2026-07-01)
The TOS web UI shows a fixed 9-value role dropdown (`Tegund`): *Athugunarmaður,
**Eigandi gagna** (data owner), **Eigandi stöðvar** (owner), Háloftaathugunarmaður,
**Rekstraraðili stöðvar** (operator), Snjóeftirlitsmaður, **Tengiliður stöðvar** (station
contact/POC), Tæknimaður, Umsjónarmaður*. **But the backend does not validate roles** — a
live `tos contact assign --role "data center"` succeeded (created rel 5176, since removed),
and an unknown role just echoes itself into `role_is` (no EN/IS mapping). Consequences:
- **Don't mint off-vocabulary roles** (e.g. a custom "data center") — no Icelandic term,
  invisible in the UI dropdown, semantically redundant with `Eigandi gagna`.
- **`tos contact assign` should validate `--role` against the known 9** and warn/refuse on
  a miss — else a typo silently creates an off-vocab relationship (follow-up, see below).

### The division of labour: TOS roles = *who*, `agencies.yaml` = *how to render*
TOS contact **roles** carry *which agency* plays each part; the contact **entity** can't
carry the sitelog presentation (no English-name / abbreviation / URL field, single Icelandic
`name`). So we split cleanly:

**`gps-config-data/agencies.yaml`** (deployed to `~/.config/gpsconfig/`, like `stations.cfg`;
loaded by `receivers.dissemination.agencies.AgencyResolver`, **built**) — keyed by the
Icelandic TOS org string, stores everything the TOS contact entity *cannot* (EN+IS names,
abbrevs, address, contact, email, url):
```yaml
agencies:
  "Veðurstofa Íslands":
    abbrev: IMO
    english_name: "Icelandic Meteorological Office / Infrastructure Division"
    observer: GNSSatIMO          # RINEX OBSERVER
    agency_label: "Vedurstofa Islands"   # RINEX AGENCY (decision: ASCII vs English)
    address: "Bústaðarvegur 7-9, 105 Reykjavík, Ísland"
    email: gnss-epos@vedur.is    # NOT in TOS (org record #1256 has empty email)
    url: "https://en.vedur.is"
  "Landmælingar Íslands":        # → NATT (fields pending NATT's reply)
    abbrev: NATT
    english_name: "(pending)"
    observer: GNSSatNATT
    agency_label: NATT
    ...
  "Jarðvísindastofnun Háskóla Íslands":   # → IES (strings TBD)
    abbrev: IES
    english_name: "Institute of Earth Sciences, University of Iceland"
    ...
defaults:
  operator_agency: "Veðurstofa Íslands"    # IMO — §11 POC default where no operator role
  data_center_agency: "Veðurstofa Íslands" # IMO — §13 primary DC default
  url: "https://en.vedur.is"               # global until a dedicated info page exists
```
Resolver `resolve_agency(org) -> AgencyInfo` in tostools, English-first; unknown org →
falls back to the IMO default (never emit a raw Icelandic org name to EPOS).

### Role-guided §11/§12/§13 + RINEX AGENCY resolution (with IMO default)
Read the station's TOS contact **roles**, map each to an agency, render via `agencies.yaml`:

| Output | TOS role that drives it | Fallback |
|---|---|---|
| RINEX `AGENCY` / `OBSERVER` | **owner** (Eigandi stöðvar) org | — (61/61 present) |
| Sitelog §12 Responsible Agency | **owner** org — emitted **only if ≠ §11** | (empty when = §11) |
| Sitelog §11 On-Site POC | — (always **IMO**: it runs the network + disseminates; TOS Rekstraraðili is upkeep, belongs in §12 via owner) | **IMO** always |
| Sitelog §13 **Primary** Data Center | **data-owner** (Eigandi gagna) org | **IMO** default |
| Sitelog §13 **Secondary** Data Center | **owner** org, only if ≠ primary | (empty for IMO-owned) |
| Sitelog §13 URL / §11 email | — (not in TOS) | `agencies.yaml` constant |

Rationale — owner/data-owner roles guide §12/§13 so TOS stays the source of truth
(auditable). §11 is deliberately **not** role-driven: IMO disseminates the data for every
EPOS station, so it is the on-site/data POC even where TOS names the owner as
Rekstraraðili (13/61 duplicate the owner org there; 47/61 have none — the role is
unreliable, see warning above). The **IMO default covers today's data-owner gaps**
(9/61), so **bulk-populating roles is NOT a prerequisite** — optional hygiene only.

### Issue 2 — RINEX OBSERVER/AGENCY from TOS (receivers)
1. `tos_access.make_session_provider` already attaches `marker`/`domes` to the session;
   also attach the resolved **agency** (from `contact.owner.organization` →
   `resolve_agency`). Empty/unknown org → fall back to the config default (IMO).
2. `convert.finalize_epos_header` writes `OBSERVER = agency.observer`,
   `AGENCY = agency.agency_label` from the session, config value only as fallback.
3. **Cache + reactive impact (ties into the history-fingerprint work):** OBSERVER/AGENCY
   is header-affecting, so it MUST enter `session_fingerprint` (convert cache key) **and**
   `history_fingerprint` (reactive detection) — otherwise a re-designation (Landmælingar→NATT)
   would not re-render/re-push the affected files. Add an `agency` field to the fingerprint
   field set. (Low risk: additive, invalidates cache once.)
4. QC gate: optionally verify `OBSERVER / AGENCY` matches the resolved agency
   (non-blocking at first — it's a formatting field, not safety-critical).

### Issue 1 — site-log completeness (tostools `core/site_log.py`)
1. Title: `{marker}{monument}{country}` (→ `RHOF00ISL`) + ` (site log v2.0)`; §1 label
   `Nine Character ID`. (Align the whole form to the current v2.0 `blank.log`.)
2. Instructions URL → `https://files.igs.org/pub/station/general/sitelog_instr.txt`.
3. §0: `Prepared by … (gnss-epos@vedur.is)`, `Report Type`, `Previous Site Log`
   (last emitted filename if tracked).
4. **Generate §11/§12/§13** per the **role-guided resolution table above** (owner role →
   §12/RINEX AGENCY; operator/Tengiliður or IMO-default → §11; data-owner or IMO-default →
   §13 primary; owner → §13 secondary if ≠ primary; URL/email from `agencies.yaml`).
   Also None-guard the DOMES-less path (HAMR/SKOG crash — see the second-round todo).
5. Reuse the shared resolver so header and sitelog never disagree on the agency.

### Follow-up — `tos contact assign` role validation
Since the API accepts any `--role` string (proven live), add a client-side guard: validate
`--role` against the 9 known enum values (owner/operator/data-owner/Tengiliður/…); warn or
refuse on a miss (with an override flag if a deliberate off-vocab role is ever needed).
Prevents silent typos creating off-vocabulary relationships that the UI can't show.

## Open / blocked decisions

- **NATT new name + contact** — pending NATT's reply (user contacting). §12/§11/§13 NATT
  fields, RINEX AGENCY label, and `agencies.yaml` English name are blocked on this.
- **3rd agency (Univ. of Iceland Earth Sciences, GRIV/TKJ2/TKJS)** — OBSERVER/AGENCY
  abbrev + English name undecided.
- **IMO RINEX AGENCY string** — user's first example kept `Vedurstofa Islands` (ASCII
  proper noun). Confirm whether EPOS/global prefers the English `Icelandic Meteorological
  Office` or the abbrev. (Sitelog §11/§13 already use the English name + `IMO`.)
- **Sitelog scope** — full v2.0 form alignment vs just adding §11-13 + the title fix.
- **tostools branch** — the laptop's editable `../tostools` is on
  `feat/firmware-chain-emit-triage` (@f1c8da8), not `master` (receivers pin 66d2ad2).
  Decide the base branch for the site_log.py work + whether it rides an existing branch.

## Impact on the current validation sweep

The in-flight May+June sweep uses the hard-coded `GNSSatIMO / Vedurstofa Islands` for
**all** stations, so the **17 non-IMO** stations (14 NATT + 3 Univ.) have the wrong
OBSERVER/AGENCY in both their pushed headers and site logs. Once implemented, those 17
must be re-run (cheap — cache hits for the obs; only header re-finalize + re-push differ,
which the agency-in-fingerprint change will trigger automatically via the reactive path).
