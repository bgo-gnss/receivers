# Scope — `receivers rec-upgrade-firmware`

Status: **proposal / not built**. Scopes an end-to-end PolaRX5 firmware-upgrade verb
that performs the flash itself (today the flash is a manual web-UI step) and chains
into the already-tooled aftermath (`rec-provision`, `cfg update-device`, `cfg reconcile`).

## Findings that shape the design

### 1. Sequential upgrade is NOT required by the docs (confirm 5.4 separately)
PolaRX5 `.suf` files are **full images**, not deltas. The 5.7.0 Reference Guide § 1.32
("Upgrade the Receiver") describes upgrading directly to the target version; there is **no
"must step through intermediate versions" language**. The only "incremental" references in
the manuals are the `NamingType=Incremental` file-naming option — unrelated.

**Caveat:** the firmware repo (`gps_taeki/PolaRx5/firmware/`) only holds 5.5.0 / 5.6.0 /
5.7.0 — no 5.4 package or release notes. A direct `5.4 → 5.7` should be confirmed against
Septentrio's 5.4 release notes / support before relying on it. The verb therefore **supports
an optional stepped path** (`--via 5.5.0,5.6.0`) so a fleet policy or a confirmed constraint
can be honored without code changes — default is direct-to-target.

### 2. The flash CAN be automated (the migration doc's "web-UI only" was incomplete)
Ref guide § 1.32 lists 5 methods; two are CLI-automatable:
- **`exeFTPUpgrade`** — receiver fetches the `.suf` from a remote FTP server and upgrades
  itself. Clean, but needs an FTP server reachable *from the receiver* hosting the `.suf`.
- **Stream-download (method 5, self-contained — preferred):**
  1. `exeResetReceiver, Upgrade, none` → receiver enters upgrade mode.
  2. Wait for `Ready for SUF download ...` (must start the download within **200 s**, else it
     restarts in normal mode).
  3. Stream the `.suf` over the connection in **binary** (progress indicator emitted).
  4. Receiver verifies integrity, executes the upgrade, **auto-reboots** (~3 min) with the new
     firmware. Corrupt/invalid file → discarded, restarts normally.
  5. Verify with `lif, Identification`.

### 3. Post-upgrade state (already documented in fw570-migration.md)
The upgrade resets `SISAuthData` → `sis = secure`: **port 28784 (plaintext) closes on reboot;
28783 (TLS) is the only entry point.** User accounts + network settings are preserved.
→ **Port 28783 MUST be forwarded on the router before starting**, or recovery needs a site visit.

## Proposed verb

```
receivers rec-upgrade-firmware <SID...> --to 5.7.0 [options]
```

### Resolution & flags
- `<SID>` → `router_ip:receiver_controlport` via the shared `resolve_station_probe` helper
  (handles shared-IP/port-forward stations; same as `update-device --station`).
- `--to <version>` target (must exist under `gps_taeki/PolaRx5/firmware/<version>/firmware/*.suf`).
- `--via <v1,v2>` optional explicit stepped path (default: direct to `--to`).
- `--method stream|ftp` (default `stream`; `ftp` uses `exeFTPUpgrade` + an FTP source).
- `--host HOST[:PORT]` bench/manual override (mirrors rec-provision).
- `--dry-run` (default) — print the plan, validate everything, send nothing.
- `--no-provision` / `--no-record` — skip the chained aftermath steps.
- `--ensure-port-forward` — auto-add the `28783→28783` rule via the Teltonika router API
  (reuse `cfg/telemetry_probe` port-forward management) instead of just asserting it.

### Flow (per target, looping over `--via` steps then `--to`)
1. **Pre-flight (all refuse-before-touch):**
   - Resolve connection; probe **current** fw (existing PolaRX5 TCP extractor) → skip if already ≥ target.
   - Locate + checksum-verify the local `.suf`.
   - **Assert/establish port 28783 forward** (the lifeline). Refuse if absent and not `--ensure-port-forward`.
   - Confirm credentials (`receivers.cfg [polarx5]`), reachability.
2. **Flash (stream method):** connect → login → `exeResetReceiver, Upgrade, none` → await
   `Ready for SUF download` → stream `.suf` binary with a progress callback → close.
3. **Wait for reboot** (~3 min, bounded) → reconnect on **28783 (TLS)** → `lif,Identification`
   → assert version == step target. Hard-fail (no further steps) on mismatch/timeout.
4. **Per-step or once at end — chain existing tooling:**
   - `rec-provision <SID>` — restore accounts + `sis,all,FTP` + `shs,HTTP` (upgrade wiped them).
   - `cfg update-device --station <SID> --field firmware_version --change` — TOS (Pattern 2).
   - `cfg reconcile <SID> --global --push` — stations.cfg + gps-config-data repo.
   - `receivers health <SID>` — operational verification.

### Safety / failure handling
- Dry-run default; explicit `--no-dry-run` to flash.
- 200 s download-start window and reboot wait are bounded with clear timeouts.
- The receiver self-verifies `.suf` integrity; the verb also checksums the local file first.
- On any step failure, **stop the chain** (don't attempt the next `--via` hop) and report the
  receiver's current version + how to recover via 28783.
- Never flash > 1 station truly in parallel without an explicit opt-in (a botched flash needs
  attention; serialize by default).
- Loud warning that power/connection must stay up through the reboot.

## Locked decisions (2026-06-24)
1. **Direct upgrade** is the default — confirmed safe by Septentrio docs: `.suf` is a full
   image and the 5.6.0 Release Notes describe applying the target `.suf` with **no
   source-/minimum-version constraint**. `--via v1,v2` remains as a manual escape hatch.
   (Caveat retained: no 5.4 release notes in-repo; the documented model is nonetheless direct.)
2. **Stream-download** method (self-contained). `exeFTPUpgrade` not implemented for now.
3. **Auto-chain the aftermath by default** (`rec-provision` → `cfg update-device --change` →
   `cfg reconcile --global --push` → `health`). `--no-provision`/`--no-record` to opt out.
4. **Port 28783 IS required** (post-upgrade reconnect lifeline) → **auto-add** the
   `28783→28783` Teltonika forward as part of pre-flight (reuse `cfg/telemetry_probe`), and
   verify it landed before sending `exeResetReceiver, Upgrade`.

## Reuses (low new surface)
- `cfg/device_probe.resolve_station_probe` (SID → ip:control_port) — already built.
- `health/polarx5_tcp_extractor` — TCP login + 28784→28783 TLS fallback + version read.
- `cfg/telemetry_probe` — Teltonika port-forward add (for `--ensure-port-forward`).
- `rec-provision` / `cfg update-device` / `cfg reconcile --global` — the aftermath chain.
- Firmware `.suf` files — `gps_taeki/PolaRx5/firmware/<version>/firmware/`.
