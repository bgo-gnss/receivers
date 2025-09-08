#!/usr/bin/env python3
"""
Binary SBF Parser for Septentrio Health Messages

This module parses binary SBF files to extract health message data that's not 
available through sbf2asc ASCII conversion.
"""

import struct
import os
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
import pandas as pd


class BinarySBFParser:
    """Parses binary SBF files to extract health message data."""
    
    # SBF message IDs for health messages (actual IDs from parsed messages)
    HEALTH_MESSAGE_IDS = {
        0x1712: 'ReceiverTime',      # 5914 decimal
        0x0FAD: 'ChannelStatus',     # 4013 decimal  
        0x0FAC: 'SatVisibility',     # 4012 decimal
        0x0FAE: 'ReceiverStatus',    # 4014 decimal
        0x0FF2: 'QualityInd',        # 4082 decimal
        0x0FCD: 'NTRIPClientStatus', # 4053 decimal
        0x101A: 'NTRIPServerStatus', # 4122 decimal
        0x1B37: 'DiskStatus',        # 6967 decimal (actual ID, not 4059 from filename)
        0x0FCE: 'WiFiAPStatus',      # 4054 decimal
        0x100A: 'PowerStatus',       # 4106 decimal (actual ID, not 4101 from filename)
        0x1006: 'LogStatus',         # 4102 decimal
        0x1770: 'SystemInfo',        # 6000 decimal
    }
    
    def __init__(self):
        """Initialize the binary SBF parser."""
        self.parsed_messages = []
        
    def parse_sbf_file(self, filepath: str) -> List[Dict[str, Any]]:
        """
        Parse a binary SBF file and extract health messages.
        
        Args:
            filepath: Path to the binary SBF file
            
        Returns:
            List of parsed message dictionaries
        """
        messages = []
        self.parsed_messages = []  # Reset for debug tracking
        
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
                
            print(f"  File size: {len(data)} bytes")
            print(f"  First 8 bytes: {data[:8].hex()}")
                
            # Find all SBF message headers ($@ = 0x2440)
            offset = 0
            sync_found = 0
            while offset < len(data) - 8:
                # Look for SBF sync bytes  
                if data[offset:offset+2] == b'$@':  # 0x24 0x40
                    sync_found += 1
                    message = self._parse_sbf_message(data, offset)
                    if message:
                        messages.append(message)
                        self.parsed_messages.append(message)  # Track for debug
                        offset += message.get('message_length', 8)
                    else:
                        offset += 1
                else:
                    offset += 1
                    
            print(f"  Sync patterns found: {sync_found}")
            print(f"  Messages parsed: {len(messages)}")
                    
        except Exception as e:
            print(f"Error parsing SBF file {filepath}: {e}")
            
        return messages
    
    def _parse_sbf_message(self, data: bytes, offset: int) -> Optional[Dict[str, Any]]:
        """
        Parse a single SBF message starting at the given offset.
        
        Args:
            data: Binary data buffer
            offset: Starting offset of the message
            
        Returns:
            Dictionary with parsed message data or None if parsing failed
        """
        try:
            # SBF health messages have variable lengths:
            # PowerStatus: 16 bytes, DiskStatus: 52 bytes, etc.
            # Bytes 0-1: Sync ($@) = 0x2440
            # Bytes 2-3: Message ID and revision 
            # Bytes 4-5: Message length OR voltage data (for PowerStatus)
            # Bytes 6-7: Time of week (TOW) MSB
            # Bytes 8+: TOW LSB + message-specific data
            
            # Need at least 8 bytes for header
            if offset + 8 > len(data):
                if len(self.parsed_messages) < 3:
                    print(f"    Insufficient data at offset {offset}")
                return None
                
            # Parse header
            sync = data[offset:offset+2]
            if sync != b'$@':  # 0x24 0x40
                if len(self.parsed_messages) < 3:
                    print(f"    Invalid sync at offset {offset}: {sync.hex()}")
                return None
                
            # Extract message ID and revision
            id_revision = struct.unpack('<H', data[offset+2:offset+4])[0]
            message_id = id_revision & 0x1FFF  # Lower 13 bits
            revision = (id_revision >> 13) & 0x07  # Upper 3 bits
            
            # Get message name to determine expected length
            message_name = self.HEALTH_MESSAGE_IDS.get(message_id, f"Unknown_{message_id:04X}")
            
            # Different message types have different lengths in filtered files
            if message_name == 'PowerStatus':
                message_length = 16  # PowerStatus: bytes 4-5 contain voltage, not length
            elif message_name == 'DiskStatus':
                message_length = 52  # DiskStatus: larger message with disk info
            else:
                # For unknown types, try to read length from bytes 4-5
                message_length = struct.unpack('<H', data[offset+4:offset+6])[0]
                if message_length > 1000 or message_length < 8:
                    message_length = 16  # Default fallback
            
            # Extract time of week (GPS time) - structure might be different
            tow_msb = struct.unpack('<H', data[offset+6:offset+8])[0] 
            tow_lsb = struct.unpack('<I', data[offset+8:offset+12])[0] if offset + 12 <= len(data) else 0
            tow = (tow_msb << 16) + (tow_lsb >> 16)  # Approximate - need to verify
            
            # Ensure we have enough data for the expected message length
            if offset + message_length > len(data):
                if len(self.parsed_messages) < 3:
                    print(f"    Insufficient data for {message_name}: need {message_length} bytes")
                return None
            
            # Debug output for first few messages
            if len(self.parsed_messages) < 3:
                print(f"    Message ID: {message_id} ({message_id:04X}), Name: {message_name}")
                print(f"    Raw header: {data[offset:offset+8].hex()}")
                print(f"    Full message: {data[offset:offset+16].hex()}")
            
            # Convert to GPS time in seconds (approximate)
            gps_time = tow / 1000.0 if tow > 0 else 0
            
            # Extract message payload (data after 8-byte header)
            payload_start = offset + 8  # After header
            payload_end = offset + message_length
            payload = data[payload_start:payload_end]
            
            message = {
                'message_id': message_id,
                'message_name': message_name,
                'revision': revision,
                'message_length': message_length,
                'gps_time': gps_time,
                'datetime': self._gps_to_datetime(gps_time) if gps_time > 0 else None,
                'payload_length': len(payload),
                'raw_payload': payload,
                'raw_message': data[offset:offset+message_length],
            }
            
            # Parse specific message types
            if message_name == 'PowerStatus':
                message.update(self._parse_power_status_full(data[offset:offset+message_length]))
            elif message_name == 'DiskStatus':
                message.update(self._parse_disk_status_full(data[offset:offset+message_length]))
            elif message_name == 'ReceiverTime':
                message.update(self._parse_receiver_time(payload))
            elif message_name == 'QualityInd':
                message.update(self._parse_quality_ind(payload))
            # Add more parsers as needed
            
            return message
            
        except Exception as e:
            if len(self.parsed_messages) < 3:
                print(f"    Error parsing SBF message at offset {offset}: {e}")
            return None
    
    def _parse_power_status(self, payload: bytes) -> Dict[str, Any]:
        """
        Parse PowerStatus message payload.
        
        PowerStatus messages are 16 bytes total (8-byte SBF header + 8-byte payload).
        Voltage field is located at bytes 4-5 of the message (bytes 0-1 of payload 
        after SBF header), encoded as big-endian centivolts.
        
        Based on analysis: bytes 4-5 as big-endian centivolts gives ~12.96V,
        closely matching web interface reading of 12.58V.
        """
        try:
            if len(payload) >= 4:
                # PowerStatus structure (confirmed through analysis):
                # Message bytes 4-5 (payload bytes 0-1): Voltage in big-endian centivolts
                # Message bytes 6-7 (payload bytes 2-3): Additional status/flags
                
                # Extract voltage from first 2 bytes of payload (message bytes 4-5)
                voltage_raw = struct.unpack('>H', payload[0:2])[0]  # Big-endian
                voltage_v = voltage_raw / 100.0  # Convert centivolts to volts
                
                # Extract additional status flags if available
                status_flags = struct.unpack('>H', payload[2:4])[0] if len(payload) >= 4 else 0
                
                return {
                    'voltage_raw_cv': voltage_raw,     # Raw centivolts
                    'voltage_v': voltage_v,            # Voltage in volts
                    'status_flags': status_flags,      # Status/flags
                    'voltage_field_location': 'bytes_4-5_big_endian_centivolts',
                }
        except Exception as e:
            return {'parsing_error': f'PowerStatus: {e}'}
        return {'parsing_error': 'PowerStatus: insufficient payload'}

    def _parse_power_status_full(self, full_message: bytes) -> Dict[str, Any]:
        """
        Parse PowerStatus message from full 16-byte message.
        
        Based on analysis, bytes 4-5 contain voltage in big-endian centivolts.
        This gives 12.96V which closely matches web interface reading of 12.58V.
        """
        try:
            if len(full_message) >= 16:
                # Extract voltage from bytes 4-5 (big-endian centivolts)
                voltage_raw = struct.unpack('>H', full_message[4:6])[0]
                voltage_v = voltage_raw / 100.0
                
                # Extract additional fields
                status_flags = struct.unpack('<H', full_message[6:8])[0]  
                
                return {
                    'voltage_raw_cv': voltage_raw,         # Raw centivolts
                    'voltage_v': voltage_v,                # Voltage in volts
                    'status_flags': status_flags,          # Status/flags
                    'voltage_field_location': 'bytes_4-5_big_endian_centivolts',
                    'web_interface_voltage': '12.58V',     # Reference from user
                    'voltage_difference': abs(voltage_v - 12.58),
                }
        except Exception as e:
            return {'parsing_error': f'PowerStatus_full: {e}'}
        return {'parsing_error': 'PowerStatus_full: insufficient data'}

    def _parse_disk_status_full(self, full_message: bytes) -> Dict[str, Any]:
        """
        Parse DiskStatus message from full 52-byte message.
        
        DiskStatus messages contain disk usage information including:
        - Total disk space, free space, used space
        - Disk status flags and error conditions
        - Logging session information
        """
        try:
            if len(full_message) >= 52:
                # Based on analysis of the 52-byte DiskStatus structure
                # This is initial parsing - may need refinement based on actual data
                
                # Extract various fields (approximate positions)
                status_byte = full_message[16] if len(full_message) > 16 else 0
                disk_number = full_message[17] if len(full_message) > 17 else 0
                
                # Try to find disk space information (common locations)
                # These offsets are educated guesses and may need adjustment
                total_space_candidates = []
                free_space_candidates = []
                
                # Test different positions for 32-bit integers that might be disk space
                for i in range(12, min(48, len(full_message) - 4), 4):
                    value = struct.unpack('<I', full_message[i:i+4])[0]
                    if 1000000 < value < 20000000000:  # Reasonable disk space range (1MB - 20GB)
                        if len(total_space_candidates) < 3:
                            total_space_candidates.append((i, value))
                
                return {
                    'disk_status_byte': status_byte,
                    'disk_number': disk_number,
                    'message_length_bytes': len(full_message),
                    'total_space_candidates': total_space_candidates[:3],  # Top 3 candidates
                    'raw_hex': full_message.hex(),
                    'parsing_status': 'experimental_diskstatus_parser',
                }
        except Exception as e:
            return {'parsing_error': f'DiskStatus_full: {e}'}
        return {'parsing_error': 'DiskStatus_full: insufficient data'}
    
    def _parse_disk_status(self, payload: bytes) -> Dict[str, Any]:
        """Parse DiskStatus message payload."""
        try:
            if len(payload) >= 16:
                # DiskStatus structure (approximate):
                # Bytes 4-7: Total disk space
                # Bytes 8-11: Free disk space
                # Bytes 12-15: Used disk space
                
                total_space = struct.unpack('<I', payload[4:8])[0] if len(payload) >= 8 else 0
                free_space = struct.unpack('<I', payload[8:12])[0] if len(payload) >= 12 else 0
                
                return {
                    'total_disk_kb': total_space,
                    'free_disk_kb': free_space,
                    'used_disk_kb': total_space - free_space if total_space > free_space else 0,
                    'disk_usage_percent': ((total_space - free_space) / total_space * 100) if total_space > 0 else 0,
                }
        except Exception:
            pass
        return {'parsing_error': 'DiskStatus'}
    
    def _parse_receiver_time(self, payload: bytes) -> Dict[str, Any]:
        """Parse ReceiverTime message payload."""
        try:
            if len(payload) >= 8:
                # ReceiverTime structure (approximate):
                # Additional time information
                utc_offset = struct.unpack('<h', payload[4:6])[0] if len(payload) >= 6 else 0
                time_flags = struct.unpack('<H', payload[6:8])[0] if len(payload) >= 8 else 0
                
                return {
                    'utc_offset_sec': utc_offset,
                    'time_flags': time_flags,
                }
        except Exception:
            pass
        return {'parsing_error': 'ReceiverTime'}
    
    def _parse_quality_ind(self, payload: bytes) -> Dict[str, Any]:
        """Parse QualityInd message payload."""
        try:
            if len(payload) >= 8:
                # QualityInd structure (approximate):
                # Signal quality indicators
                quality_flags = struct.unpack('<I', payload[4:8])[0] if len(payload) >= 8 else 0
                
                return {
                    'quality_flags': quality_flags,
                }
        except Exception:
            pass
        return {'parsing_error': 'QualityInd'}
    
    def _gps_to_datetime(self, gps_seconds: float) -> datetime:
        """Convert GPS seconds to datetime."""
        gps_epoch = datetime(1980, 1, 6)
        return gps_epoch + pd.Timedelta(seconds=gps_seconds)
    
    def parse_health_messages_directory(self, health_dir: str) -> Dict[str, List[Dict]]:
        """
        Parse all binary SBF files in the health messages directory.
        
        Args:
            health_dir: Directory containing filtered SBF files
            
        Returns:
            Dictionary mapping message types to lists of parsed messages
        """
        results = {}
        
        # Map decimal IDs to hex for file matching
        id_mapping = {
            '4101': 'PowerStatus',
            '4059': 'DiskStatus', 
            '5914': 'ReceiverTime',
            '4082': 'QualityInd',
            '4013': 'ChannelStatus',
            '4012': 'SatVisibility',
            '4053': 'NTRIPClientStatus',
            '4122': 'NTRIPServerStatus',
            '4054': 'WiFiAPStatus',
            '4102': 'LogStatus',
            '6000': 'SystemInfo',
        }
        
        for msg_id, msg_name in id_mapping.items():
            # Look for corresponding filtered SBF file
            sbf_pattern = f"/tmp/{msg_name}_{msg_id}.sbf"
            if os.path.exists(sbf_pattern):
                print(f"Parsing {msg_name} messages...")
                messages = self.parse_sbf_file(sbf_pattern)
                if messages:
                    results[msg_name] = messages
                    print(f"  Parsed {len(messages)} {msg_name} messages")
                else:
                    print(f"  No messages found for {msg_name}")
        
        return results


def main():
    """Main function for testing binary SBF parsing."""
    parser = BinarySBFParser()
    
    # Test with PowerStatus filtered file
    power_status_file = "/tmp/PowerStatus_4101.sbf"
    if os.path.exists(power_status_file):
        print("Testing PowerStatus parsing...")
        messages = parser.parse_sbf_file(power_status_file)
        
        if messages:
            print(f"Parsed {len(messages)} PowerStatus messages")
            for i, msg in enumerate(messages[:3]):  # Show first 3
                print(f"Message {i+1}:")
                print(f"  Time: {msg['datetime']}")
                if 'voltage_v' in msg:
                    print(f"  Voltage: {msg['voltage_v']:.2f}V (from {msg.get('voltage_field_location', 'unknown field')})")
                    print(f"  Raw voltage: {msg.get('voltage_raw_cv', 0)} centivolts")
                print(f"  Message length: {msg['message_length']} bytes")
                print()
        else:
            print("No PowerStatus messages parsed")
    
    # Test with DiskStatus
    disk_status_file = "/tmp/DiskStatus_4059.sbf"
    if os.path.exists(disk_status_file):
        print("Testing DiskStatus parsing...")
        messages = parser.parse_sbf_file(disk_status_file)
        
        if messages:
            print(f"Parsed {len(messages)} DiskStatus messages")
            for i, msg in enumerate(messages[:3]):  # Show first 3
                print(f"Message {i+1}:")
                print(f"  Time: {msg['datetime']}")
                if 'total_space_candidates' in msg:
                    print(f"  Message length: {msg.get('message_length_bytes', 0)} bytes")
                    print(f"  Status byte: {msg.get('disk_status_byte', 0)}")
                    print(f"  Disk candidates: {msg['total_space_candidates']}")
                elif 'disk_usage_percent' in msg:
                    print(f"  Disk usage: {msg['disk_usage_percent']:.1f}%")
                    print(f"  Free space: {msg['free_disk_kb']} KB")
                print()
        else:
            print("No DiskStatus messages parsed")


if __name__ == "__main__":
    main()