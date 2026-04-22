# Receivers — production roadmap

Plan to take the `receivers` package from **"health-only pilot on reknew"** (phase 1 — complete as of 2026-04-21) to **"fully operational + easily reproducible on a fresh production server"**.

**Status**: PR #7 (merged 2026-04-21) fixed the bootstrap blockers — URL-pinned deps, hatch direct-references, Python floor, ownership model, cache/log perms, docker group, systemd watchdog kill-loop, `--max-workers` override, mirror password injection. Health-monitoring for 178 stations now lands end-to-end in both local DB and the pgdev mirror.

This file is a living in-repo companion to the private-vault version at `2.Areas/VI_GPS_servers/1776795084-reknew-server-setup.md` + `1.Projects/Work_GPS_Receivers/1776795087-production-roadmap.md`, which carry additional operator context that doesn't belong in a public repo.

---

## M0 — PolaRX5 firmware update (prerequisite, blocks M1.2 / M1.3 at scale)

A new PolaRX5 firmware version introduces significant changes that require code updates in the `receivers` package before 15s_24hr and 1Hz_1hr downloads can be trusted across all ~90 PolaRX5 stations.

| Item | Notes |
|---|---|
| Identify exactly what changed in the new firmware | Check Septentrio release notes; diff against current SBF parser and FTP/TCP protocol assumptions |
| Update `polarx5_tcp_extractor.py` and any affected SBF parsers | Treat as a feature branch — `feat/polarx5-firmware-vX` — not a hotfix |
| Update `rxtools_extractor.py` if RxTools CSV output format changed | Cross-check `bin2asc` field names against the new firmware |
| Update receiver documentation | Protocol assumptions, SBF block versions, tested firmware versions |
| Validate on one station before rolling to all PolaRX5 stations | Use ELDC or THOB as canary |

**Branch**: `feat/polarx5-firmware-vX` → PR → merge → `git pull` on reknew before enabling M1.2/M1.3.

---

## M1 — Functional rollout (sequential, gated on validation)

Each step: flip `enabled: true` in `scheduler.yaml`, restart scheduler, watch 24-48h, address anything that surfaces before advancing.

| Step | Flip | Validation signal | Watch for |
|---|---|---|---|
| **1.1** | `sessions.status_1hr.enabled` | hourly files in `/mnt/data/gpsdata/*/status_1hr/raw/`, rows in `file_tracking` | FTP auth regressions, disk fill rate |
| **1.2** | `sessions.15s_24hr.enabled` + `archive_reconciler.enabled` | daily 24h files @ 00:01 UTC + RINEX conversion via `sbf2rin` (Septentrio) and `trm2rinex` (Trimble) | SBF→RINEX failures, Trimble `.T02` conversion gaps |
| **1.3** | `sessions.1Hz_1hr.enabled` | hourly 1Hz files (largest bandwidth: ~1GB/day/station × ~90 polarx5 ≈ 90GB/day) | network saturation, disk headroom, worker pool capacity |
| **1.4** | `gap_detection.enabled`, `integrity_checker.enabled` | missing-file reports, integrity `suspect` rows | false positives, DB load from integrity scans |

## M2 — Discovered bugs + cleanups (parallelizable with M1)

| Item | Effort | Priority |
|---|---|---|
| 6 stations missing `router_ip` / `receiver_type` in `stations.cfg` (silent today) | S | **High** |
| `rnx2crx` case-sensitivity — add lowercase symlink in `install.sh` Phase 8 loop | S | Medium |
| Trimble `.T02` conversion failures on some stations — diagnose which files, which firmware, trm2rinex version | M | Medium |
| `mdb2rinex` (Leica) binary missing — add to `gps-tools` or document the G10 as conversion-limited | M | Low |
| Persisted APScheduler jobs surviving `enabled: false` — auto-reconcile on startup OR document `--wipe-all` as the disable path | S-M | Medium |
| Symmetric unit test for the mirror password fix shipped in PR #7 (`database_factory.py:_get_mirror_connection`) | S | Low |

## M3 — Infrastructure dependencies on other teams

| Item | Blocker | Unblocks |
|---|---|---|
| Create `gps-tools` repo in `gps/` team namespace on git.vedur.is | IT | Remove `TODO: move to gps/` comment in `install.sh`; repo stops being personal |
| Create `gpsops` PostgreSQL role on `pgdev.vedur.is` | IT / DBA | `mirror_user = gpsops` in database.cfg; admin creds stop being needed on reknew |
| Pre-provision Icinga services for all 178 stations (or adjust scheduler to stop warning when Icinga lacks the service) | Icinga admin | Quiets the log |
| Scheduler `sd_notify` integration — implement keepalive in `bulk_scheduler` main loop, flip `Type=notify` + re-enable `WatchdogSec` in service file | Code change, small | Real watchdog protection instead of the current disabled state |

## M4 — Production-grade operability

| Item | Why |
|---|---|
| **DB backup cron** — `pg_dump gps_health` → timestamped off-server archive | DB loss = reset network history |
| **Scheduler self-monitoring dashboard** — "time since last sweep", "stations silent ≥2h", "mirror lag" | Detects silent regressions |
| **Alerting rules in Grafana** — scheduler down, mirror lag, station offline >X min, disk fill rate | Pages the right person |
| **Log archival** — `receivers.log` rotates at 20MB×3 locally; move rotated chunks off-server | Cheap insurance against ephemeral VM disk loss |
| **Worker count tuning** — measure actual sweep duration under full M1 load, pick `max_workers` accordingly | Don't cargo-cult the 200 |
| **systemd unit hardening review** — `ProtectSystem=strict` + write paths + `NoNewPrivileges` already set; audit for anything else | Low-effort wins |

## M5 — Fresh-install polish + handoff

| Item | Why |
|---|---|
| **CI smoke test** — spin up a fresh Ubuntu VM in CI, run `install.sh --dev --skip-tools`, verify all phases green | Prevents regression of the PR #7 fixes |
| **Production runbook** — deploy, upgrade, rollback, troubleshoot; common failure → fix matrix | Operators need it |
| **Upgrade procedure** for `gtimes` / `gps_parser` / `tostools` sibling packages — bump tag in `pyproject.toml`, test on dev, roll to prod | Sibling-package version hygiene |
| **`stations.cfg` source-of-truth** — clarify flow from `gps-config-data` repo → `/home/gpsops/.config/gpsconfig/stations.cfg` | Config management is currently fuzzy |
| **Production host selection** — pick the `-p01` replacement for the `rek` line, document it | Need a target |

## Suggested execution order

1. **M1.1** (`status_1hr`) — low bandwidth, low risk, confirms download pipeline end-to-end
2. **M2 quick wins** in parallel — fix the 6 station configs, add the `rnx2crx` symlink, open IT tickets for M3.1 and M3.2
3. **M1.2** (`15s_24hr`) — once M1.1 stable; surfaces Trimble `.T02` issues more broadly
4. **M3.3** (`sd_notify`) — small code change, substantial operational win
5. **M1.3** (`1Hz_1hr`) — after M1.2 stable AND disk+network headroom measured
6. **M4** in parallel with the tail of M1
7. **M1.4** (maintenance jobs) — last, since they depend on everything else being populated
8. **M5** — once M1-M4 are stable

## Working procedure

### Git workflow

The laptop is the development environment. Reknew is always a consumer — it never originates changes.

**Most changes — feature branch:**
```
main → feat/<topic> or fix/<topic>  (laptop)
       ↓ develop + test locally
       ↓ PR → review → merge to main
       ↓ git pull on reknew
```

**Minor changes — direct to main:**
```
fix locally on laptop → commit directly to main → push → git pull on reknew
```

**Directly on reknew**: only in rare/emergency cases. If it happens, the change must be committed back immediately — never leave reknew ahead of the repo.

### Which repo

| Change | Repo |
|---|---|
| Code, tests, docs, systemd units | `receivers` (this repo) |
| `scheduler.yaml`, `stations.cfg`, `database.cfg` | `gps-config-data` |

Same rules apply to both repos — git is always the source of truth.

### When to use a PR vs. direct commit to main

| Use a PR | Commit directly to main |
|---|---|
| New feature, significant refactor | Single-line fix, typo, comment |
| Anything touching the download pipeline or DB writers | Config-only tweak with no logic change |
| M0 firmware work, M3 sd_notify integration | Trivial doc addition |
| Changes that need local testing before reknew sees them | Changes that are obviously safe |

### Documentation split

| What to document | Where |
|---|---|
| What changed and how to reproduce | `docs/` in repo; update roadmap milestone status |
| Why a decision was made, alternatives rejected | bgovault private companion note (`1776795087-production-roadmap.md`) |
| Server-specific notes, timestamps, operator diary | bgovault companion note |
| Significant architectural learnings (non-obvious, reusable) | `CLAUDE.md` + memory files |

### Branch naming

| Type | Pattern |
|---|---|
| New feature or milestone work | `feat/<topic>` |
| Bug fix | `fix/<topic>` |
| Docs only | `docs/<topic>` |

---

## Lessons from the 2026-04-20/21 bootstrap

All fixed in PR #7 (merged). Captured here for future-operators hitting the same issues on a different host:

1. **`WatchdogSec` + `Type=simple` = kill loop.** Scheduler never survives past first 5-min tick. Fix: disable until `sd_notify` integration.
2. **CLI flags overriding yaml.** `--max-workers 5` in the systemd `ExecStart` silently overrode `scheduler.yaml`'s 200, capping parallelism at 5. Single source of truth wins.
3. **`/home/<service-user>/.cache` is mode 700 by default.** Blocks admin read without a bunch of sudo. Fix via chgrp to service group, chmod 750, recursive g+rX, SGID on dirs.
4. **Config ownership hardcoded admin.** `bgo:gpsops` baked bgo into filesystem layout. Better: `gpsops:gpsops 660` — admin in group gets write via group membership, software doesn't assume admin identity.
5. **Mirror password injection bug.** `database_factory._get_mirror_connection` spread primary `params` (including empty password) into mirror params, defeating `.pgpass`. Fix: drop `password` when `mirror_user != primary_user`.
6. **Hatchling direct-references.** `pyproject.toml` git-URL deps require explicit opt-in: `[tool.hatch.metadata] allow-direct-references = true`.
7. **rxtools ships libraries alongside binaries.** `rxtools/bin/` holds both `.so` files and executables; `ld.so.conf.d` must point at `bin/`, not a nonexistent `lib/`.
8. **APScheduler jobstore survives config flips.** Disabling a session in yaml doesn't remove already-registered jobs from `scheduler.db`. Either code auto-reconciles or operators use `--wipe-all` when changing topology.

---

**Updated**: 2026-04-22
**Private-vault companion**: `bgovault/1.Projects/Work_GPS_Receivers/1776795087-production-roadmap.md`
