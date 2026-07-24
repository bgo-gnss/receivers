# Host disk + scheduler-liveness alert (server-side)

Durable, laptop-independent alerting for rek-d01's **OS/data volumes** and the
**"is the scheduler actually working?"** question. Fills the gap that let the
2026-07-21 outage run silent for ~2 days.

## Why this exists

On **2026-07-21 14:56** the small `/home` OS LV (82 G) filled to **100%**. Every
scheduler write then failed with `OSError: [Errno 28] No space left on device` —
including its own logging — yet `systemctl --user status` still reported the unit
**`active (running)`**. Downloads silently stopped until **2026-07-23**. Nothing
alerted because:

- `local_prune` only watches `data_prepath` (`/mnt/data`), never the OS volumes.
- The receiver-side disk checks watch the *stations*, not the server.
- There was no liveness signal at all — "active" is not "working".

## What it checks

Two independent signals, both filesystem-only (no DB, no log scraping):

| Signal | Source | WARN | CRIT |
|--------|--------|------|------|
| **Disk usage** (per mount) | `shutil.disk_usage()` | `--warn-pct` (85%) | `--crit-pct` (92%) |
| **Scheduler liveness** | newest mtime of the activity files | `--activity-warn-minutes` (20) | `--activity-crit-minutes` (60) |

**Default mounts:** `/ /home /var /mnt/data` (override with `--mount`, repeatable).
`/mnt/rawgpsdata` is deliberately excluded — it sits chronically ~96% and is
tracked by days-to-full forecast instead (see receivers-todos #73). A mount that
is absent/unreadable reports **UNKNOWN** (a vanished mount is itself a problem).

**Activity files** (default, newest mtime wins):
`~/.cache/gps_receivers/logs/download_audit.jsonl` and
`~/.cache/gps_receivers/heartbeat`. The audit trail exists today; the heartbeat is
written by the scheduler's `_schedule_heartbeat()` job (build step 3). During the
freeze both stop advancing while the process stays "active" — a stale age is the
exact **"active but wedged"** detector. Because the heartbeat lives on `/home`, a
full `/home` freezes it too, so the disk and liveness signals reinforce each other.

The overall result is **worst-of** the two, with a real WARN/CRIT outranking an
UNKNOWN.

## How it runs

`python -m receivers.monitoring.host_disk_check` is a plain Nagios plugin
(`LABEL - summary | perfdata`, exit 0/1/2/3). With `--icinga` it also pushes a
**passive check result** to Icinga:

- Service: **`rek-d01.gps.vedur.is!Host disk and liveness`**
- `--ttl 900` → Icinga marks the service **stale** after ~3 missed pushes (3× the
  5-min cadence), so the alert fires even if rek-d01 / the timer itself stops —
  not just on a CRIT push. This is the "who watches the watcher" cover, and the
  reason the check runs **out of process** from the scheduler (a wedged scheduler
  can't report on itself).

It is **stateless** — Icinga owns renotification/dedup and (via `ttl`) staleness.

## Deploy / manage (as gpsops on rek-d01, no sudo)

Installed by `install.sh` as a gpsops **user** unit (same model as
`gps-archive-sync-alert`); linger lets the timer fire with no active session.

```bash
# install (after `cd ~bgo/git/receivers && git pull && sudo bash deployment/server/install.sh`)
# — install.sh lays down and enables the timer automatically.

# inspect the timer:
systemctl --user list-timers gps-host-monitor.timer
journalctl --user-unit gps-host-monitor -n 20

# run the check by hand — no-sudo inspect path (Nagios output + exit code):
python -m receivers.monitoring.host_disk_check
# …with the Icinga push:
python -m receivers.monitoring.host_disk_check --icinga --icinga-host rek-d01 --ttl 900
```

Cadence: **every 5 min** (`OnUnitActiveSec=5min`). Disk fills are fast (this
incident went 81%→100% between human glances), so a tight cadence pages within
minutes of a real wedge.

## Last mile — Icinga service object (IMO-IT)

The push only notifies once the service object exists on the Icinga server with
notifications enabled. Until then the push logs `Service not found` harmlessly and
the status still lands in the journal. Define a **passive** service with a
**freshness threshold** so a stale/absent result alerts:

```icinga2
apply Service "Host disk and liveness" {
  import "generic-service"
  check_command         = "passive"
  enable_active_checks  = false
  enable_passive_checks = true
  check_interval        = 5m
  // freshness: alert if no fresh result arrives (matches the pushed --ttl 900)
  check_timeout         = 15m
  assign where host.name == "rek-d01.gps.vedur.is"
}
```

Attach the standard notification apply-rules (email/on-call) used by the other GPS
services. Tune thresholds via the systemd unit's `ExecStart` flags
(`--warn-pct` / `--crit-pct` / `--activity-*-minutes` / `--mount`) if the defaults
are too tight/loose.

## Relation to the build plan

This is **step 1–2** of the host-disk + liveness alerting work (receivers-todos
#74/#72). Remaining:

- **Step 3** — `_schedule_heartbeat()` in `bulk_scheduler.py` (1-min heartbeat
  file) + an `OSError [Errno 28]` escalation path that fires one best-effort
  Icinga CRITICAL over the network before logging becomes impossible.
- **Step 4** — fold the richer days-to-full forecast (`archive/prune.py:
  record_and_forecast`, already used by `local_prune`) into the disk signal so
  chronically-full-but-not-growing mounts alert on trajectory, not just percent.
