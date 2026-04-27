# Septentrio PolaRX5 TCP Command Reference

TCP/IP command interface for Septentrio PolaRX5 receivers.
Used for configuration management, live health data extraction, and receiver provisioning.

> **fw 5.7.0 change**: All TCP connections now require authentication before any command is accepted.
> See the [Authentication](#authentication-fw-570) section first.
> Receivers on earlier firmware (≤5.5.0) accept commands without login.

---

## Connection Details

| Port | Protocol | Purpose |
|------|----------|---------|
| **28784** | TCP plaintext | Commands (all firmware versions) |
| **28783** | TCP TLS | Secure commands (fw 5.7.0+) |
| **21** | FTP control | File transfer (must be enabled — see [IP Services](#ip-services-fw-570)) |
| **22** | SSH | SFTP / rsync (fw 5.7.0+) |

- **Prompt format**: `IPxx>` (e.g., `IP10>`, `IP11>`) — the `xx` is the connection ID used in `esoc`
- **Command format**: `command, arg1, arg2, ...\n`
- **Success response**: `$R: command_name` or `$R; command_name` (with body)
- **Error response**: `$E: error message`
- **Auth error**: `$E: Not authorized!` (fw 5.7.0 — no login sent or wrong credentials)

---

## Authentication (fw 5.7.0+)

### login

Authenticates the current TCP session. Must be sent immediately after the prompt appears, before any other command.

```
login, <UserName>, <Password>
```

Two forms depending on whether any user accounts exist on the receiver:

**Regular login** (after accounts have been created):
```
login, gpsops, <your_password>
```

**Factory bootstrap login** (receiver has no user accounts — fresh install or factory reset):
```
login, gpsops, <your_password>, RxAdmin, S3pt3ntr10
```

The 3rd and 4th arguments are the Septentrio factory credentials (`RxAdmin` / `S3pt3ntr10`).
This form **creates User1 and logs in** in one step — the supplied `UserName`/`Password` become the first account.
Factory credentials only work when no accounts exist; they have no further use once accounts are set.

Response (success):
```
$R! LogIn
  User gpsops logged in.
IPxx>
```

Response (wrong credentials):
```
$R? LogIn: Wrong username or password!
IPxx>
```

After a successful login the receiver emits a fresh `IPxx>` prompt. The client must consume both the login response body and the new prompt before sending the next command.

The session remains authenticated until the TCP connection is closed. No keepalive needed.

### Default access levels (fw 5.7.0)

The default interface access level for IP connections is `none` in fw 5.7.0 — meaning unauthenticated IP connections cannot issue any commands at all. This is the EU RED compliance requirement.

Previous firmware had a default `User` level on IP connections, which allowed unauthenticated control.

---

## User Management (fw 5.7.0+)

### setUserAccessLevel / getUserAccessLevel (sual / gual)

Creates or updates a named user account on the receiver. Up to 8 accounts (User1…User8).

```
setUserAccessLevel, <UserID>, <UserName>, <Password>, <UserLevel>[, <SSHPublicKey>]
```

| Parameter | Values | Notes |
|-----------|--------|-------|
| UserID | User1 … User8 | Slot to assign |
| UserName | string ≤16 chars | Login username |
| Password | string ≤32 chars | Login password (≤16 usable for TCP login per fw note) |
| UserLevel | User \| Viewer | `User` = full config; `Viewer` = read-only |
| SSHPublicKey | base64 string ≤232 chars | Optional; ECDSA, Ed25519, or RSA in RFC 4716 format |

Create the two standard accounts for the IMO GNSS network:
```
setUserAccessLevel, User1, gpsops, <your_password>, User
setUserAccessLevel, User2, admin, <your_admin_password>, User
```

Add an SSH public key to allow passwordless SFTP/SCP/rsync (omit password in later logins):
```
setUserAccessLevel, User1, gpsops, <your_password>, User, AAAA...base64key...==
```

Retrieve current settings:
```
gual
```

### setDefaultAccessLevel / getDefaultAccessLevel (sdal / gdal)

Sets the access level for unauthenticated connections on each interface.

```
setDefaultAccessLevel, <Interface>, <Level>
```

| Interface | fw 5.7.0 default | Meaning |
|-----------|-----------------|---------|
| Web | none | HTTP/browser requires login |
| FileTransfer | none | FTP/SFTP requires login |
| Ip | none | TCP command port requires login |
| Com | User | Serial port: no login needed |
| Usb | User | USB serial: no login needed |

The `none` defaults are the security-hardened state. Do not change these unless specifically required.

---

## IP Services (fw 5.7.0+)

### setIPServices / getIPServices (sis / gis)

Enables or disables IP-based services. **FTP is disabled by default in fw 5.7.0** and must be explicitly activated.

```
setIPServices, <Command>, <FileTransfer>
```

| Parameter | Values | Default |
|-----------|--------|---------|
| Command | none \| +secure \| +plaintext \| all | `secure` (port 28783 only) |
| FileTransfer | none \| +FTP \| +SFTP \| +rsync \| +SCP \| all | `none` |

Enable FTP for the IMO download workflow:
```
sis, secure, FTP
```

Enable SFTP instead (preferred — encrypts credential exchange):
```
sis, secure, SFTP
```

Query current state:
```
gis
```

Response example:
```
$R: gis
  IPServices, secure, FTP
```

> **Note**: `setIPServices` is **not** a permanent command. A factory reset disables FTP again.
> The setting is preserved across normal reboots and power cycles when saved to Boot via `eccf`.

### setHttpsSettings / getHttpsSettings (shs / ghs)

Controls whether the web interface accepts HTTP, HTTPS, or both.

```
setHttpsSettings, <Protocol>
```

| Protocol | Behaviour |
|----------|-----------|
| all (default) | Both HTTP and HTTPS accepted; HTTP redirects to HTTPS |
| HTTPS | HTTPS only; HTTP connections rejected |
| HTTP | HTTP only; no redirect (keeps router forwards 8060→80 working) |
| none | Web interface disabled |

For the IMO network where all routers forward **port 8060 → port 80** (not 443):
```
shs, HTTP
```

This prevents the receiver from redirecting HTTP to HTTPS, which would break web access through existing router port-forwards.

---

## Port Configuration (permanent)

### setIPPortSettings / getIPPortSettings (sipp / gipp)

Changes the TCP port numbers for IP services. This is a **permanent command** — survives factory reset.

```
setIPPortSettings, <PlaintextCommand>, <FTPControl>, <SecureCommand>, <SSHControl>
```

Defaults (fw 5.7.0):

| Service | Default port |
|---------|-------------|
| Plaintext command | 28784 |
| FTP control | 21 |
| Secure (TLS) command | 28783 |
| SSH (SFTP/rsync) | 22 |

Do not change defaults unless there is a specific conflict on the network.

---

## Configuration Management

### List configuration
```
lstConfigFile, Current    # Active configuration
lstConfigFile, Boot       # Boot (persistent) configuration
lstConfigFile, all        # All stored configurations
```

Response:
```
$R; lstConfigFile, Current

---->
$-- BLOCK 1 / 0
# Configuration File "Current"
# Different from RxDefault:
  setDataInOut, NTR1, , RTCMv3
  setSBFOutput, Stream1, LOG1
  ...
```

### Save / copy configuration
```
eccf, Current, Boot       # Save current settings to boot (persist across reboot)
eccf, RxDefault, Current  # Reset current settings to factory defaults
```

Always run `eccf, Current, Boot` after making configuration changes via TCP.

### Receiver information
```
grc    # Get receiver capabilities
gri    # Get receiver identification (model, serial number)
gfv    # Get firmware version
```

---

## SBF Output Configuration

### Get SBF output settings
```
gso, all        # All streams
gso, Stream1    # Specific stream
gso, Res1       # Reserved stream
```

Response:
```
$R: gso, all
  SBFOutput, Stream1, LOG1, MeasEpoch+GPSNav+..., sec15
  SBFOutput, Stream2, IPS1, MeasEpoch+..., sec1
```

### Set SBF output
```
sso, Stream1, LOG1                      # Set destination
sso, Stream1, , MeasEpoch+GPSNav        # Set blocks
sso, Stream1, , , sec15                 # Set interval
sso, Res1, none, none, off              # Disable stream
```

> **Important**: Never set SBF output to an IP connection (`IP10`, `IP11`, etc.) used for commands.
> Continuous binary SBF data on the same connection breaks command parsing.
> SBF output belongs on LOG files only.

---

## Live SBF Data Requests

### Execute SBF once (esoc)

Requests a single SBF block output to a specific IP connection.

```
esoc, IP10, PowerStatus       # Voltage data → current connection
esoc, IP10, ReceiverStatus2   # CPU, temperature, uptime → current connection
esoc, IP10, DiskStatus        # Internal storage → current connection
esoc, IP10, QualityInd        # Signal quality → current connection
```

Use the connection ID shown in the prompt (e.g., `IP10>` → use `IP10`).
The binary SBF block arrives on the same TCP socket immediately after the `$R:` response.

---

## SBF Block IDs for Health Monitoring

| Block Name | ID | Description | Key fields |
|------------|-----|-------------|-----------|
| PowerStatus | 4101 | Power supply | Voltage [V], source |
| ReceiverStatus2 | 4014 | Receiver health | CPU load [%], temperature [°C], uptime [s] |
| DiskStatus | 4059 | Internal storage | DiskSize [MB], DiskUsagePercent [%] |
| QualityInd | 4082 | Signal quality | N (tracked satellites) |
| PVTGeodetic2 | 4007 | Position fix | lat, lon, height |
| ChannelStatus | 4013 | Satellite channels | All visible channels |
| ReceiverTime | 4015 | GPS time | WNc, TOW |
| SatVisibility | 5914 | Satellite visibility | Elevation, azimuth per satellite |

> Use `ReceiverStatus2` (block 4014 with revision ≥1) for CPU/temperature. The earlier `ReceiverStatus` (same ID, revision 0) lacks the temperature field.

## SBF Binary Header Format

```
Bytes 0-1:  Sync pattern ($@)
Bytes 2-3:  CRC16
Bytes 4-5:  Block ID (lower 13 bits) + Revision (upper 3 bits)
Bytes 6-7:  Length (total block size including header)
```

---

## Configuration File Format

Plain text, one command per line, `#` comments:
```
setDataInOut, NTR1, , RTCMv3
setSBFOutput, Stream1, LOG1
setSBFOutput, Stream1, , MeasEpoch+GPSNav+GPSIon+GPSUtc+GLONav+PVTGeodetic+ReceiverSetup
setSBFOutput, Stream1, , , sec15
setMarkerParameters, "THOB"
# Comments start with #
```

File naming convention for extracted configs:
```
{ReceiverType}_{StationID}_{ConfigType}_{YYYY-MM-DD-HHMMSS}.txt
Example: PolaRx5_ISFS_Current_2026-01-21-104844.txt
```

---

## Troubleshooting

### "Not authorized!" on every command
The receiver is running fw 5.7.0 and the client is not sending `login`. See the
[Authentication](#authentication-fw-570) section.

### Connection refused on port 28784
Check firewall / router port-forward. For older routers, port 28784 may not be forwarded.
Verify with `nc -zv <ip> 28784`.

### No FTP connection
FTP is disabled by default in fw 5.7.0. Log in via TCP and run:
```
sis, secure, FTP
eccf, Current, Boot
```

### Binary data on TCP port at connect time
A misconfigured SBF output stream is targeting the IP connection. Identify and disable it:
```
gso, all
# find streams with IPSx or Res1 destinations
sso, Res1, none, none, off
eccf, Current, Boot
```

### Command errors
- `$E: Invalid command!` — command not recognised (check spelling, firmware version)
- `$E: Argument 'X' is invalid!` — bad argument value
- `$R?` prefix — partial success with warnings (check response body)

---

## CLI Usage

```bash
# Extract current config to stdout
receivers rec-config STATION --extract

# Extract boot config
receivers rec-config STATION --extract --config-type Boot

# Save to file (uses rec_config_dir from receivers.cfg, falls back to /tmp/polarconfig/)
receivers rec-config STATION --extract --save

# Save to specific directory
receivers rec-config STATION --extract --save --output-dir ~/configs/

# Diff against existing file
receivers rec-config STATION --extract --diff-with old_config.txt

# Push config file to receiver
receivers rec-config STATION --push config_file.txt

# Push without saving to boot
receivers rec-config STATION --push config_file.txt --no-save

# Dry run
receivers rec-config STATION --push config_file.txt --dry-run

# Multiple stations
receivers rec-config THOB,ISFS,ELDC --extract --save
receivers rec-config THOB,ISFS --push standard_config.txt
```

---

## Python API

```python
from receivers.septentrio.tcp_client import PolaRX5TCPClient, save_config_to_file

# Extract configuration (fw 5.7.0: credentials loaded from receivers.cfg automatically)
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    config = client.extract_config('Current')
    save_config_to_file(config, 'ISFS', 'Current', output_dir='~/configs/')

# Push configuration
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    commands = ['setSBFOutput, Stream1, LOG1', 'eccf, Current, Boot']
    success, errors = client.push_config(commands, save_to_boot=True)

# Request SBF block for health data
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    sbf_data = client.request_sbf_block('ReceiverStatus2', expected_id=4014)
```

---

## References

- Reference Guide: [`firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Reference-Guide.pdf`](firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Reference-Guide.pdf)
- Release Notes: [`firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Release-Notes.pdf`](firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Release-Notes.pdf)
- Previous firmware: [`firmware/5.5.0/`](firmware/5.5.0/)

---

**Last Updated**: 2026-04-22
**Applies to**: fw 5.7.0 (with notes for ≤5.5.0 where behaviour differs)
