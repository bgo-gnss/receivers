# Examples

Usage examples for the `receivers` package.

## Quick Start

```bash
# Download data
receivers download ELDC --sync --archive

# Check status
receivers status ISFS

# Get health info (live)
receivers health ISFS --json

# Extract health history
receivers health ISFS -s 20260110 -e 20260113
receivers health ISFS -d 7  # Last 7 days
```

## CLI Reference

See `receivers --help` and `receivers <command> --help` for full options.

## Development & Exploration

For exploration scripts and prototypes, see the `dev/` directory.
