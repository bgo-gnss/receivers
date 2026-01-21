# Septentrio PolaRX5 TCP Command Reference

This document describes the TCP command interface for Septentrio PolaRX5 receivers,
used for configuration management and live health data extraction.

## Connection Details

- **Default Port**: 28784 (configurable)
- **Protocol**: TCP/IP
- **Prompt Format**: `IPxx>` (e.g., `IP10>`, `IP11>`)
- **Command Format**: `command, arg1, arg2, ...\n`
- **Response Format**: `$R: response` (success) or `$E: error` (failure)

## Common Commands

### Configuration Management

#### List Configuration
```
lstConfigFile, Current    # List current (active) configuration
lstConfigFile, Boot       # List boot configuration
lstConfigFile, all        # List all stored configurations
```

Response format:
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

#### Copy/Save Configuration
```
eccf, Current, Boot       # Copy Current config to Boot (save permanently)
eccf, RxDefault, Current  # Reset Current to factory defaults
```

### SBF Output Configuration

#### Get SBF Output Settings
```
gso, all                  # Get all SBF output stream settings
gso, Stream1              # Get specific stream settings
gso, Res1                 # Get reserved stream settings
```

Response format:
```
$R: gso, all
  SBFOutput, Stream1, LOG1, MeasEpoch+GPSNav+..., sec15
  SBFOutput, Stream2, IPS1, MeasEpoch+..., sec1
  ...
```

#### Set SBF Output
```
sso, Stream1, LOG1        # Set stream destination
sso, Stream1, , MeasEpoch+GPSNav  # Set stream blocks
sso, Stream1, , , sec15   # Set stream interval
sso, Res1, none, none, off  # Disable a stream
```

**Important**: Setting SBF output to IP connections (IP10, IP11, etc.) will cause
continuous data streaming on the TCP port, which can interfere with command responses.
Only LOG files should receive continuous SBF output.

### Live SBF Data Requests

#### Execute SBF Once (esoc)
```
esoc, IP10, PowerStatus       # Request single PowerStatus block to IP10
esoc, IP11, ReceiverStatus    # Request ReceiverStatus to IP11
```

The `esoc` command outputs a single SBF block to the specified IP connection.
Use the connection ID from the prompt (e.g., if prompt is `IP10>`, use `IP10`).

### Receiver Information

```
grc                       # Get receiver capabilities
gri                       # Get receiver identification
gfv                       # Get firmware version
```

## SBF Block IDs for Health Monitoring

| Block Name | ID | Description |
|------------|-----|-------------|
| PowerStatus | 4101 | Power supply voltage, source |
| ReceiverStatus | 4014 | CPU load, temperature, uptime |
| DiskStatus | 4059 | Internal storage status |
| PVTGeodetic2 | 4007 | Position (lat, lon, height) |
| PVTSatCartesian | 4008 | Satellites used in solution |
| ChannelStatus | 4013 | All satellite channels |
| ReceiverTime | 4015 | GPS time information |
| SatVisibility | 5914 | Satellite visibility |

## SBF Binary Format

SBF blocks have the following header structure:
```
Bytes 0-1:  Sync pattern ($@)
Bytes 2-3:  CRC16
Bytes 4-5:  ID + Revision (lower 13 bits = Block ID)
Bytes 6-7:  Length (total block size)
```

## CLI Usage

### Extract Configuration
```bash
# Extract current config
receivers rec-config STATION --extract

# Extract boot config
receivers rec-config STATION --extract --config-type Boot

# Extract to specific directory
receivers rec-config STATION --extract --output-dir ~/configs/

# Extract and compare with existing file
receivers rec-config STATION --extract --diff-with old_config.txt

# Extract from multiple stations
receivers rec-config THOB,ISFS,ELDC --extract --output-dir ~/configs/
```

### Push Configuration
```bash
# Push config file to receiver
receivers rec-config STATION --push config_file.txt

# Push without saving to boot
receivers rec-config STATION --push config_file.txt --no-save

# Dry run (show what would be sent)
receivers rec-config STATION --push config_file.txt --dry-run

# Push to multiple stations
receivers rec-config THOB,ISFS --push standard_config.txt
```

## Configuration File Format

Configuration files are plain text with one command per line:
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

## Troubleshooting

### Connection Issues
```bash
# Test TCP connectivity
nc -zv 10.6.1.201 28784

# Test with simple command
echo -e "grc\n" | nc 10.6.1.201 28784
```

### Unexpected SBF Data on TCP Port
If you see binary data immediately upon connecting, check for misconfigured
SBF output streams:
```
gso, all
# Look for streams targeting IP connections (IP10, IP11, etc.)
# Disable with:
sso, Res1, none, none, off
eccf, Current, Boot  # Save to make permanent
```

### Command Errors
- `$E: Invalid command!` - Command not recognized
- `$E: Argument 'X' is invalid!` - Invalid argument value
- `$R?` prefix indicates partial success with warnings

## Python API Usage

```python
from receivers.septentrio.tcp_client import PolaRX5TCPClient, save_config_to_file

# Extract configuration
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    config = client.extract_config('Current')
    save_config_to_file(config, 'ISFS', 'Current', output_dir='~/configs/')

# Push configuration
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    commands = ['setSBFOutput, Stream1, LOG1', 'eccf, Current, Boot']
    success, errors = client.push_config(commands, save_to_boot=True)

# Request SBF block for health data
with PolaRX5TCPClient('10.6.1.201', 'ISFS') as client:
    sbf_data = client.request_sbf_block('PowerStatus', expected_id=4101)
```

## References

- [Septentrio PolaRX5 Product Page](https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5)
- [RxTools Software](https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools)
- Reference Guide is included in firmware package downloads

---

**Last Updated**: 2026-01-21
**Version**: receivers package
