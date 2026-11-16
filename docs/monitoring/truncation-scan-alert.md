# Truncation scan alert (`rek-d01!Truncation scan`)

A raw `.gz` whose gzip stream ends early passes every cheap presence check the
pipeline makes (existence, size, gzip magic bytes). It is therefore never fetched
again, and its RINEX conversion fails on every attempt. Nothing else notices it:
KALT 2026-11-08 03:00 sat broken for 8 days, and the 2026-11-16 incident found
four such hours.

The scheduler job `truncation_scan` runs `receivers archive-truncation plan`
read-only once a week and pushes one passive check result to Icinga.

| Status | Meaning | What to do |
|---|---|---|
| OK | No truncated or corrupt raw files in the last 30 days | Nothing |
| WARNING | N broken raw files; the plan is saved | Review the plan named in the output, then `receivers archive-truncation apply <plan> --catalog-prod` (dry-run), then add `--yes` |
| UNKNOWN | The scan could not run: archive not mounted, database down, or an error | See `receivers.scheduler.truncation` in `receivers.log` |
| stale | No result for 8 days: the job did not run (scheduler down, job lost) | Check `systemctl --user status gps-receivers-scheduler` as gpsops |

It never repairs anything. Repair is the separate, reviewed `apply` step, which
replaces from a complete copy, re-downloads from the receiver, or salvages the
readable prefix. See `receivers archive-truncation --help`.

Perfdata: `broken`, `replace`, `refetch`, `deferred`, `salvage`, `unrecoverable`,
`files`, `probed`.

## Configuration (scheduler.yaml, in gps-config-data)

```yaml
truncation_scan:
  enabled: true
  schedule: "cron: 40 6 * * 1"   # Monday 06:40 (standard cron weekdays, 0/7 = Sunday)
  days: 30
  keep_plans: 8                  # plans under ~gpsops/.cache/gps_receivers/truncation_plans
  icinga: true                   # off by default, so a dev scheduler never reports as rek-d01
  icinga_host: rek-d01
  icinga_ttl: 691200             # 8 days: one weekly run plus a day
```

The scheduler registers the job only at startup, so restart the scheduler after
changing this section.

## Last mile — Icinga service object (IMO-IT)

The push only notifies once the service object exists on the Icinga server with
notifications enabled. Until then the push logs `Service not found` harmlessly
and the result still lands in `receivers.log`. Define a **passive** service whose
freshness matches the pushed TTL:

```icinga2
apply Service "Truncation scan" {
  import "generic-service"
  check_command         = "passive"
  enable_active_checks  = false
  enable_passive_checks = true
  check_interval        = 7d
  // freshness: alert if no fresh result arrives (matches the pushed ttl, 8 days)
  check_timeout         = 8d
  assign where host.name == "rek-d01.gps.vedur.is"
}
```

Attach the standard notification apply-rules used by the other GPS services. A
WARNING here is a data-integrity finding, not an outage, so office-hours
notification is enough.
