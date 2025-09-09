#!/usr/bin/env python3
"""
SBF (Septentrio Binary Format) Parser for Status/Health Data

This module provides basic SBF parsing capabilities to extract health and status
information from Septentrio GNSS receiver binary log files, specifically for
PolaRx5 status session data.
"""

import struct
import gzip
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Union
import logging


class SBFParser:
    """Basic SBF parser for extracting status and health data."""
    
    # SBF Message IDs for status/health data (from Septentrio documentation)
    SBF_MSG_IDS = {
        0x4013: "ReceiverTime",
        0x4014: "ReceiverStatus", 
        0x4015: "SatVisibility",
        0x4016: "ChannelStatus",
        0x4017: "ReceiverSetup",
        0x4018: "QualityInd",
        0x401A: "IPStatus",
        0x401B: "NTRIPClientStatus", 
        0x401C: "WiFiAPStatus",
        0x401D: "DiskStatus",
        0x401E: "NTRIPServerStatus",
        0x401F: "PowerStatus",
        0x4020: "LogStatus",
        0x4021: "SystemInfo",
    }
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize SBF parser.
        
        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)
        
    def parse_file(self, sbf_path: Union[str, Path]) -> Dict[str, Any]:
        """Parse SBF file and extract status/health messages.
        
        Args:
            sbf_path: Path to SBF file (can be .gz compressed)
            
        Returns:
            Dictionary with parsed messages and metadata
        """
        sbf_path = Path(sbf_path)
        
        if not sbf_path.exists():
            raise FileNotFoundError(f"SBF file not found: {sbf_path}")
            
        # Handle compressed files
        if sbf_path.suffix == '.gz':
            opener = gzip.open
        else:
            opener = open
            
        try:
            with opener(sbf_path, 'rb') as f:
                data = f.read()
                
            return self._parse_sbf_data(data)
            
        except Exception as e:
            self.logger.error(f"Failed to parse SBF file {sbf_path}: {e}")
            raise
            
    def _parse_sbf_data(self, data: bytes) -> Dict[str, Any]:
        """Parse SBF binary data.
        
        Args:
            data: Raw SBF binary data
            
        Returns:
            Dictionary with parsed messages
        """
        messages = []
        pos = 0
        
        while pos < len(data) - 8:  # Minimum header size
            # Look for SBF sync pattern: $@ (0x24, 0x40)
            if pos + 1 < len(data) and data[pos] == 0x24 and data[pos + 1] == 0x40:
                try:
                    message, bytes_consumed = self._parse_message(data[pos:])
                    if message:
                        messages.append(message)
                    pos += bytes_consumed if bytes_consumed > 0 else 1
                except Exception as e:
                    self.logger.debug(f"Error parsing message at pos {pos}: {e}")
                    pos += 1
            else:
                pos += 1
                
        return {
            "file_info": {
                "total_bytes": len(data),
                "messages_found": len(messages),
                "parse_timestamp": datetime.now(timezone.utc).isoformat()
            },
            "messages": messages
        }
        
    def _parse_message(self, data: bytes) -> tuple[Optional[Dict[str, Any]], int]:
        """Parse a single SBF message.
        
        Args:
            data: Raw message data starting with sync pattern
            
        Returns:
            Tuple of (message_dict, bytes_consumed)
        """
        if len(data) < 8:
            return None, 0
            
        # SBF Header format: $@ + CRC + ID + Length
        try:
            sync, crc, msg_id, length = struct.unpack('<2sHHH', data[:8])
            
            if sync != b'$@':
                return None, 0
                
            if length > len(data):
                self.logger.debug(f"Message length {length} exceeds available data")
                return None, 0
                
            # Extract message payload
            payload = data[8:length] if length > 8 else b''
            
            # Parse based on message type
            message = {
                "sync": sync.decode('ascii'),
                "crc": crc,
                "msg_id": f"0x{msg_id:04X}",
                "msg_name": self.SBF_MSG_IDS.get(msg_id, f"Unknown_{msg_id:04X}"),
                "length": length,
                "timestamp": self._extract_timestamp(payload) if len(payload) >= 4 else None,
                "payload_size": len(payload)
            }
            
            # Add specific parsing for known health messages
            if msg_id in self.SBF_MSG_IDS:
                parsed_data = self._parse_specific_message(msg_id, payload)
                if parsed_data:
                    message["data"] = parsed_data
                    
            return message, length
            
        except struct.error as e:
            self.logger.debug(f"Struct unpack error: {e}")
            return None, 0
            
    def _extract_timestamp(self, payload: bytes) -> Optional[str]:
        """Extract GPS timestamp from message payload.
        
        Args:
            payload: Message payload bytes
            
        Returns:
            ISO format timestamp string or None
        """
        if len(payload) < 4:
            return None
            
        try:
            # First 4 bytes usually contain GPS time (TOW in milliseconds)
            tow_ms = struct.unpack('<I', payload[:4])[0]
            
            # Convert GPS TOW to approximate UTC timestamp
            # Note: This is simplified - proper conversion requires GPS week
            gps_epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)
            seconds = tow_ms / 1000.0
            
            # Approximate current GPS week (simplified)
            current_time = datetime.now(timezone.utc)
            weeks_since_gps = (current_time - gps_epoch).days // 7
            
            timestamp = gps_epoch + datetime.timedelta(weeks=weeks_since_gps, seconds=seconds)
            return timestamp.isoformat()
            
        except (struct.error, ValueError) as e:
            self.logger.debug(f"Timestamp extraction error: {e}")
            return None
            
    def _parse_specific_message(self, msg_id: int, payload: bytes) -> Optional[Dict[str, Any]]:
        """Parse specific message types for detailed health data.
        
        Args:
            msg_id: SBF message ID
            payload: Message payload bytes
            
        Returns:
            Parsed message data or None
        """
        parsers = {
            0x4013: self._parse_receiver_time,
            0x4014: self._parse_receiver_status,
            0x4015: self._parse_sat_visibility,
            0x4018: self._parse_quality_ind,
            0x401D: self._parse_disk_status,
            0x401F: self._parse_power_status,
        }
        
        parser = parsers.get(msg_id)
        if parser and len(payload) >= 4:
            try:
                return parser(payload)
            except Exception as e:
                self.logger.debug(f"Error parsing message 0x{msg_id:04X}: {e}")
                
        return None
        
    def _parse_receiver_time(self, payload: bytes) -> Dict[str, Any]:
        """Parse ReceiverTime message."""
        if len(payload) < 12:
            return {"error": "Insufficient payload length"}
            
        tow, week_nb = struct.unpack('<II', payload[:8])
        return {
            "time_of_week_ms": tow,
            "gps_week": week_nb,
            "raw_payload_hex": payload[:16].hex() if len(payload) >= 16 else payload.hex()
        }
        
    def _parse_receiver_status(self, payload: bytes) -> Dict[str, Any]:
        """Parse ReceiverStatus message."""
        if len(payload) < 8:
            return {"error": "Insufficient payload length"}
            
        tow, status_word = struct.unpack('<II', payload[:8])
        
        # Decode status bits (simplified)
        return {
            "time_of_week_ms": tow,
            "status_word": f"0x{status_word:08X}",
            "status_bits": {
                "antenna_power": bool(status_word & 0x01),
                "antenna_connected": bool(status_word & 0x02),
                "cpu_overload": bool(status_word & 0x10),
                "disk_full": bool(status_word & 0x20),
            },
            "raw_payload_hex": payload[:12].hex() if len(payload) >= 12 else payload.hex()
        }
        
    def _parse_sat_visibility(self, payload: bytes) -> Dict[str, Any]:
        """Parse SatVisibility message."""
        if len(payload) < 8:
            return {"error": "Insufficient payload length"}
            
        tow, num_sats = struct.unpack('<IH', payload[:6])
        return {
            "time_of_week_ms": tow,
            "num_satellites": num_sats,
            "raw_payload_hex": payload[:16].hex() if len(payload) >= 16 else payload.hex()
        }
        
    def _parse_quality_ind(self, payload: bytes) -> Dict[str, Any]:
        """Parse QualityInd message."""
        if len(payload) < 12:
            return {"error": "Insufficient payload length"}
            
        tow, indicators = struct.unpack('<IH', payload[:6])
        return {
            "time_of_week_ms": tow,
            "quality_indicators": f"0x{indicators:04X}",
            "raw_payload_hex": payload[:16].hex() if len(payload) >= 16 else payload.hex()
        }
        
    def _parse_disk_status(self, payload: bytes) -> Dict[str, Any]:
        """Parse DiskStatus message."""
        if len(payload) < 12:
            return {"error": "Insufficient payload length"}
            
        tow = struct.unpack('<I', payload[:4])[0]
        return {
            "time_of_week_ms": tow,
            "raw_payload_hex": payload[:20].hex() if len(payload) >= 20 else payload.hex()
        }
        
    def _parse_power_status(self, payload: bytes) -> Dict[str, Any]:
        """Parse PowerStatus message."""
        if len(payload) < 8:
            return {"error": "Insufficient payload length"}
            
        tow = struct.unpack('<I', payload[:4])[0]
        return {
            "time_of_week_ms": tow,
            "raw_payload_hex": payload[:16].hex() if len(payload) >= 16 else payload.hex()
        }

    def convert_to_ascii(self, parsed_data: Dict[str, Any], output_path: Optional[Union[str, Path]] = None) -> str:
        """Convert parsed SBF data to ASCII format.
        
        Args:
            parsed_data: Parsed SBF data from parse_file()
            output_path: Optional path to save ASCII output
            
        Returns:
            ASCII representation of the data
        """
        lines = []
        
        # Header information
        file_info = parsed_data.get("file_info", {})
        lines.append(f"# SBF Status File Analysis")
        lines.append(f"# Parse timestamp: {file_info.get('parse_timestamp', 'unknown')}")
        lines.append(f"# Total bytes: {file_info.get('total_bytes', 0)}")
        lines.append(f"# Messages found: {file_info.get('messages_found', 0)}")
        lines.append("")
        
        # Message data
        messages = parsed_data.get("messages", [])
        for i, msg in enumerate(messages):
            lines.append(f"## Message {i+1}: {msg.get('msg_name', 'Unknown')}")
            lines.append(f"ID: {msg.get('msg_id', 'unknown')}")
            lines.append(f"Length: {msg.get('length', 0)} bytes")
            lines.append(f"CRC: 0x{msg.get('crc', 0):04X}")
            
            if msg.get('timestamp'):
                lines.append(f"Timestamp: {msg['timestamp']}")
                
            # Add parsed data if available
            if 'data' in msg and msg['data']:
                lines.append("Parsed Data:")
                data = msg['data']
                for key, value in data.items():
                    lines.append(f"  {key}: {value}")
                    
            lines.append("")
            
        ascii_output = "\n".join(lines)
        
        # Save to file if requested
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                f.write(ascii_output)
                
        return ascii_output


def main():
    """Command line interface for SBF parsing."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Parse SBF status files to ASCII")
    parser.add_argument("sbf_file", help="Input SBF file (can be .gz compressed)")
    parser.add_argument("-o", "--output", help="Output ASCII file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
    
    # Parse SBF file
    parser = SBFParser()
    
    try:
        parsed_data = parser.parse_file(args.sbf_file)
        ascii_output = parser.convert_to_ascii(parsed_data, args.output)
        
        if not args.output:
            print(ascii_output)
            
        print(f"Parsed {len(parsed_data.get('messages', []))} messages from {args.sbf_file}")
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
        
    return 0


if __name__ == "__main__":
    exit(main())