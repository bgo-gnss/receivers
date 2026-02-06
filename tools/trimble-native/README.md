# Trimble Native RINEX 3 Converter

This tool provides native RINEX 3 conversion for Trimble T00/T02 raw data files
using the official Trimble Convert to RINEX utility running in a Docker container.

## Why Use This?

The standard workflow (runpkr00 → teqc → gfzrnx) produces RINEX 3 by reformatting
RINEX 2 output. This means observation codes are translated, not native.

The native Trimble converter produces **true RINEX 3** files with:
- Native RINEX 3 observation codes (C1C, C2W, C5X, etc.)
- Proper multi-GNSS support (GPS, GLONASS, Galileo, BeiDou)
- Full L5/E5 signal support
- Official Trimble conversion quality

## Supported Formats

| Format | Receiver | Extension |
|--------|----------|-----------|
| T00 | NetRS | `.T00`, `.T00.gz` |
| T02 | NetR9 | `.T02`, `.T02.gz` |

## Requirements

1. **Docker** - Must be installed and running
2. **Disk space** - ~3 GB for the Docker image
3. **Internet** - For initial image download

### Installing Docker

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER  # Log out and back in after this
```

**Fedora:**
```bash
sudo dnf install docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
```

**Arch Linux:**
```bash
sudo pacman -S docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
```

## Installation

### Quick Install (Recommended)

```bash
cd tools/trimble-native
./setup.sh
```

This will:
1. Check Docker is installed and running
2. Pull the pre-built Docker image (~2.4 GB download)
3. Tag it for use with the receivers package
4. Verify the converter works

### Manual Install

If you prefer to install manually:

```bash
# Pull the image
docker pull geodesyewsp/trm2rinex:cli-light

# Tag it for our code
docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light

# Verify it works
docker run --rm --entrypoint="" trm2rinex:cli-light \
    /opt/wine/bin/wine \
    "C:\\Program Files\\Trimble\\convertToRINEX\\convertToRinex.exe" \
    --help
```

### Check Installation Status

```bash
./setup.sh --check
```

### Run Test

```bash
./setup.sh --test
```

## Usage

### Command Line

Use the `--native-trimble` flag with the `receivers rinex` command:

```bash
# Convert a single day for a station
receivers rinex MANA --native-trimble -d 1

# Convert with verbose output
receivers rinex BLEI --native-trimble -d 2 --verbose

# Convert specific date range
receivers rinex SJUK --native-trimble --start 20260201 --end 20260203
```

### Python API

```python
from receivers.rinex import TrimbleNativeConverter, RinexVersion
from pathlib import Path

# Create converter
converter = TrimbleNativeConverter(
    station_id='MANA',
    rinex_version=RinexVersion.RINEX_3,
)

# Check if Docker is available
if TrimbleNativeConverter.is_available():
    # Convert a file
    result = converter.convert_file(
        raw_file=Path('/path/to/MANA202601010000a.T02.gz'),
        output_dir=Path('/path/to/output'),
    )

    if result.success:
        print(f"Created: {result.rinex_file}")
        print(f"Duration: {result.duration_seconds:.1f}s")
else:
    print("Docker image not available")
```

## Performance

The Docker+Wine wrapper is approximately **3x slower** than native Windows:

| Station | Format | Native Windows | Docker+Wine |
|---------|--------|----------------|-------------|
| BLEI | T00 (24h) | ~2s | ~6s |
| SJUK | T02 (24h) | ~5s | ~16s |
| RHOF | T02 (1h) | ~4s | ~11s |

This is acceptable for batch processing but may be slow for real-time applications.

## Troubleshooting

### Docker Permission Denied

```
Got permission denied while trying to connect to the Docker daemon socket
```

**Solution:** Add your user to the docker group:
```bash
sudo usermod -aG docker $USER
# Log out and back in, or run:
newgrp docker
```

### Image Not Found

```
Error: Docker image 'trm2rinex:cli-light' not found
```

**Solution:** Run the setup script:
```bash
./setup.sh
```

### Wine Errors

Wine may print warnings like:
```
wine: failed to open L"C:\\windows\\system32\\services.exe": c0000135
```

This is normal and does not affect conversion. The converter still works.

### Conversion Fails

If conversion fails, check:
1. Input file exists and is readable
2. Docker has sufficient memory (at least 1 GB)
3. Output directory is writable

Enable verbose logging:
```bash
receivers rinex STATION --native-trimble -d 1 --verbose
```

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                     Host System (Linux)                      │
│                                                              │
│  ┌─────────────┐     ┌────────────────────────────────────┐ │
│  │ T02/T00     │     │      Docker Container              │ │
│  │ Raw File    │────▶│  ┌──────────────────────────────┐  │ │
│  └─────────────┘     │  │         Wine 6.22            │  │ │
│                      │  │  ┌────────────────────────┐  │  │ │
│  ┌─────────────┐     │  │  │  convertToRinex.exe   │  │  │ │
│  │ RINEX 3.04  │◀────│  │  │  (Trimble Official)   │  │  │ │
│  │ Output      │     │  │  └────────────────────────┘  │  │ │
│  └─────────────┘     │  └──────────────────────────────┘  │ │
│                      └────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

1. Input file is copied to a temporary directory
2. Directory is mounted as a volume in the Docker container
3. Wine runs the official Trimble convertToRinex.exe
4. Output RINEX file is copied back to the output directory
5. Temporary files are cleaned up

## Credits

- Docker image: [geodesyewsp/trm2rinex](https://hub.docker.com/r/geodesyewsp/trm2rinex)
- Original project: [Matioupi/trm2rinex-docker](https://github.com/Matioupi/trm2rinex-docker)
- Trimble Convert to RINEX: [Trimble Geospatial](https://geospatial.trimble.com/)

## License

The Docker wrapper and setup scripts are provided under the same license as the
receivers package. The Trimble Convert to RINEX utility inside the container
is proprietary software from Trimble Navigation Ltd.
