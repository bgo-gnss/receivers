# Manual Test Scripts

This directory contains manual test scripts for validating specific receiver functionality
outside of the automated test suite.

## Purpose

These scripts are useful for:
- Testing new receiver types before integration
- Validating authentication mechanisms
- Debugging connection issues with specific stations
- Isolated testing of protocol changes

## Available Scripts

### test_natt_auth.py

Tests HTTP Basic Auth with NATT-operated Trimble receivers that have downgraded firmware.

**Purpose**: Validate authentication approach before integrating NATT stations into main codebase.

**Features**:
- Tests HTTP Basic Auth with configurable credentials
- Validates directory listing with authentication
- Downloads test files to `/tmp/natt_test/`
- Detects firmware bug (underscore-padded filenames like `ISAF______...`)

**Usage**:
```bash
# Edit configuration section in script first
cd /home/bgo/work/projects/gpslibrary/receivers
PYTHONPATH=src:../gtimes/src:../gps_parser/src python3 tests/manual/test_natt_auth.py
```

**Configuration**:
Edit the CONFIGURATION section (lines 27-48) with:
- Station ID
- IP address and port
- Username and password
- Session type to test

**Current Configuration**: ISAF (Ísafjörður) station
- IP: 193.109.17.51:80
- Credentials: IMO / piene16
- Session: 15s_24hr

## Directory Structure

```
tests/
├── manual/          # Manual test scripts (this directory)
│   ├── README.md
│   └── test_natt_auth.py
└── [other test directories...]
```

## Best Practices

1. **Credentials**: Store credentials in test scripts only temporarily
   - Final credentials should go in stations.cfg (gps-config-data repo)
   - Test scripts are for validation, not production

2. **Isolation**: These scripts should not modify production configuration
   - Download to /tmp/ locations
   - Use separate test directories

3. **Documentation**: Update this README when adding new scripts
   - Explain purpose and usage
   - Document any special requirements

4. **Git Branches**: Use feature branches for experimental work
   - Keep main branch stable
   - Merge after successful validation

## Notes

Manual test scripts may contain temporary credentials or configuration that
will later be moved to the proper configuration files (stations.cfg, receivers.cfg).

Always verify scripts work correctly before integrating changes into the main codebase.
