# GAMIT host config sync (`sync-config.sh`)

Deploys `gamit/station.info.sopac.apr05` from **gps-config-data** to the GAMIT host
that reads it. Counterpart to `deployment/server/sync-config.sh`, which does the same
job for rek-d01's own configs — same convergent shape, different destination.

| host | status | destination |
|---|---|---|
| **okada** | deploy now | `/D/DATABASE/GAMIT/station.info.sopac.apr05` |
| gamit-p01 | **later**, at cutover | same path |

## Why it runs ON the GAMIT host

Not from rek-d01 and not pushed from a workstation:

* **rek-d01 cannot reach the GAMIT hosts** — `ssh gpsops@okada` fails on host-key
  verification, and opening that trust edge would give a production box write access
  to the GAMIT hosts for no benefit.
* **The host needs nothing inbound.** It pulls, and writes its own local path — the
  same architecture as rek-d01's config sync.
* **No deploy key is needed.** gps-config-data is internal/public, so an anonymous
  **HTTPS** clone works. (SSH from okada fails on host-key verification; HTTPS does not.)

## Install (okada)

```bash
# 1. the script — any stable path the cron user can execute
install -m 0755 sync-config.sh /home/gpsops/bin/sync-config.sh

# 2. one-time clone + first deploy
/home/gpsops/bin/sync-config.sh --init-clone

# 3. schedule it — see the cron note below
```

**Cron, not systemd.** okada has no systemd user timers and linger is unset; it
schedules with **cron**, and its crontab is centrally managed —

```text
# Please only edit the original file on
#      mogi:/crontab/crontab.file  and then install it.
```

So the entry must be added to **`mogi:/crontab/crontab.file`**, not edited locally on
okada:

```cron
# Deploy GAMIT station.info from gps-config-data (every 15 min)
*/15 * * * * /home/gpsops/bin/sync-config.sh --quiet
```

## Modes

| command | behaviour |
|---|---|
| `sync-config.sh` | converge: fetch, validate, deploy if changed |
| `sync-config.sh --quiet` | as above, silent unless it changed something or failed |
| `sync-config.sh --dry-run` | report what would change; **writes nothing** |
| `sync-config.sh --verify` | exit 1 if deployed ≠ repo. For monitoring — never writes |
| `sync-config.sh --init-clone` | create the clone if absent, then converge |

`REPO`, `REPO_URL`, `BRANCH`, `SRC_REL`, `DST` are env-overridable (that is how the
script is tested locally against a throwaway repo and a temp destination).

## Safety properties

* **git/network failure** → exit silently; the deployed file is untouched.
* **candidate fails validation** → the current file stays; the run errors loudly.
* **already converged** (`cmp` equal) → no write, so **no mtime bump**.
* **atomic swap** — temp file in the same directory, then `mv` (a rename), so nothing
  ever reads a torn 4 MB file mid-write.
* **backup before every swap** (`station.info.sopac.apr05.backup.<UTC>`, newest 10
  kept), replacing okada's ad-hoc litter (`.apr05~`, `.apr05~~` from 2018, `.bak_*`).
* **post-swap proof** — re-reads the file and compares bytes to the repo; a mismatch
  is a hard error.
* **breadcrumb** — appends `UTC commit size sha256` to `$DST_DIR/.station.info.deployed`
  so any GAMIT result can be traced back to a metadata revision.

### What the guard is, and is not

It proves the **checkout is intact**: file present, ≥1 MB, a `*SITE` header in the
first five lines, and size/line-count within 25 %/20 % of the current file. That is
what corruption or a bad transfer actually looks like on a host.

It deliberately **does not** re-implement semantic validation (fixed-column
round-trip, date ordering/non-overlap, valid `HtCod`). That belongs at **commit
time**, where tostools and the human review live; a second parser here would drift
from the first, which is the failure mode this whole migration exists to remove.

## Verified behaviour (2026-10-13)

Exercised against a throwaway repo and a temp destination, not just reasoned about:

| case | result |
|---|---|
| dry-run with no destination | reports, writes nothing ✓ |
| first deploy | deploys, writes breadcrumb ✓ |
| re-run | no-op, **mtime unchanged** ✓ |
| `--verify` in sync | exit 0 ✓ |
| tampered deployed file | `--verify` exit 1; redeploy backs up and repairs ✓ |
| truncated candidate (8.9 kB) | refused, **deployed file untouched** ✓ |
| candidate with no `*SITE` header | refused ✓ |
| candidate shrunk 28 % | `size moved 28% in one tick — refusing` ✓ |
| `--init-clone` from the public URL | clones, deploys, byte-identical to the repo ✓ |
| second run after clone | no-op, mtime unchanged ✓ |

## Don'ts

* **Never edit the deployed file on the host** — the next tick overwrites it.
* **Do not check out or edit the clone** (`~/git/gps-config-data` on the host); it is a
  deploy artefact and is `reset --hard` to `origin/$BRANCH` every run. Local
  modifications are warned about and then discarded.
* **Do not enable `xstinfo`** on a GAMIT host without deciding where its output goes —
  it can write station.info from RINEX headers, i.e. outside this repo and outside review.
* Do not deploy to gamit-p01 until the cutover is agreed.
