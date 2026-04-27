# PolaRX5 fw 5.7.0 — Migration Guide

This document covers what changed in firmware 5.7.0, how to provision a fresh receiver,
and how the `receivers` package must be updated to work with the new firmware.

**GJAC** is the canary station — it is the first in the IMO network with fw 5.7.0 installed.
All testing and validation happens there before rollout to the full ~90 PolaRX5 fleet.

---

## What Changed in fw 5.7.0

### Driver: EU Radio Equipment Directive (EU RED 2014/53/EU)

Firmware 5.7.0 is a security hardening release required for EU market compliance.
The key mandate is eliminating insecure-by-default configurations.

### Breaking changes

| Area | Before (≤5.5.0) | After (fw 5.7.0) |
|------|-----------------|-----------------|
| TCP authentication | None — prompt accepts commands immediately | **Mandatory** — `login` must be first command |
| Default user | Anonymous `User`-level access on IP connections | **No default user** — factory bootstrap required |
| FTP | Enabled by default | **Disabled by default** — must use `setIPServices` |
| HTTP/HTTPS | HTTP only (port 80) | Both enabled; HTTP **redirects to HTTPS** by default |
| SSH key auth | Not supported | SSH public key per user for SFTP/rsync (optional) |

### Non-breaking changes (code-relevant)

- `login` on receivers running ≤5.5.0 firmware returns `$E: Invalid command!` (the command did not exist). The updated `polarx5_tcp_extractor.py` treats this response as "old firmware — proceed unauthenticated", so it works against both firmware versions.
- SBF block format and block IDs are unchanged between 5.5.0 and 5.7.0 for the blocks we parse (4101, 4014, 4059, 4082).
- `bin2asc` (RxTools) output format is unchanged for these blocks — `rxtools_extractor.py` requires no changes.
- **Getter abbreviations removed**: `gsis` and `gshs` (short forms of `getIPServices` / `getHttpsSettings`)
  return `$R? Invalid command!` on fw 5.7.0. Use the full command names. Setter abbreviations (`sis`,
  `shs`, `sual`, `eccf`) still work. Confirmed on GJAC (2026-04-24).

---

## Provisioning a New fw 5.7.0 Receiver

These steps must be performed once per receiver, either via the web interface or via TCP.
After provisioning, the `receivers` package handles everything programmatically.

### Prerequisites

- TCP access to the receiver on port 28784 (plaintext)
- Factory credentials: configured in `receivers.cfg [polarx5]` as `factory_username` / `factory_password`.
  Septentrio-wide defaults: `RxAdmin` / `S3pt3ntr10` (documented in Septentrio Reference Guide § 3.5).
  Override in `receivers.cfg` only if your fleet ships with non-standard factory credentials.
- The receiver must have **no user accounts** (fresh install or post-factory-reset state)

### Step-by-step provisioning sequence

```
# 1. Connect to TCP port 28784 (nc, telnet, or the provisioning tool)
#    You will see the prompt:  IP10>

# 2. Bootstrap — create the operations account using factory credentials
#    This creates gpsops as User1 AND logs in as gpsops in one step.
login, gpsops, <your_password>, RxAdmin, S3pt3ntr10

# 3. Add the admin account (while logged in as gpsops)
setUserAccessLevel, User2, admin, <your_admin_password>, User

# 4. (Optional) Add SSH public key to gpsops for passwordless SFTP/rsync
#    Key must be ECDSA/Ed25519/RSA in RFC 4716 base64, max 232 chars
setUserAccessLevel, User1, gpsops, <your_password>, User, AAAA...base64key...==

# 5. Enable FTP for the IMO download workflow
sis, all, FTP

# 6. Disable HTTPS redirect (keeps existing router port-forwards 8060->80 working)
setHttpsSettings, HTTP

# 7. Save everything to boot config (persists across power cycle / reboot)
eccf, Current, Boot
```

### Verification

```
# Confirm users created
getUserAccessLevel

# Confirm FTP enabled
getIPServices

# Confirm HTTP-only web interface
getHttpsSettings

# Confirm config saved
lstConfigFile, Boot
```

---

## HTTPS Decision

### The issue

In fw 5.7.0, the default web interface behaviour is **both HTTP and HTTPS enabled, with HTTP redirecting to HTTPS**. This breaks the existing IMO router setup where all ~90 PolaRX5 routers forward port `8060 → 80` (HTTP only, not 443).

### Two options

| Option | What to do | Trade-off |
|--------|-----------|-----------|
| **A — Disable redirect** (recommended for now) | `shs, HTTP` on each receiver | Stays HTTP; no router changes needed; less secure on LAN |
| **B — Full HTTPS** | Change all router port-forwards to `8060 → 443` | More secure; ~90 router visits required |

**Recommendation**: Use Option A now to keep the rollout simple. Open Option B as a separate M3 infrastructure task once the fleet is on fw 5.7.0.

The `receivers` Python code uses TCP commands (not HTTPS), so this decision only affects
direct web interface access — it has no impact on the download or health-monitoring pipeline.

---

## SSH Key Workflow

SSH keys allow passwordless SFTP/SCP/rsync connections without embedding plaintext passwords in config files.

### Key pair setup (one-time, on reknew or the ops workstation)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/polarx5_gpsops -C "gpsops@IMO-GNSS" -N ""
```

This produces:
- `~/.ssh/polarx5_gpsops` — private key (stays on the server, never shared)
- `~/.ssh/polarx5_gpsops.pub` — public key (pushed to each receiver)

### Convert to RFC 4716 format for the receiver

The `setUserAccessLevel` SSH key argument uses RFC 4716 base64 (the key body only, without the `-----BEGIN/END-----` wrapper and without the `ssh-ed25519` prefix).

```bash
# Extract just the base64 body (the PolaRX5 wants only this part)
awk '{print $2}' ~/.ssh/polarx5_gpsops.pub
```

That output is what goes into the 5th argument of `setUserAccessLevel`.

### Per-station key override

For stations that need a different key (e.g., a legacy station not reachable from reknew),
override per station in `stations.cfg`:

```ini
[GJAC]
tcp_ssh_key_path = /path/to/station_specific_key
```

### Configuration storage

| Item | Default location | Per-station override |
|------|-----------------|---------------------|
| TCP username | `receivers.cfg` `[polarx5] tcp_username` | `stations.cfg` `[STATION] tcp_username` |
| TCP password | `receivers.cfg` `[polarx5] tcp_password` | `stations.cfg` `[STATION] tcp_password` |
| SSH private key path | `receivers.cfg` `[polarx5] tcp_ssh_key_path` | `stations.cfg` `[STATION] tcp_ssh_key_path` |

`receivers.cfg` example:

```ini
[polarx5]
tcp_username = gpsops
tcp_password = <your_password>
# tcp_ssh_key_path = /home/gpsops/.ssh/polarx5_gpsops  # uncomment when keys deployed
```

---

## Code Changes Required

### 1. `polarx5_tcp_extractor.py` — TCP authentication

The extractor opens a TCP connection to port 28784 and reads the `IPxx>` prompt.
In fw 5.7.0 any command before `login` returns `$E: Not authorized!`.

**Fix**: Inject `login, <user>, <pw>\n` immediately after reading the prompt,
before sending `esoc` or any other command.

The fix must handle both cases gracefully:
- fw 5.7.0 — send login, expect `$R: login`
- fw ≤5.5.0 — login is silently ignored (returns `$R: login` anyway)

Credentials are read from `receivers.cfg` `[polarx5]` section, with per-station override
from `stations.cfg`. If no credentials are configured, skip the login step and rely on the
pre-fw5.7.0 unauthenticated behaviour.

Implementation: add a `_login()` method called from `_send_sbf_request()` and `_send_ascii_command()`
after reading the prompt line.

### 2. `rxtools_extractor.py` — No changes needed

File-based extraction via `bin2asc` has no TCP session and requires no auth changes.

### 3. `receivers.cfg` — New `[polarx5]` section

Add credential storage for TCP authentication. See Configuration Storage above.

### 4. `polarx5_tcp_extractor.py` — ReceiverStatus2 block name

Double-check that `esoc` requests use `ReceiverStatus2` (not `ReceiverStatus`) when targeting
block 4014 with the temperature field. The block name in the `esoc` command must match
what the firmware recognises. Verify against GJAC.

### 5. `polarx5_tcp_extractor.py` — TLS fallback for sis=secure

After a firmware upgrade, `sis` resets to `secure` — port 28784 closes on reboot.
The health extractor must fall back to TLS on port 28783 rather than failing the whole
health check.

**Fix**: `_open_socket()` helper tries port 28784 first. On `ConnectionRefusedError`, it
retries with TLS on port 28783, sets `self.use_tls = True` and `self.port = 28783` so
subsequent connections within the same extractor instance reuse TLS. Implemented in all
three command-socket sites (`_request_receiver_setup_unauthenticated`, `_send_ascii_command`,
`_request_sbf_block`). Port-checker sockets are intentionally excluded — they test
specific ports and must not wrap unrelated ports in TLS.

---

## Validation Checklist (GJAC)

All items validated on GJAC (2026-04-22). Ready for fleet rollout.

- [x] TCP login succeeds with `gpsops` / `<your_password>` — confirmed 2026-04-22
- [x] `esoc` returns `PowerStatus` (4101) block correctly — 12.55 V confirmed 2026-04-22
- [x] `esoc` returns `ReceiverStatus2` (4014) block with temperature field populated — 22 °C confirmed 2026-04-22
- [x] `esoc` returns `DiskStatus` (4059) block with non-zero values — 3.0% / 15257 MB confirmed 2026-04-22
- [x] FTP download of `status_1hr` session files works — 3 files downloaded, 0 errors, 2026-04-22
- [x] `receivers health GJAC` produces correct health data end-to-end — HEALTHY, 11/11 green, 2026-04-22
- [x] `receivers download GJAC --session status_1hr --sync` completes successfully — confirmed 2026-04-22
- [x] Reboot receiver — settings persist across power cycle, confirmed 2026-04-22
- [x] Login still works after reboot — accounts survive in Boot config, confirmed 2026-04-22

---

## Operational Procedures

Required router port-forwards:

| Router port | Receiver port | Purpose | Required for |
|-------------|---------------|---------|-------------|
| 8060 | 80 | HTTP web interface | Always |
| 2160 | 21 | FTP data download | Always |
| 28784 | 28784 | TCP control (plaintext) | Normal operation |
| 28783 | 28783 | TCP control (TLS) | **Fw upgrade recovery** |

**Port 28783 must be forwarded before doing any remote firmware upgrade.** After a fw
upgrade, `sis` resets to `secure` — port 28784 closes immediately on reboot. Port 28783
(TLS) is the only remaining entry point. Without it, recovery requires a physical site visit.

Port 8060→443 is **not** needed. The provisioning sequence uses `shs, HTTP` to keep the
web interface on plain HTTP, so the existing 8060→80 forward continues to work after
upgrading to fw 5.7.0 — no router change required for the web interface.

---

### Procedure 1 — Firmware Upgrade to 5.7.0

> **⚠ PREREQUISITE — do this before starting the upgrade:**
> Port **28783 (TLS)** must be forwarded on the router before uploading firmware.
> After the reboot, port 28784 closes immediately. Port 28783 is the only remaining
> entry point. Without it, recovery requires a physical site visit.
> See the port-forward table above — add the `28783 → 28783` rule now.

**What happens during upgrade**: The firmware upgrade resets the `SISAuthData` permanent
command area, setting `sis = secure` (TLS-only). After the reboot, port 28784 (plaintext)
is closed and port 28783 (TLS) is open. User accounts and network settings are preserved.

#### Programmatic

```bash
# Step 1 — upload firmware via web interface (no CLI command for this)
#   Browser → http://<station-ip>:8060 → Admin → Upgrade Firmware
#   Upload the .sfx firmware file and wait for the receiver to reboot (~3 min)

# Step 2 — restore FTP and plaintext TCP access (TLS fallback is automatic)
receivers rec-provision STATION

# Step 3 — verify
receivers health STATION
```

`rec-provision` tries port 28784 first; on `ConnectionRefusedError` falls back to 28783
(TLS) automatically. It re-sends `sis, all, FTP` and `shs, HTTP`, restoring both FTP and
plaintext TCP without any manual intervention.

#### Manual

```bash
# After the reboot, port 28784 is closed. Connect via TLS:
openssl s_client -connect <ip>:28783 -quiet

# At the IP10> prompt:
login, gpsops, <your_password>
sis, all, FTP          # re-enable FTP + plaintext TCP
shs, HTTP              # keep web interface on HTTP (no redirect to HTTPS)
eccf, Current, Boot    # save to boot config

# Verify:
getIPServices
getHttpsSettings
```

---

### Procedure 2 — New Receiver Setup (fw 5.7.0 factory state)

A receiver shipped or factory-reset with fw 5.7.0 has no user accounts, FTP disabled,
and HTTPS redirect enabled. Port 28784 is open (TLS not yet enforced on a blank receiver).

#### Programmatic

```bash
receivers rec-provision STATION
```

This performs the full bootstrap sequence: creates `gpsops` account using factory
credentials, enables FTP, sets HTTP-only web interface, pushes SSH key, and saves
everything to boot config.

#### Manual

```
# Connect to port 28784 (plaintext — open on factory-fresh receiver)
nc <ip> 28784

# At the IP10> prompt — bootstrap using factory credentials:
login, gpsops, <your_password>, RxAdmin, S3pt3ntr10

# Add admin account
setUserAccessLevel, User2, admin, <your_admin_password>, User

# Enable FTP for IMO download workflow
sis, all, FTP

# Keep web interface on HTTP (router forwards 8060→80, not 443)
setHttpsSettings, HTTP

# (Optional) push SSH public key for passwordless SFTP
#   Extract base64 body: awk '{print $2}' ~/.ssh/polarx5_gpsops.pub
setUserAccessLevel, User1, gpsops, <your_password>, User, AAAA...base64...==

# Save to boot config
eccf, Current, Boot

# Verify
getUserAccessLevel
getIPServices
getHttpsSettings
```

---

### Procedure 3 — Config Push on fw 5.7.0

Pushing a station config file to a receiver that already has accounts set up.

**Critical rule**: the config file must contain **zero** `setUserAccessLevel` / `sual` lines.
Pushing `sual` commands overwrites existing accounts, including wiping the SSH key and
potentially locking out access.

#### Programmatic

```bash
# Verify the config file has no sual lines before pushing
grep -i "setUserAccessLevel\|sual" /path/to/STATION_config.txt
# (must return nothing)

receivers rec-config STATION --push /path/to/STATION_config.txt

# Confirm receiver accepted the config
receivers health STATION
```

#### Manual

```bash
# 1. Inspect the config file — must be zero sual/setUserAccessLevel lines
grep -i "setUserAccessLevel\|sual" STATION_config.txt

# 2. Connect via TCP
nc <ip> 28784

# 3. Log in (required on fw 5.7.0)
login, gpsops, <your_password>

# 4. Paste or send each non-sual command from the config file
#    Example commands (adjust to the actual config):
setNMEAOutput, ...
setSBFOutput, ...
...

# 5. Save to boot config
eccf, Current, Boot
```

#### gps_taeki config file naming convention

Config files stored in the `gps_taeki` repository follow this naming scheme:

```
PolaRx5_{STATION}_{description}_{YYYYMMDD}.txt
```

Examples:
```
PolaRx5_GJAC_initial_provision_20260422.txt
PolaRx5_ELDC_sbf_output_update_20260301.txt
```

Files must never contain `setUserAccessLevel` or `sual` lines — these are managed
exclusively by `rec-provision` and must not appear in pushable config files.

---

### Procedure 4 — Add Incremental Config (e.g. `Add_PolaRx5_health_session.txt`)

Used when adding a specific set of commands to an existing, already-provisioned receiver
without touching the full station config. Examples: adding a new SBF logging session,
enabling new constellations, updating NTRIP settings.

These `Add_*` files contain only the commands being added — **no `sual` lines, no
networking commands, no `eccf`** (the add-file must not save to boot; the operator
saves after verifying). The `eccf` is a deliberate last step.

#### Programmatic

```bash
# Verify the file has no sual lines
grep -i "setUserAccessLevel\|sual" Add_PolaRx5_health_session.txt
# (must return nothing)

receivers rec-config STATION --push Add_PolaRx5_health_session.txt

# Confirm the session appeared on the receiver
receivers health STATION
```

#### Manual (TCP)

```bash
nc <ip> 28784

# fw 5.7.0 requires login first
login, gpsops, <your_password>

# Paste the commands from the Add_* file:
setSBFOutput, Stream7, LOG5
setSBFOutput, Stream7, , PVTGeodetic+PosCovGeodetic+ReceiverTime+...
setSBFOutput, Stream7, , , sec60
setLogSession, LOG5, Enabled
setLogSession, LOG5, , , 'status_1hr'
setLogSession, LOG5, , , , After1Year
setLogSession, LOG5, , , , , High
setFileNaming, LOG5, IGS1H
setFileNaming, LOG5, , , on

# Verify the session is configured as expected
getLogSession

# Then save to boot config
eccf, Current, Boot
```

#### Manual (web GUI)

1. Browser → `http://<station-ip>:8060` → **Expert Control**
2. Navigate to the relevant section (e.g. SBF Output, Logging)
3. Add settings manually via the GUI
4. Click **Save to Boot** when satisfied

The web GUI approach is slower but useful when the exact command syntax is uncertain —
the GUI validates input and shows current state side-by-side.

#### Config file naming (Add_* files in gps_taeki)

Add-files are shared across stations (not station-specific), so they live in the top-level
config directory (not `station_config/`) and follow this naming:

```
Add_PolaRx5_{description}.txt
```

Examples:
```
Add_PolaRx5_health_session.txt
Add_PolaRx5_Galileo_BDS_NTR2.txt
```

---

## Factory Reset Behaviour

`factoryReset` on fw 5.7.0 **preserves** (permanent commands):
- `setEthernetMode` — static IP stays
- `setIPSettings` — IP address, netmask, gateway stay
- `setIPPortSettings` — port numbers stay

`factoryReset` **resets** (non-permanent):
- All user accounts (must re-run provisioning sequence above)
- `setIPServices` (FTP disabled again)
- `setHttpsSettings` (redirect re-enabled)
- All SBF output configurations
- All `setSBFOutput`, `setDataInOut` settings

This means after a factory reset, the full provisioning sequence must be repeated.

---

**Last Updated**: 2026-04-27
**Branch**: `feat/polarx5-firmware-v5.7`
**Canary station**: GJAC (fw 5.7.0 installed)
