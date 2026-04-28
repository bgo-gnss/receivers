# Config Data Flow — Architecture Vision

## Current state (Layer 0 + 1)

```
  gps-config-data (git repo, git.vedur.is)
        │
        │  gps-config-sync.timer (every 10 min)
        │  fast-forward pull → copy safe files
        ▼
  /home/gpsops/.config/gpsconfig/  ← local, runtime-independent
        │  stations.cfg
        │  receivers.cfg
        │  scheduler.yaml
        │  icinga.cfg
        │  database.cfg  (never synced — local credentials)
        │
        ▼
  receivers scheduler
        │  reads config at startup + watches stations.cfg mtime (5 min)
        │  hot-reloads without restart when file changes
        ▼
  GPS hardware (173 stations)
        │  FTP/HTTP/TCP download
        │  extracts receiver identity (serial, firmware, model)
        ▼
  receiver_identity persisted to stations.cfg
  health metrics written to PostgreSQL (gps_health)
  Grafana shows live status
```

**Design principle**: receivers is fully independent at Layer 0. Drop a stations.cfg next to it
and it runs anywhere — no git, no TOS, no network dependencies at runtime. The sync timer is
optional ops infrastructure that keeps Layer 0 current; it is not required to operate.


## Future state (Layer 2 — tostools + TOS)

```
  TOS Database (authoritative source for station metadata)
  │  receiver serial, antenna type/serial/height, coordinates,
  │  station status, ownership, installation dates
  │
  │  tostools generates config files from TOS
  ▼
  gps-config-data (git repo) ◄──────── (candidate diff shown to operator)
        │                                       │
        │  Layer 1 sync (unchanged)              │ operator approves
        ▼                                       │ correction in Grafana
  /home/gpsops/.config/gpsconfig/               │
        │                                       │
        ▼                                       │
  receivers                                     │
        │  downloads SBF → converts RINEX       │
        │  extracts receiver identity            │
        ▼                                       │
  tostools RINEX QC (co-located, same server)   │
        │  validates RINEX headers               │
        │  against stations.cfg                  │
        │  flags discrepancies                   │
        └───────────────────────────────────────►┘
                  discrepancy → TOS correction
                  (operator-approved, never automatic)
```


## Trust migration path

### Phase A — today
`gps-config-data/stations.cfg` (the git repo) is the single source of truth. All edits are
made there — never directly on the server. The local `/home/gpsops/.config/gpsconfig/stations.cfg`
is a deployed copy kept current by the sync timer.

Config is continuously validated by hardware feedback: receivers reports actual serial/firmware,
which can be checked against the config.

### Phase B — tostools introduction
1. tostools reads TOS and generates a *candidate* stations.cfg
2. Diff candidate against current hand-maintained stations.cfg
3. Surface all discrepancies: "TOS says antenna is SEPCHOKE_B3E6; stations.cfg says
   SEPPOLANT_X_MF — which is correct?"
4. Operator resolves each discrepancy by inspecting site records / visiting Grafana
5. Repeat until candidate and current agree

At the end of Phase B, TOS and stations.cfg are in agreement. No automatic writes have
happened — all corrections were operator-approved.

### Phase C — steady state
tostools generates stations.cfg from TOS on a schedule (or on TOS change event). The
generated file is committed to gps-config-data and distributed via Layer 1 sync.

receivers continues to validate hardware identity. Discrepancies between what a receiver
reports and what stations.cfg says are:
- Flagged in the Grafana interface ("receiver THOB reports serial 3028499, config says 3028400")
- Presented as a suggested TOS correction with supporting evidence
- Approved or rejected by the operator before any write to TOS
- Automatic writes to TOS are **never** implemented — the human-in-the-loop is permanent

### Phase D — mature
TOS is the trusted source because it has been beaten into agreement by the comparison
process and validated continuously by hardware feedback. stations.cfg becomes an artefact
of TOS, not a primary source.

The Grafana interface serves as the operational dashboard for the feedback loop:
flag raised → operator reviews hardware history → approves correction → TOS updated →
tostools regenerates → sync distributes → receivers validates → flag cleared.


## tostools co-location rationale

tostools RINEX QC runs on the same server as receivers (rek-d01) because:

1. **Freshness**: RINEX files are validated immediately after conversion, before archiving.
   Catching a bad antenna type in the header at this point prevents it from propagating
   into the long-term archive.

2. **Context**: The receiver identity just pulled from hardware is available in the same
   process context. "Header says SEPCHOKE, hardware reports SEPPOLANT — flag it."

3. **No data movement**: QC happens where the data lives. No need to ship files to a QC
   server and back.

4. **Correction loop closure**: The flag can be written to the same PostgreSQL database
   that Grafana reads, making it immediately visible to operators without a separate
   notification system.


## What is never distributed through git

`database.cfg` contains:
- PostgreSQL connection credentials for the local gps_health database
- Mirror server credentials for replication

These are server-specific secrets. They are deployed once by install.sh and never touched
by the sync timer. If credentials need rotating, edit the file directly on the server.


## Related files

- `deployment/server/sync-config.sh` — the Layer 1 sync script
- `deployment/systemd/gps-config-sync.{service,timer}` — systemd units for Layer 1
- `src/receivers/scheduling/bulk_scheduler.py` — config watcher (`_check_config_changes`,
  lines ~1213-1268) that hot-reloads stations.cfg on mtime change
- `deployment/server/install.sh` — Phase 5 (initial config deploy) + Phase 10 (timer install)
