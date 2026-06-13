# Stream-capture rollout runbook (steps 1/4/5)

Brings the stream-capture acquisition mode (RTCM3 → BNC → RINEX) live on rek-d01.
The **code** is complete on PR #98 and inert by default (`stream_capture.enabled`
off). These are the operational steps to enable it — production-affecting, run them
deliberately.

**Prerequisites**
- Merge + deploy **receivers PR #96** (mosaic-X5) and **#98** (stream-capture).
- Merge **tostools PR #54** (IGS `mosaic-X5`/`TRM115000.10` names) and bump the
  receivers tostools dep — otherwise affected RINEX headers fall back to raw names.
- `ssh` aliases: `reknew` = `bgo@rek-d01` (code/install), `gpsops_rek` = `gpsops@rek-d01`.

The stream network is **74 stations** (the `rtcm2rinex-<SID>.bnc` configs on legacy
`rek.vedur.is`), ~73 of which already have a `.SKL` header there:

```
AFST AKUR ALHV ASVE AUSV BJTV BLAL BLON DYNC ELDC ELEY FAFC FEFC GEVK GFUM GIGO GJFV
GONH GRIC GRIV GRVC GRVM GRVV GUSK HAFC HAUC HEID HELF HERV HRAG HRIC HS02 HUSM HVAS
HVEL HVER ICEB ISAF ISAK KAST KEIC KIDC KLVC KRIV KRVC LAVI LISK MOHA MYVA NAMC NORV
NYLA ODDF OLAC ORFC RHOL SAFH SELF SENG SKA2 SKHA SKSH SUDV SUND SVIE SVIN TANC THNA
THOB THOC UNDH VMEY VMOS VOGC
```

> Note: several of these (ASVE, GRVC, ICEB, NORV, SUND, MYVA …) are currently
> `discontinued`/`inactive`/`passive` in stations.cfg — only flip *active* stations to
> `stream`. Reconcile this list against current station_status before step 4.

---

## Step 1 — deploy the BNC binary to gps-tools

install.sh already symlinks `gps-tools/bin/bnc → /usr/local/bin/bnc` (Phase 8) and
installs its X11/glib runtime libs (Phase 1). It just needs the binary in the repo.

```bash
# 1a. Copy the proven BNC 2.12.7 binary from legacy rek into the gps-tools checkout
#     (run where you have the gps-tools repo; e.g. the laptop or reknew):
scp gpsops@rek.vedur.is:/home/gpsops/bin/bnc  ~/git/gps-tools/bin/bnc
chmod 755 ~/git/gps-tools/bin/bnc

# 1b. Commit + push to gps-tools (binaries are committed there, like teqc/sbf2rin)
cd ~/git/gps-tools && git add bin/bnc \
  && git commit -m "Add BNC 2.12.7 (BKG Ntrip Client) for RTCM3 stream capture" \
  && git push

# 1c. Re-run install.sh on rek-d01 (idempotent) to pull gps-tools + symlink + libs
ssh reknew 'cd ~/git/receivers && git pull && sudo bash deployment/server/install.sh'

# 1d. Verify
ssh gpsops_rek 'bnc --version'        # expect: BNC 2.12.x
```

---

## Step 4 — set `acquisition_mode = stream` (via gps-config-data)

`stations.cfg` is owned by the **gps-config-data** repo (never edit the deployed copy
directly — the sync timer propagates within ~10 min). Add `acquisition_mode = stream`
to each *active* stream station's section.

```bash
cd ~/git/gps-config-data        # $GPS_CONFIG_DATA_REPO

# Helper: add the field to the [SID] sections that don't already have it.
python3 - <<'PY'
import re, pathlib
STREAM = """AFST AKUR ALHV AUSV BJTV BLAL BLON DYNC ELDC ELEY FAFC FEFC GEVK GFUM GIGO
GJFV GONH GRIC GRIV GRVM GRVV GUSK HAFC HAUC HEID HELF HERV HRAG HRIC HS02 HUSM HVAS
HVEL HVER ISAF ISAK KAST KEIC KIDC KLVC KRIV KRVC LAVI LISK MOHA NAMC NYLA ODDF OLAC
ORFC RHOL SAFH SELF SENG SKA2 SKHA SKSH SUDV SVIE SVIN TANC THNA THOB THOC UNDH VMEY
VMOS VOGC""".split()   # discontinued/inactive removed — re-check before running
p = pathlib.Path("stations.cfg"); lines = p.read_text().splitlines()
out, cur, added = [], None, []
for ln in lines:
    m = re.match(r"\[([0-9A-Z]{4})\]", ln)
    if m: cur = m.group(1)
    out.append(ln)
    if m and cur in STREAM:
        out.append("acquisition_mode = stream")
        added.append(cur)
p.write_text("\n".join(out) + "\n")
print(f"added acquisition_mode=stream to {len(added)} stations")
PY

# Review the diff carefully, then commit + push; sync timer deploys it.
git diff stations.cfg
git add stations.cfg && git commit -m "stream: acquisition_mode=stream for stream-captured stations" && git push
```

**Effect:** the download scheduler now *skips* these stations (handled by the stream
pipeline; gap-filler still downloads on demand). Health monitoring is unchanged.

---

## Step 5 — migrate headers, enable, validate

```bash
# 5a. (Optional) migrate existing .SKL headers from legacy rek so they're not
#     rebuilt from scratch. Otherwise the config-refresh job self-seeds them from
#     stations.cfg position + TOS (base-skeleton generator).
ssh gpsops_rek 'mkdir -p ~/tmp/RT-rinex'
rsync -av --include='*/' --include='*.SKL' --exclude='*' \
  gpsops@rek.vedur.is:/home/gpsops/tmp/RT-rinex/ \
  gpsops@rek-d01.vedur.is:/home/gpsops/tmp/RT-rinex/

# 5b. Caster credentials + (optional) path overrides in the DEPLOYED receivers.cfg
#     [streaming] section (via gps-config-data — NOT the repo template):
#        caster_user = <ntrip_user>
#        caster_password = <ntrip_password>

# 5c. Enable in scheduler.yaml (gps-config-data):  stream_capture: { enabled: true }
#     Optional: supervise_schedule (10m), pipeline_schedule (:20),
#     config_refresh_schedule (06:00).

# 5d. Dry first cycle BEFORE the scheduler picks it up — run the jobs by hand:
ssh gpsops_rek '/home/bgo/git/receivers/venv/bin/python3 - <<PY
from receivers.scheduling.stream_scheduler import (
    _run_stream_config_refresh_job, _run_stream_supervise_job, _run_stream_pipeline_job,
)
_run_stream_config_refresh_job()   # writes .bnc + .SKL (built/refreshed from TOS)
_run_stream_supervise_job()        # starts BNC daemons
import time; time.sleep(120)        # let BNC produce a first hourly RINEX
_run_stream_pipeline_job()         # ingest -> downsample -> gap-fill
PY'

# 5e. Verify (pick a station, e.g. GONH):
ssh gpsops_rek 'ls ~/.config/BKG/rtcm2rinex-GONH.bnc; cat ~/tmp/RT-rinex/GONH/GONH.SKL'
ssh gpsops_rek 'pgrep -af "bnc --conf" | head'                 # daemons alive
ssh gpsops_rek 'ls /mnt/data/gpsdata/$(date +%Y)/$(date +%b|tr A-Z a-z)/GONH/1Hz_1hr/rinex/ | tail'
ssh gpsops_rek 'receivers health-query "SELECT sid,session_type,status,max(file_date) FROM file_tracking WHERE sid=\$\$GONH\$\$ GROUP BY 1,2,3"'
```

**⚠️ Validate the toolchain on first run** — the downsample/ingest tool flags and the
final `.Z` compression format have not been exercised against the real
teqc/CRX2RNX/RNX2CRX + archive yet (flagged in `downsample.py`/`ingest.py`). Confirm a
produced `15s_24hr` file opens correctly before trusting the daily product.

## Rollback

- Set `stream_capture.enabled: false` in scheduler.yaml (jobs stop next reload).
- Remove `acquisition_mode = stream` from stations.cfg to return stations to the
  download scheduler.
- `ssh gpsops_rek 'pkill -f "bnc --conf"'` to stop BNC daemons.
