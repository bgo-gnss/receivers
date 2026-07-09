# Relocate gps_health PGDATA off `/var` onto `/mnt/data` (rek-d01)

**Status:** runbook — todo #62. **Downtime:** brief (DB stop → rsync ~13 GB → start;
scheduler + health monitoring blip and self-restart). **Privilege:** root/sudo on rek-d01.

## Why

On rek-d01 PostgreSQL 17 stores its cluster at the Debian default
`data_directory = /var/lib/postgresql/17/main`. `/var` here is a small dedicated
LVM volume (`vg_os-lv_var`, 24 GB). The `gps_health` DB is ~13 GB and **actively
growing** — the unified-file-index rollout adds `archive_catalog` rows
(~0.6 KB/row; a full deep-history `archive-index-backfill` is millions of rows),
and health monitoring writes continuously. During the 2026-07-08/09 deep-history
backfill `/var` fell to ~4.0 GB free (84%). Filling `/var` would take down the
**production** Postgres the scheduler and health monitoring depend on.

`/mnt/data` (`rek-vg-data_lv`, 1000 GB XFS, ~338 GB free) is the dedicated data
volume and the natural home. It is a **local LVM mount in `/etc/fstab`**
(`UUID=… /mnt/data xfs defaults 0 0`) so it mounts early at boot, before
`postgresql@17-main`.

`/var/lib/postgresql/17/main` being the default is not a misconfiguration — it is
simply the wrong volume *on this box* for a growing time-series DB.

## Two equivalent relocation methods

`data_directory` is already an explicit line in `postgresql.conf`, so either works:

- **A — repoint `data_directory` (native / preferred):** edit the config line to the
  new path. Cleanest; no dangling symlink.
- **B — symlink (keeps the `/var` path resolving):** leave the config, replace the
  old dir with a symlink to the new location.

Both leave PGDATA physically on `/mnt/data`. Pick one; the steps below show both.

## Guardrail (do this regardless of A or B)

The unit `postgresql@17-main.service` has
`RequiresMountsFor=/etc/postgresql/%I /var/lib/postgresql/%I` — it does **not**
reference `/mnt/data`. Add a drop-in so systemd guarantees the mount precedes
postgres at boot. Fail-safe even without it (a missing `PG_VERSION` makes
`pg_ctlcluster` refuse to start rather than corrupt), but make it explicit:

```
[Unit]
RequiresMountsFor=/mnt/data
```

## Preconditions

- `/mnt/data` free space ≫ DB size: ~338 GB free vs ~13 GB DB. ✅
- Preserve `postgres:postgres` ownership and `0700` mode on the copied dir, or PG
  refuses to start.
- Nothing else writing to `gps_health` during the copy (stop backfill + scheduler).

## Runbook

```bash
# 0. Stop everything writing to gps_health FIRST
ssh reknew 'pkill -f "archive-index-backfill"'                 # deep-history backfill (resumable)
ssh -l gpsops reknew 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user stop gps-receivers-scheduler'

# 1. Stop postgres (root)
sudo systemctl stop postgresql@17-main

# 2. Copy PGDATA to /mnt/data, preserving perms/owner/ACLs/xattrs
sudo mkdir -p /mnt/data/postgresql/17
sudo rsync -aHAX --info=progress2 /var/lib/postgresql/17/main/ /mnt/data/postgresql/17/main/
sudo chown -R postgres:postgres /mnt/data/postgresql
sudo chmod 700 /mnt/data/postgresql/17/main

# 3. Repoint — choose ONE:
#   A) native: edit /etc/postgresql/17/main/postgresql.conf
#        data_directory = '/mnt/data/postgresql/17/main'
#   B) symlink:
#        sudo mv /var/lib/postgresql/17/main /var/lib/postgresql/17/main.old
#        sudo ln -s /mnt/data/postgresql/17/main /var/lib/postgresql/17/main

# 4. Boot-ordering guardrail
sudo systemctl edit postgresql@17-main   # add [Unit] / RequiresMountsFor=/mnt/data
sudo systemctl daemon-reload

# 5. Start + verify
sudo systemctl start postgresql@17-main
psql -h localhost -d gps_health -tAc 'SHOW data_directory'    # → /mnt/data/postgresql/17/main
psql -h localhost -d gps_health -tAc 'SELECT count(*) FROM archive_catalog'
df -h /var /mnt/data

# 6. Restart consumers
ssh -l gpsops reknew 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start gps-receivers-scheduler'
#    resume backfill when ready (same throttled nohup cmd as before):
#    cd ~/git/receivers && nohup ionice -c3 nice -n19 receivers archive-index-backfill \
#      --root /mnt/rawgpsdata --dir /mnt/rawgpsdata --catalog-host localhost \
#      --dest-prefix '~/gpsdata' --sleep 0.05 --progress-every 20000 \
#      --report-unparsable ~/archive_deephist_unparsable_20260708.txt \
#      > ~/archive_deephist_20260708.out 2>&1 &

# 7. Reclaim /var AFTER a day or two of healthy running
#    A) rm the old contents of /var/lib/postgresql/17/main
#    B) sudo rm -rf /var/lib/postgresql/17/main.old
```

## Verification / rollback

- **Verify:** `SHOW data_directory` reports the `/mnt/data` path; row counts match
  pre-move; `df -h /var` shows the reclaimed space; scheduler + health monitoring
  resume writing (check `receivers.log`); a reboot still brings PG up cleanly
  (proves the `RequiresMountsFor` drop-in).
- **Rollback (before step 7 deletes anything):** stop PG, revert the config line
  (A) or restore `main.old` (B), start PG — the original `/var` PGDATA is untouched
  until the reclaim step, so rollback is trivial.

## Notes

- The old `/var` copy is retained until step 7 → the move is reversible by design;
  do not delete early.
- After the move, watch `/mnt/data` growth instead of `/var`; the deep-history
  backfill can add a few GB. `/mnt/data` at 338 GB free absorbs it comfortably.
