# RINEX Tools Installation Guide

This guide explains how to install external tools required for RINEX conversion.

## Quick Start

```bash
# Install auto-installable tools (teqc, rnx2crx)
receivers tools install-all

# Check installation status
receivers tools list

# Update receivers.cfg with tool paths
receivers tools configure
```

## Tool Overview

| Tool | Purpose | Source | Auto-Install |
|------|---------|--------|--------------|
| **mdb2rinex** | Leica m00 → RINEX 3/4 (native) | Leica myWorld | ❌ Manual |
| **teqc** | Leica m00 → RINEX 2 (fallback) | UNAVCO | ✅ Auto |
| **gfzrnx** | RINEX format conversion, QC | GFZ Potsdam | ❌ Manual (registration) |
| **rnx2crx** | Hatanaka compression | GSI Japan | ✅ Auto |
| **runpkr00** | Trimble T00/T02 extraction | Trimble | ❌ Manual |
| **sbf2rin** | Septentrio SBF → RINEX | Septentrio | ❌ Manual |

Tools install to `~/.local/share/gps-rinex-tools/bin/` by default.

---

## Leica Receivers (GR10, GR25, GR30, GR50)

### mdb2rinex (Recommended)

**mdb2rinex** is Leica's official converter that outputs native RINEX 3/4.

#### Download

1. Log into [Leica myWorld](https://myworld.leica-geosystems.com/)
2. Navigate to: **Products & Services** → **Your GNSS Receiver** → **Tools**
3. Download "MDB Converter" for Linux 64-bit

#### Install

```bash
# Extract and install
unzip mdb2rinex_Linux_64bit_*.zip -d /tmp/mdb2rinex
cp /tmp/mdb2rinex/mdb2rinex ~/.local/share/gps-rinex-tools/bin/
chmod +x ~/.local/share/gps-rinex-tools/bin/mdb2rinex

# Update config
receivers tools configure

# Verify
receivers tools list
```

#### Usage

```bash
# mdb2rinex is used automatically when available
receivers rinex SKFC -d 7

# Direct usage
mdb2rinex -f input.m00 -o output_dir -r rinex3.04
```

#### Command-Line Options

```
mdb2rinex -f <files> -o <output_dir> [-r <version>] [-s]

Options:
  -f, --files         Input m00 file(s)
  -o, --out           Output directory
  -r, --rinex_version RINEX version (rinex3.04 or rinex4.00)
  -s, --summary       Print tracking summary at end of obs file
  -h, --help          Print help
  -v, --version       Print version
```

### teqc (Fallback)

**teqc** is UNAVCO's legacy tool. It only outputs RINEX 2, so gfzrnx is needed for RINEX 3 conversion (reformatting only).

```bash
# Auto-install
receivers tools install teqc

# Or manual download from UNAVCO
wget https://www.unavco.org/software/data-processing/teqc/development/teqc_CentOSLx86_64d.zip
unzip teqc_CentOSLx86_64d.zip -d /tmp
cp /tmp/teqc ~/.local/share/gps-rinex-tools/bin/
chmod +x ~/.local/share/gps-rinex-tools/bin/teqc
```

**Note:** teqc is end-of-life (final release 2019-02-25) but still works.

---

## Trimble Receivers (NetR9, NetRS, NetR5)

### runpkr00

**runpkr00** extracts raw data from Trimble T00/T02 files to DAT/TGD format for teqc processing.

#### Download

Available from [UNAVCO Knowledge Base](https://kb.unavco.org/article/trimble-runpkr00-latest-versions-744.html):

- **Linux v5.40 RPM**: `runpkr00-5.40-1trmb.i586.rpm` (32-bit, statically linked)
- **Linux v6.03 RPM**: `runpkr00-6.03-3trmb.i686.rpm` (requires libT01Utils8)

#### Install from RPM

```bash
# Download the RPM
wget https://unavco.knowledgebase.co/assets/744/runpkr00-5.40-1trmb.i586.rpm

# Install rpmfile Python package for extraction
pip install rpmfile

# Extract the binary
python3 << 'EOF'
import rpmfile
import os
with rpmfile.open('runpkr00-5.40-1trmb.i586.rpm') as rpm:
    for member in rpm.getmembers():
        if member.name.endswith('/runpkr00'):
            with open('runpkr00', 'wb') as f:
                f.write(rpm.extractfile(member.name).read())
            os.chmod('runpkr00', 0o755)
            print(f"Extracted: runpkr00")
EOF

# Install to tools directory
cp runpkr00 ~/.local/share/gps-rinex-tools/bin/
receivers tools configure

# Verify
receivers tools list
```

#### Usage

```bash
# Extract T02 file to TGD format (for teqc)
runpkr00 -g -d input.T02

# Produces: input.TGD (or input.DAT if not RT27 format)
# The -g flag is required for GNSS signals from RT27 files
```

---

## Septentrio Receivers (PolaRX5)

### sbf2rin

**sbf2rin** converts Septentrio SBF files to RINEX.

#### Download

1. Go to [Septentrio Support](https://www.septentrio.com/en/support)
2. Register for a free account
3. Download RxTools for Linux
4. Extract sbf2rin from the package

#### Install

```bash
# After extracting from RxTools
cp sbf2rin ~/.local/share/gps-rinex-tools/bin/
chmod +x ~/.local/share/gps-rinex-tools/bin/sbf2rin
receivers tools configure
```

---

## RINEX Format Tools

### gfzrnx

**gfzrnx** handles RINEX format conversion, quality control, splicing, and more.

#### Download

gfzrnx now requires registration (free for non-commercial use):

1. Visit [GFZ GFZRNX page](https://gnss.gfz-potsdam.de/services/gfzrnx)
2. Register for a scientific license
3. Download the Linux 64-bit binary

#### Install

```bash
cp gfzrnx ~/.local/share/gps-rinex-tools/bin/
chmod +x ~/.local/share/gps-rinex-tools/bin/gfzrnx
receivers tools configure
```

### Hatanaka Compression (rnx2crx, crx2rnx)

**rnx2crx** compresses RINEX files to compact format (.d).

```bash
# Auto-install
receivers tools install rnx2crx

# Or manual download from GSI Japan
wget https://terras.gsi.go.jp/ja/crx2rnx/RNXCMP_4.1.0_Linux_x86_64bit.tar.gz
tar xzf RNXCMP_4.1.0_Linux_x86_64bit.tar.gz
cp RNXCMP_*/bin/* ~/.local/share/gps-rinex-tools/bin/
```

---

## Configuration

After installing tools, update the configuration:

```bash
# Auto-update receivers.cfg with installed tool paths
receivers tools configure

# Or manually edit ~/.config/gpsconfig/receivers.cfg
[rinex_tools]
mdb2rinex_path = /home/user/.local/share/gps-rinex-tools/bin/mdb2rinex
teqc_path = /home/user/.local/share/gps-rinex-tools/bin/teqc
gfzrnx_path = /home/user/.local/share/gps-rinex-tools/bin/gfzrnx
runpkr00_path = /home/user/.local/share/gps-rinex-tools/bin/runpkr00
sbf2rin_path = /home/user/.local/share/gps-rinex-tools/bin/sbf2rin
rnx2crx_path = /home/user/.local/share/gps-rinex-tools/bin/RNX2CRX
crx2rnx_path = /home/user/.local/share/gps-rinex-tools/bin/CRX2RNX
```

---

## Verification

```bash
# List all tools and their status
receivers tools list

# Check tools for a specific receiver type
receivers tools check --receiver-type G10
receivers tools check --receiver-type PolaRX5
receivers tools check --receiver-type NetR9

# Test conversion
receivers rinex STATION -d 1 --dry-run
```

---

## Troubleshooting

### Tool not found

```bash
# Check if tool is in PATH
which mdb2rinex

# Check configured path
grep mdb2rinex ~/.config/gpsconfig/receivers.cfg

# Reconfigure
receivers tools configure
```

### Permission denied

```bash
chmod +x ~/.local/share/gps-rinex-tools/bin/mdb2rinex
```

### glibc version error (mdb2rinex)

mdb2rinex v6.0+ requires glibc 2.35 or later (Ubuntu 22.04+).

```bash
# Check glibc version
ldd --version

# For older systems, use mdb2rinex v5.6.x or teqc fallback
```

### Missing library errors

```bash
# Check dependencies
ldd ~/.local/share/gps-rinex-tools/bin/mdb2rinex

# Install missing libraries (Ubuntu/Debian)
sudo apt install libc6
```
