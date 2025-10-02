# Receiver Architecture Diagrams

This document contains the core architecture diagrams for the receivers package, showing the modular design with proper abstractions.

## Core Architecture Overview

```mermaid
graph LR
    subgraph CLI["CLI Layer"]
        A[receivers CLI] --> B[Argument Parser]
        B --> C[download command]
        B --> D[health command]
        B --> E[status command]
        B --> F[validate command]
    end

    subgraph Factory["Factory & Abstraction Layer"]
        G[ReceiverFactory]
        H[BaseReceiver]
        I[BaseDownloadManager]
    end

    subgraph Impl["Receiver Implementations"]
        J[PolaRX5]
        K[NetRS]
        L[GeoSystem]
        M[SeptentrioDownloadManager]
        N[TrimbleDownloadManager]
        O[LeicaDownloadManager]
    end

    subgraph Config["Configuration Layer"]
        P[gps_parser]
        Q[stations.cfg]
        R[postprocess.cfg]
        S[timeout configs]
    end

    subgraph Data["Data Processing Layer"]
        T[FTP Connection]
        U[File Validator]
        V[Archive Manager]
        W[SBF Files]
        X[Validated Files]
        Y[Compressed Archive]
    end

    subgraph Utils["Utility Layer"]
        Z[gtimes]
        AA[GPS Time Conversion]
        BB[Path Builder]
    end

    C --> G
    D --> G
    E --> G
    F --> G
    G --> H
    G --> I
    H --> J
    H --> K
    H --> L
    I --> M
    I --> N
    I --> O
    J --> M
    K --> N
    L --> O
    P --> Q
    P --> R
    P --> S
    G --> P
    J --> P
    M --> T
    M --> U
    M --> V
    T --> W
    U --> X
    V --> Y
    Z --> AA
    Z --> BB
    M --> Z
    N --> Z
    O --> Z

    style G fill:#e1f5fe
    style H fill:#f3e5f5
    style I fill:#f3e5f5
    style P fill:#e8f5e8
    style Z fill:#fff3e0
```

## Class Hierarchy

```mermaid
classDiagram
    class BaseReceiver {
        <<abstract>>
        +station_id: str
        +station_config: dict
        +get_connection_status()* dict
        +download_data()* dict
        +get_health_status()* dict
        +get_station_info()* dict
    }

    class BaseDownloadManager {
        <<abstract>>
        +station_id: str
        +connection_timeout: int
        +test_connection()* dict
        +establish_connection()* Any
        +download_file()* dict
        +download_session() dict
        +archive_file() bool
    }

    class ReceiverFactory {
        +_receiver_types: dict
        +get_available_types() dict
        +is_supported() bool
        +create_receiver() BaseReceiver
    }

    class PolaRX5 {
        +ip_number: str
        +ip_port: int
        +pasv: bool
        +get_connection_status() dict
        +download_data() dict
        +get_health_status() dict
    }

    class SeptentrioDownloadManager {
        +session_map: dict
        +use_passive_ftp: bool
        +test_connection() dict
        +establish_connection() FTP
        +download_file() dict
    }

    BaseReceiver <|-- PolaRX5
    BaseDownloadManager <|-- SeptentrioDownloadManager
    PolaRX5 --> SeptentrioDownloadManager : uses
    ReceiverFactory --> BaseReceiver : creates
    ReceiverFactory --> PolaRX5 : creates
```

## Data Flow Sequence

```mermaid
sequenceDiagram
    participant CLI as CLI Command
    participant Factory as ReceiverFactory
    participant Config as gps_parser
    participant Receiver as PolaRX5
    participant DM as SeptentrioDownloadManager
    participant Times as gtimes

    CLI->>Factory: create_receiver(station_id)
    Factory->>Config: get_station_config(station_id)
    Config-->>Factory: station_config
    Factory->>Receiver: __init__(station_id, config)
    Receiver->>DM: __init__(station_id, config)

    CLI->>Receiver: download_data(start, end)
    Receiver->>DM: download_session(start, end, session)
    DM->>Times: process_time_parameters()
    DM->>Times: generate_file_list()
    DM->>DM: identify_missing_files()

    alt sync enabled
        DM->>DM: establish_connection()
        loop for each missing file
            DM->>DM: download_file()
            DM->>DM: archive_file()
        end
        DM->>DM: close_connection()
    end

    DM-->>Receiver: download_results
    Receiver-->>CLI: final_results
```

## Component Dependencies

```mermaid
graph LR
    subgraph "External Dependencies"
        A[gps_parser]
        B[gtimes]
        C[ftplib]
        D[tqdm]
    end

    subgraph "Core Components"
        E[ReceiverFactory]
        F[BaseReceiver]
        G[BaseDownloadManager]
        H[CLI Interface]
    end

    subgraph "Implementation Components"
        I[PolaRX5]
        J[SeptentrioDownloadManager]
        K[Configuration Utils]
    end

    A --> K
    B --> J
    C --> J
    D --> J
    E --> F
    F --> I
    G --> J
    H --> E
    I --> J
    K --> E

    style A fill:#ffeb3b
    style B fill:#ffeb3b
    style C fill:#ffeb3b
    style D fill:#ffeb3b
    style E fill:#4caf50
    style G fill:#4caf50
```

## Key Design Principles

1. **Factory Pattern**: `ReceiverFactory` centralizes receiver creation and type discovery
2. **Strategy Pattern**: `BaseDownloadManager` provides common logic with receiver-specific implementations
3. **Separation of Concerns**: Clear separation between receiver logic and download logic
4. **Configuration Abstraction**: Centralized configuration through `gps_parser` integration
5. **Modular Architecture**: Each receiver type can have its own download manager implementation

## Extension Points

- **New Receiver Types**: Implement `BaseReceiver` and corresponding `BaseDownloadManager`
- **New Protocols**: Extend `BaseDownloadManager` for HTTP, SFTP, etc.
- **New Configurations**: Add receiver-specific configuration via `gps_parser`
- **New Features**: Add health monitoring, scheduling, etc. through base classes