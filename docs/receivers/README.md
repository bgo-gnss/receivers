# GPS Receivers Package Documentation

This documentation provides comprehensive coverage of the GPS receivers package, from high-level architecture to detailed implementation specifics.

## Documentation Structure

```
docs/receivers/
├── README.md                    # 📋 This overview
├── architecture.md              # 🏗️ System architecture
├── download-flow.md             # 🔄 Download process guide
├── receivers-guide.md           # 🔧 Individual receiver types
└── diagrams/                    # 📊 Mermaid diagram sources
    ├── receivers-overview.mmd   # High-level architecture
    ├── download-flow.mmd        # Main download process
    ├── validation-flow.mmd      # File validation logic
    ├── leica-g10.mmd           # Leica G10 class diagram
    ├── netr9.mmd               # NetR9 class diagram
    └── netrs.mmd               # NetRS class diagram
```

## Quick Start Guide

### 🚀 System Overview

- **[System Architecture](architecture.md)** - Complete architectural overview with design principles and layer breakdown
- **[Architecture Diagram](diagrams/receivers-overview.mmd)** - High-level system architecture diagram source

### 🔄 Download Process

- **[Download Flow Guide](download-flow.md)** - Complete download workflow documentation with diagrams
- **[Download Flow Diagram](diagrams/download-flow.mmd)** - Main download process diagram source
- **[Validation Flow Diagram](diagrams/validation-flow.mmd)** - File validation process diagram source

### 🔧 Receiver Implementations

- **[Receivers Guide](receivers-guide.md)** - Detailed guide for all supported receiver types
- **[Leica G10 Diagram](diagrams/leica-g10.mmd)** - Leica G10 class structure diagram source
- **[NetR9 Diagram](diagrams/netr9.mmd)** - NetR9 implementation diagram source
- **[NetRS Diagram](diagrams/netrs.mmd)** - NetRS implementation diagram source
- **[PolaRX5 Diagram](diagrams/polarx5.mmd)** - Septentrio PolaRX5 implementation diagram source

## System Architecture

The receivers package implements a layered architecture supporting Iceland's 173-station GNSS network:

### Core Layers

- **CLI & API Layer**: User interface and configuration management
- **Receiver Types Layer**: Manufacturer-specific implementations (Leica, Trimble, Septentrio)
- **Download Layer**: Protocol-specific clients (FTP, HTTP, TCP)
- **Processing Layer**: Validation, archiving, and compression
- **Storage Layer**: Organized file storage and logging

### Key Design Principles

- **Unified Interface**: All receivers implement consistent `download_data()` interface
- **Factory Pattern**: Automatic receiver type detection and instantiation
- **Protocol Abstraction**: Clean separation between receiver logic and communication protocols
- **Fault Tolerance**: Comprehensive error handling, retry logic, and resume capabilities

## Supported Receivers

| Receiver      | Protocol      | Port | File Format               | Directory Structure | Status                   |
| ------------- | ------------- | ---- | ------------------------- | ------------------- | ------------------------ |
| **Leica G10** | FTP (Active)  | 2160 | .m00.zip → .m00 → .m00.gz | Flat structure      | ✅ Fully Implemented     |
| **NetR9**     | HTTP          | 8060 | .T02 → .T02.gz            | Cache directory     | ✅ Fully Implemented     |
| **NetRS**     | HTTP          | 8060 | .T00 → .T00.gz            | Simple structure    | ✅ Fully Implemented     |
| **PolaRX5**   | FTP (Passive) | 21   | .sbf → .sbf.gz            | Nested structure    | 🚧 Partially Implemented |

---

**Last Updated**: 2025-09-25
**Version**: Development (gpslibrary_new)
**Maintainer**: Veðurstofan Íslands GPS Team
