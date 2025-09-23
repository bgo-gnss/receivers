# Legacy Code

This directory contains legacy code and scripts that have been replaced by the modern receivers package implementation.

## Files

- **getSeptentrio3*** - Original GPS data download scripts (replaced by receivers CLI)
- **__init__.py** - Legacy Python initialization file

## Migration Status

These legacy scripts have been **fully replaced** by the modern receivers package:

- **Legacy**: `getSeptentrio3` script
- **Modern**: `receivers download` command with full CLI interface

The legacy code is preserved for reference and migration verification purposes.

## Usage Note

**Use the new receivers package instead:**
```bash
# Old way (legacy)
./getSeptentrio3 ELDC --sync

# New way (recommended)
receivers download ELDC --sync --archive
```