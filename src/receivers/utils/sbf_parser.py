"""
SBF (Septentrio Binary Format) Parser.

This module provides utilities for parsing SBF binary files and extracting
specific block types for health monitoring.

SBF Block Structure:
- Sync: 2 bytes ($@ = 0x24 0x40)
- CRC: 2 bytes
- ID: 2 bytes (block number in lower 13 bits, revision in upper 3 bits)
- Length: 2 bytes (total block length including header)
- TOW (Time of Week): 4 bytes (milliseconds)
- WNc (Week Number): 2 bytes
- Data: variable length
- Padding: 0-3 bytes to align to 4-byte boundary

Block IDs of interest:
- 4014 (0xfae): ReceiverStatus - CPU load, uptime, status flags
- 4101 (0x1005): PowerStatus - Power supply source and voltage
- 4059 (0xfdb): DiskStatus - Internal logging status
- 4082 (0xff2): QualityInd - Quality indicators
"""

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional

# GPS epoch: January 6, 1980 00:00:00 UTC
GPS_EPOCH = datetime(1980, 1, 6, 0, 0, 0)

# SBF Block IDs
BLOCK_ID_RECEIVER_STATUS = 4014
BLOCK_ID_POWER_STATUS = 4101
BLOCK_ID_DISK_STATUS = 4059
BLOCK_ID_QUALITY_IND = 4082
BLOCK_ID_RECEIVER_TIME = 5914
BLOCK_ID_PVT_GEODETIC = 4007


@dataclass
class SBFBlockHeader:
    """SBF block header information."""

    sync: bytes
    crc: int
    block_id: int
    block_rev: int
    length: int
    tow: int  # Time of Week in milliseconds
    wnc: int  # Week Number


@dataclass
class ReceiverStatusData:
    """Parsed ReceiverStatus block data."""

    tow: int  # milliseconds
    wnc: int
    datetime: datetime
    cpu_load: int  # percentage
    uptime: int  # seconds
    rx_error: int
    rx_status: int
    rx_state: int


@dataclass
class PowerStatusData:
    """Parsed PowerStatus block data."""

    tow: int  # milliseconds
    wnc: int
    datetime: datetime
    voltage_internal: float  # Volts
    voltage_external: float  # Volts (0 if not connected)
    power_source: int  # 0=internal, 1=external


class SBFParser:
    """Parser for SBF binary files."""

    SYNC_BYTES = b"$@"

    def __init__(self, file_path: Path):
        """Initialize parser with SBF file path."""
        self.file_path = Path(file_path)

    @staticmethod
    def gps_time_to_datetime(wnc: int, tow: int) -> datetime:
        """
        Convert GPS Week Number and Time of Week to Python datetime.

        Args:
            wnc: GPS Week Number
            tow: Time of Week in milliseconds

        Returns:
            Python datetime object
        """
        # GPS time = GPS_EPOCH + weeks + milliseconds
        delta = timedelta(weeks=wnc, milliseconds=tow)
        return GPS_EPOCH + delta

    def parse_header(self, data: bytes) -> Optional[SBFBlockHeader]:
        """
        Parse SBF block header.

        Args:
            data: Bytes containing at least 8 bytes for header

        Returns:
            SBFBlockHeader or None if invalid
        """
        if len(data) < 8:
            return None

        # Check sync bytes
        if data[0:2] != self.SYNC_BYTES:
            return None

        # Unpack header (little-endian)
        # sync(2), crc(2), id(2), length(2)
        crc = struct.unpack("<H", data[2:4])[0]
        id_field = struct.unpack("<H", data[4:6])[0]
        length = struct.unpack("<H", data[6:8])[0]

        # Extract block ID and revision
        # Lower 13 bits = block number, upper 3 bits = revision
        block_id = id_field & 0x1FFF
        block_rev = (id_field >> 13) & 0x07

        # Parse TOW and WNc if block is long enough
        tow = 0
        wnc = 0
        if len(data) >= 14:
            tow = struct.unpack("<I", data[8:12])[0]
            wnc = struct.unpack("<H", data[12:14])[0]

        return SBFBlockHeader(
            sync=data[0:2],
            crc=crc,
            block_id=block_id,
            block_rev=block_rev,
            length=length,
            tow=tow,
            wnc=wnc,
        )

    def parse_receiver_status(
        self, data: bytes, header: SBFBlockHeader
    ) -> Optional[ReceiverStatusData]:
        """
        Parse ReceiverStatus block (ID 4014).

        Block format (after header):
        - CPULoad: 1 byte (percentage)
        - ExtError: 1 byte
        - UpTime: 4 bytes (seconds)
        - RxError: 4 bytes
        - RxStatus: 4 bytes
        - RxState: 1 byte
        """
        if header.block_id != BLOCK_ID_RECEIVER_STATUS:
            return None

        # Data starts at offset 14 (after header + TOW + WNc)
        if len(data) < 14 + 15:
            return None

        offset = 14
        cpu_load = struct.unpack("<B", data[offset : offset + 1])[0]
        # ext_error = struct.unpack('<B', data[offset+1:offset+2])[0]
        uptime = struct.unpack("<I", data[offset + 2 : offset + 6])[0]
        rx_error = struct.unpack("<I", data[offset + 6 : offset + 10])[0]
        rx_status = struct.unpack("<I", data[offset + 10 : offset + 14])[0]
        rx_state = struct.unpack("<B", data[offset + 14 : offset + 15])[0]

        return ReceiverStatusData(
            tow=header.tow,
            wnc=header.wnc,
            datetime=self.gps_time_to_datetime(header.wnc, header.tow),
            cpu_load=cpu_load,
            uptime=uptime,
            rx_error=rx_error,
            rx_status=rx_status,
            rx_state=rx_state,
        )

    def parse_power_status(
        self, data: bytes, header: SBFBlockHeader
    ) -> Optional[PowerStatusData]:
        """
        Parse PowerStatus block (ID 4101).

        Block format (revision 0, length=16):
        - Data: 2 bytes (uint16, decivolts = voltage * 10)

        Note: For revision 0, only a single voltage value is provided.
        Higher revisions may have more detailed power information.
        """
        if header.block_id != BLOCK_ID_POWER_STATUS:
            return None

        # For revision 0, data is just 2 bytes at offset 14
        if len(data) < 14 + 2:
            return None

        offset = 14
        voltage_raw = struct.unpack("<H", data[offset : offset + 2])[0]

        # Verified with RxTools bin2asc utility:
        # bin2asc extracts PowerStatus "Vin Voltage [V]" field
        # Raw value 9185 → 14.35V, 9201 → 14.38V
        # Scaling factor: raw_value / 640 = volts
        # This matches observed 12-15V range for 12V power systems

        VOLTAGE_SCALE_FACTOR = 640  # Verified with RxTools bin2asc
        voltage_internal = voltage_raw / VOLTAGE_SCALE_FACTOR

        return PowerStatusData(
            tow=header.tow,
            wnc=header.wnc,
            datetime=self.gps_time_to_datetime(header.wnc, header.tow),
            voltage_internal=voltage_internal,
            voltage_external=0.0,  # Not available in revision 0
            power_source=0,  # Unknown in revision 0
        )

    def read_blocks(self, block_ids: Optional[List[int]] = None) -> List[tuple]:
        """
        Read and parse SBF blocks from file.

        Args:
            block_ids: List of block IDs to extract (None = all blocks)

        Returns:
            List of tuples (header, parsed_data) for matching blocks
        """
        blocks = []

        with open(self.file_path, "rb") as f:
            while True:
                # Find next sync pattern
                sync = f.read(2)
                if len(sync) < 2:
                    break

                if sync != self.SYNC_BYTES:
                    # Not aligned, go back 1 byte and continue searching
                    f.seek(-1, 1)
                    continue

                # Read minimum header (8 bytes)
                header_data = sync + f.read(6)
                if len(header_data) < 8:
                    break

                # Parse basic header to get length
                struct.unpack("<H", header_data[2:4])[0]
                id_field = struct.unpack("<H", header_data[4:6])[0]
                length = struct.unpack("<H", header_data[6:8])[0]

                id_field & 0x1FFF
                (id_field >> 13) & 0x07

                # Read rest of block (length includes header)
                remaining = length - 8
                if remaining < 0:
                    continue

                rest_of_block = f.read(remaining)
                if len(rest_of_block) < remaining:
                    break

                # Complete block data
                block_data = header_data + rest_of_block

                # Now parse complete header with TOW/WNc
                header = self.parse_header(block_data)
                if not header:
                    continue

                # Filter by block ID if specified
                if block_ids and header.block_id not in block_ids:
                    continue

                # Parse specific block types
                parsed_data = None
                if header.block_id == BLOCK_ID_RECEIVER_STATUS:
                    parsed_data = self.parse_receiver_status(block_data, header)
                elif header.block_id == BLOCK_ID_POWER_STATUS:
                    parsed_data = self.parse_power_status(block_data, header)

                if parsed_data:
                    blocks.append((header, parsed_data))

        return blocks

    def extract_power_status(self) -> List[PowerStatusData]:
        """Extract PowerStatus blocks from file."""
        blocks = self.read_blocks([BLOCK_ID_POWER_STATUS])
        return [data for _, data in blocks]

    def extract_receiver_status(self) -> List[ReceiverStatusData]:
        """Extract ReceiverStatus blocks from file."""
        blocks = self.read_blocks([BLOCK_ID_RECEIVER_STATUS])
        return [data for _, data in blocks]


def main():
    """Example usage."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sbf_parser.py <sbf_file>")
        sys.exit(1)

    sbf_file = Path(sys.argv[1])
    parser = SBFParser(sbf_file)

    print(f"Parsing: {sbf_file}")

    # Extract ReceiverStatus
    print("\n=== ReceiverStatus ===")
    receiver_status = parser.extract_receiver_status()
    print(f"Found {len(receiver_status)} ReceiverStatus blocks")
    if receiver_status:
        for i, data in enumerate(receiver_status[:3]):
            print(
                f"  [{i}] {data.datetime} - CPU: {data.cpu_load}%, Uptime: {data.uptime}s"
            )

    # Extract PowerStatus
    print("\n=== PowerStatus ===")
    power_status = parser.extract_power_status()
    print(f"Found {len(power_status)} PowerStatus blocks")
    if power_status:
        for i, data in enumerate(power_status[:3]):
            print(
                f"  [{i}] {data.datetime} - Vint: {data.voltage_internal:.2f}V, "
                f"Vext: {data.voltage_external:.2f}V, Source: {data.power_source}"
            )


if __name__ == "__main__":
    main()
