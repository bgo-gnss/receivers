# NATT Standalone Script Deployment Guide

## Overview

The `download_natt_stations.py` script is a standalone Python script designed to download GPS data from NATT (Landmælingar Íslands) stations on rek.vedur.is production server.

## Features

- ✅ Self-contained with minimal dependencies (only `requests` library)
- ✅ Hard-coded station configurations for production reliability
- ✅ HTTP Basic Authentication for all NATT receivers
- ✅ NetR5 CACHEDIR auto-discovery (for ISAF)
- ✅ Direct writing to `/mnt/gpsdata/` production archive
- ✅ Cron-compatible with proper exit codes
- ✅ Downloads last 5 days by default (configurable)

## NATT Stations Supported

The script supports 13 NATT stations with direct HTTP access:

| Station | Location | Type | Port | Notes |
|---------|----------|------|------|-------|
| AKUR | Akureyri | NetR9 | 80 | |
| ALHV | Álftavatnsheidi | NetR9 | 7000 | |
| BJTV | Bjartarstaðir | NetR9 | 7000 | |
| BLON | Blönduós | NetR9 | 7000 | |
| FIHO | Fíflholt | NetR9 | 7000 | |
| GJFV | Gjáfell | NetR9 | 7000 | |
| GUSK | Gufuskálar | NetR9 | 7000 | |
| HEID | Heiðarsel | NetR9 | 7000 | |
| ISAF | Ísafjörður | NetR5 | 80 | Uses underscore padding |
| LAVI | Laugarvatn | NetR9 | 7000 | |
| RHOL | Reykjahóll | NetR9 | 7000 | |
| SKHA | Skálholt | NetR9 | 7000 | |
| VOFJ | Vopnafjörður | NetR9 | 7000 | |

Note: MYVA is NATT-owned but has no direct access (excluded from script)

## Prerequisites

1. Python 3.6 or higher
2. `requests` library:
   ```bash
   pip3 install requests
   ```

3. Write access to `/mnt/gpsdata/`

## Deployment Steps

### 1. Copy Script to rek.vedur.is

```bash
# From your local machine
scp download_natt_stations.py rek.vedur.is:/usr/local/bin/

# Or via intermediate server
scp download_natt_stations.py user@intermediate:/tmp/
ssh intermediate
scp /tmp/download_natt_stations.py rek.vedur.is:/usr/local/bin/
```

### 2. Make Script Executable

```bash
ssh rek.vedur.is
chmod +x /usr/local/bin/download_natt_stations.py
```

### 3. Test Script

```bash
# Test in dry-run mode
/usr/local/bin/download_natt_stations.py --test --days 1

# Test downloading single station
/usr/local/bin/download_natt_stations.py ISAF --days 1

# Test all stations (verbose)
/usr/local/bin/download_natt_stations.py --verbose --days 1
```

### 4. Configure Cron

Add to crontab (`crontab -e`):

```cron
# Download NATT stations daily at 01:00 UTC
0 1 * * * /usr/local/bin/download_natt_stations.py 2>&1 | logger -t natt-download

# Alternative with log file
0 1 * * * /usr/local/bin/download_natt_stations.py >> /var/log/natt-download.log 2>&1
```

## Usage Examples

```bash
# Download all NATT stations (default: last 5 days)
download_natt_stations.py

# Download specific stations
download_natt_stations.py ISAF AKUR BLON

# Download last 7 days
download_natt_stations.py --days 7

# Verbose output
download_natt_stations.py --verbose

# Test mode (dry run)
download_natt_stations.py --test
```

## Exit Codes

The script uses standard exit codes for monitoring:

- **0**: Success - all stations downloaded successfully
- **1**: Partial failure - some stations failed
- **2**: Complete failure - all stations failed
- **3**: Configuration error

## File Locations

### Archive Structure

Files are written to the production archive:

```
/mnt/gpsdata/YYYY/mon/STATION/15s_24hr/raw/STATIONYYYYMMDDHHMM[a].T02.gz
```

Example:
```
/mnt/gpsdata/2025/oct/ISAF/15s_24hr/raw/ISAF202510130000a.T02.gz
```

### Log Output

The script outputs to stdout, suitable for:
- Direct cron execution (emailed output)
- Piped to `logger` (syslog integration)
- Redirected to log file

## Monitoring

### Check Script Status

```bash
# Manual run with verbose output
/usr/local/bin/download_natt_stations.py --verbose

# Check last cron execution (if using logger)
grep "natt-download" /var/log/syslog | tail -20
```

### Common Issues

**Issue**: Files not downloading
- **Check**: Network connectivity to NATT IPs
- **Check**: Write permissions on `/mnt/gpsdata/`
- **Check**: HTTP Basic Auth credentials still valid

**Issue**: CACHEDIR auto-discovery fails
- **Check**: ISAF receiver is online
- **Solution**: Script will fall back to standard paths

**Issue**: Partial downloads
- **Expected**: Some stations may be offline or not have data for requested dates
- **Action**: Check logs for specific station errors

## Security Considerations

⚠️ **Important**: This script contains hard-coded credentials for NATT stations

- Credentials are embedded in the script for production simplicity
- Restrict file permissions: `chmod 750 /usr/local/bin/download_natt_stations.py`
- Ensure only authorized users can read the script
- Consider encrypting script or using secrets management if needed

## Testing Checklist

Before production deployment:

- [ ] Test script in test mode (`--test`)
- [ ] Verify ISAF downloads (NetR5 with CACHEDIR)
- [ ] Verify NetR9 stations download (any station except ISAF)
- [ ] Check file paths in `/mnt/gpsdata/` are correct
- [ ] Verify compressed files (.T02.gz) are valid
- [ ] Test cron execution
- [ ] Verify exit codes work correctly
- [ ] Check log output format

## Maintenance

### Updating Credentials

If NATT credentials change, edit the `NATT_STATIONS` dictionary in the script:

```python
"STATION": {
    "name": "Location",
    "ip": "IP_ADDRESS",
    "port": PORT,
    "type": "NetR9",
    "user": "USERNAME",
    "password": "NEW_PASSWORD",  # Update here
    "underscore_pad": False,
},
```

### Adding New Stations

To add a new NATT station:

1. Add entry to `NATT_STATIONS` dictionary
2. Test with single station: `download_natt_stations.py NEW_STATION --test`
3. Verify download works
4. Deploy updated script

## Troubleshooting

### Debug Mode

```bash
# Run with verbose logging
download_natt_stations.py --verbose --days 1

# Test specific station
download_natt_stations.py ISAF --test --verbose
```

### Manual Testing

```bash
# Test HTTP Basic Auth manually
curl -u "LMI:piene16" http://193.109.17.51:80/prog/show?directory&path=/Internal/

# Check CACHEDIR
curl -u "LMI:piene16" http://193.109.17.51:80/ | grep CACHEDIR
```

## Support

For issues or questions:
- Contact: GPS Team, Veðurstofa Íslands
- Email: gps-validation@vedur.is
- Script author: Benedikt Gunnar Ófeigsson

---

**Last Updated**: 2025-10-13
**Version**: 1.0
**Status**: Production Ready
