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

---

## Provisioning a New fw 5.7.0 Receiver

These steps must be performed once per receiver, either via the web interface or via TCP.
After provisioning, the `receivers` package handles everything programmatically.

### Prerequisites

- TCP access to the receiver on port 28784 (plaintext)
- Factory credentials: `RxAdmin` / `S3pt3ntr10` (Septentrio-wide, documented in the Reference Guide)
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
setIPServices, secure, FTP

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

---

## Validation Checklist (GJAC)

Before rolling fw 5.7.0 changes to the full fleet, validate on GJAC:

- [ ] TCP login succeeds with `gpsops` / `<your_password>`
- [ ] `esoc` returns `PowerStatus` (4101) block correctly
- [ ] `esoc` returns `ReceiverStatus2` (4014) block with temperature field populated
- [ ] `esoc` returns `DiskStatus` (4059) block with non-zero values
- [ ] FTP download of `status_1hr` session files works (confirms FTP is enabled)
- [ ] `receivers health GJAC` produces correct health data end-to-end
- [ ] `receivers download GJAC --session status_1hr --sync` completes successfully
- [ ] Reboot receiver (`eccf, Current, Boot` already done) — confirm settings persist
- [ ] Login still works after reboot (accounts in Boot config)

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

**Last Updated**: 2026-04-22
**Branch**: `feat/polarx5-firmware-v5.7`
**Canary station**: GJAC (fw 5.7.0 installed)
