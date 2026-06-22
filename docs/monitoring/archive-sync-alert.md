# Archive-sync health alert (server-side)

Durable, laptop-independent alerting for the rek-d01 → rawdata (ananas) long-term
archive pipeline. Answers "tell me if the archive sync breaks while I'm away."

## What it checks

`receivers.monitoring.archive_sync_check` evaluates two DB-queryable signals from
`gps_health` (no log scraping) and returns a Nagios status (OK/WARN/CRIT/UNKNOWN):

| Signal | Source | WARN | CRIT |
|--------|--------|------|------|
| **Sync freshness** | `sync_state.last_success_ts` age | > `max_age_minutes` (120) | > 2× (240 min), or never synced |
| **Missing 15s dailies** (yesterday) | `file_tracking` `15s_24hr` `status='missing'` | ≥ `missing_15s_warn` (5) | ≥ `missing_15s_crit` (15) |

Freshness is the headline: it catches the sync silently stopping for *any* reason
— scheduler down, rsync/ssh broken (the rc=255 class), watermark stuck. Missing
thresholds are **counts, not a station allowlist** (which drifts); a handful is
normal (known-bad + transient), a spike is real.

**Not checked here:** archive corruption. There is no DB flag for it yet (the
read-back verify logs `ARCHIVE CORRUPT` but doesn't persist a column). Corruption
rides the verify pass's own logging until a findings table exists (tracked todo).

## How it runs

`gps-archive-sync-alert.timer` (a **gpsops `systemctl --user` unit** — same model
as the scheduler, no sudo to manage) fires every 15 min and runs the check with
`--icinga`, pushing a **passive check result** to Icinga:

- Service: **`rek-d01.gps.vedur.is!Archive sync`**
- `--ttl 3600` → Icinga marks the service **stale** after ~4 missed pushes, so the
  alert fires even if rek-d01 / the timer itself stops (not just on a CRIT push).

It is independent of the scheduler process, so a scheduler-down still produces a
report (stale sync → CRIT) rather than silence.

Installed/enabled by `deployment/server/install.sh` (idempotent, as a gpsops user
unit). To deploy or manage **as gpsops on rek-d01, no sudo**:

```bash
# install (after `cd ~bgo/git/receivers && git pull`):
mkdir -p ~/.config/systemd/user
cp /home/bgo/git/receivers/deployment/systemd/gps-archive-sync-alert.{service,timer} \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gps-archive-sync-alert.timer

# inspect:
systemctl --user list-timers gps-archive-sync-alert.timer
journalctl --user-unit gps-archive-sync-alert -n 20

# run the check by hand — Nagios output + exit code, no push:
/home/bgo/git/receivers/venv/bin/python -m receivers.monitoring.archive_sync_check
# …with the Icinga push:
… archive_sync_check --icinga --icinga-host rek-d01 --ttl 3600
```

Linger is already enabled for gpsops (the scheduler relies on it), so the timer
fires without an active session.

## Last mile — Icinga service object (IMO-IT)

The push only notifies once the service object exists on the Icinga server with
notifications enabled. Until then the push logs `Service not found` harmlessly and
the status still lands in the journal. Define a **passive** service with a
**freshness threshold** so a stale/absent result alerts:

```icinga2
apply Service "Archive sync" {
  import "generic-service"
  check_command         = "passive"
  enable_active_checks  = false
  enable_passive_checks = true
  check_interval        = 15m
  // freshness: alert if no fresh result arrives (matches the pushed --ttl 3600)
  check_timeout         = 1h
  assign where host.name == "rek-d01.gps.vedur.is"
}
```

Attach the standard notification apply-rules (email/on-call) used by the other GPS
services. Tune `max_age_minutes` / `missing_15s_*` via the systemd unit's
`ExecStart` flags if the defaults are too tight/loose for the fleet.
