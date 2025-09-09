#!/usr/bin/env python3
"""
Comprehensive Health Data Analyzer for Septentrio PolaRX5

This module analyzes comprehensive health data from SBF status sessions including:
- ReceiverStatus (CPU, uptime, status codes)
- Message block availability and frequency
- Health message types and timing analysis

Combines data from:
- sbf2asc ASCII output (ReceiverStatus values)
- sbfblocks detailed block information (message types and timing)
- sbfblocks summary information (message counts and availability)
"""

import os
import glob
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Set
import pandas as pd
from collections import defaultdict


class ComprehensiveHealthAnalyzer:
    """Analyzes comprehensive health data from Septentrio PolaRX5 status sessions."""
    
    # SBF Message type mapping based on PolaRX5 documentation and Stream7 configuration
    HEALTH_MESSAGE_TYPES = {
        '5914': {'name': 'ReceiverTime', 'description': 'Current receiver and UTC time'},
        '4013': {'name': 'ChannelStatus', 'description': 'Status of the tracking for all receiver channels'},
        '4012': {'name': 'SatVisibility', 'description': 'Azimuth/elevation of visible satellites'},
        '4014': {'name': 'ReceiverStatus', 'description': 'Overall status information of the receiver'},
        '4082': {'name': 'QualityInd', 'description': 'Quality indicators'},
        '4053': {'name': 'NTRIPClientStatus', 'description': 'NTRIP client connection status'},
        '4122': {'name': 'NTRIPServerStatus', 'description': 'NTRIP server connection status'},
        '4059': {'name': 'DiskStatus', 'description': 'Internal logging status'},
        '4054': {'name': 'WiFiAPStatus', 'description': 'WiFi status in access point mode'},
        '4101': {'name': 'PowerStatus', 'description': 'Power supply source and voltage'},
        '4102': {'name': 'LogStatus', 'description': 'Log sessions status'},
        '6000': {'name': 'SystemInfo', 'description': 'System parameters for maintenance and support'},
        '4015': {'name': 'Commands', 'description': 'Commands entered by the user'},
        '5902': {'name': 'ReceiverSetup', 'description': 'General information about the receiver installation'},
    }
    
    def __init__(self, data_base_dir: str):
        """
        Initialize comprehensive health analyzer.
        
        Args:
            data_base_dir: Base directory containing ascii/, blocks/, and other subdirectories
        """
        self.data_base_dir = data_base_dir
        self.ascii_dir = os.path.join(data_base_dir, 'ascii')
        self.blocks_dir = os.path.join(data_base_dir, 'ascii_blocks') 
        
        # Parsed data storage
        self.receiver_status_data = []
        self.message_block_data = []
        self.message_summaries = {}
        
    def load_receiver_status_data(self) -> None:
        """Load ReceiverStatus data from enhanced ASCII files (sbf2asc output)."""
        ascii_files = glob.glob(os.path.join(self.ascii_dir, "*_enhanced_receiver_status.asc"))
        ascii_files.sort()
        
        print(f"Loading ReceiverStatus data from {len(ascii_files)} ASCII files...")
        
        for filepath in ascii_files:
            filename = os.path.basename(filepath)
            records = self._parse_receiver_status_ascii(filepath, filename)
            self.receiver_status_data.extend(records)
        
        print(f"Loaded {len(self.receiver_status_data)} ReceiverStatus records")
    
    def _parse_receiver_status_ascii(self, filepath: str, source_file: str) -> List[Dict]:
        """Parse ReceiverStatus ASCII file (sbf2asc -t output)."""
        records = []
        
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    
                    # Check if this is a ReceiverStatus block (starts with -7)
                    if len(parts) >= 5 and parts[0] == '-7':
                        try:
                            record = {
                                'block_type': int(parts[0]),  # -7 for ReceiverStatus
                                'gps_time': float(parts[1]),  # GPS seconds since Jan 06, 1980
                                'cpu_load': int(parts[2]),    # CPU load percentage
                                'uptime': int(parts[3]),      # Uptime in seconds
                                'rx_status': parts[4],        # Receiver status in hex
                                'source_file': source_file,
                                'datetime': self._gps_to_datetime(float(parts[1])),
                                'message_type': 'ReceiverStatus',
                                'total_fields': len(parts)   # Track how many fields we have
                            }
                            
                            # Store additional fields if available (enhanced format has 16 total)
                            if len(parts) >= 16:
                                for i in range(5, len(parts)):
                                    record[f'field_{i}'] = parts[i]  # Store additional fields
                            
                            records.append(record)
                        except (ValueError, IndexError):
                            continue
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            
        return records
    
    def load_message_block_data(self) -> None:
        """Load message block data from sbfblocks detailed output."""
        blocks_files = glob.glob(os.path.join(self.blocks_dir, "*_blocks.txt"))
        blocks_files.sort()
        
        print(f"Loading message block data from {len(blocks_files)} block files...")
        
        for filepath in blocks_files:
            filename = os.path.basename(filepath)
            records = self._parse_message_blocks(filepath, filename)
            self.message_block_data.extend(records)
        
        print(f"Loaded {len(self.message_block_data)} message block records")
    
    def _parse_message_blocks(self, filepath: str, source_file: str) -> List[Dict]:
        """Parse message block detailed information from sbfblocks output."""
        records = []
        
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    
                    # Skip summary sections and empty lines
                    if not line or line.startswith('*') or 'Summary' in line or 'Total' in line:
                        continue
                    
                    # Parse block information lines
                    # Format: timestamp [hex_id][dec_id] [type] MessageName = Description
                    match = re.match(r'^([0-9.]+)\s+\[([0-9A-F]+)\]\[([0-9]+)\]\s+\[([^\]]+)\]\s+([^=]+)\s*=\s*(.+)$', line)
                    if match:
                        gps_time = float(match.group(1))
                        hex_id = match.group(2)
                        dec_id = match.group(3)
                        msg_type = match.group(4).strip()
                        msg_name = match.group(5).strip()
                        description = match.group(6).strip()
                        
                        # Check if this is a health-related message type
                        if dec_id in self.HEALTH_MESSAGE_TYPES:
                            record = {
                                'gps_time': gps_time,
                                'datetime': self._gps_to_datetime(gps_time),
                                'hex_id': hex_id,
                                'decimal_id': dec_id,
                                'message_type': msg_type,
                                'message_name': msg_name,
                                'description': description,
                                'source_file': source_file,
                                'health_message_name': self.HEALTH_MESSAGE_TYPES[dec_id]['name']
                            }
                            records.append(record)
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")
            
        return records
    
    def load_message_summaries(self) -> None:
        """Load message summary data from sbfblocks summary output."""
        summary_files = glob.glob(os.path.join(self.blocks_dir, "*_summary.txt"))
        summary_files.sort()
        
        print(f"Loading message summaries from {len(summary_files)} summary files...")
        
        for filepath in summary_files:
            filename = os.path.basename(filepath)
            summary = self._parse_message_summary(filepath, filename)
            if summary:
                self.message_summaries[filename] = summary
        
        print(f"Loaded summaries for {len(self.message_summaries)} files")
    
    def _parse_message_summary(self, filepath: str, source_file: str) -> Dict:
        """Parse message summary from sbfblocks summary output."""
        summary = {
            'source_file': source_file,
            'message_counts': {},
            'health_messages': {},
            'total_blocks': 0,
            'total_message_types': 0,
            'crc_errors': 0
        }
        
        try:
            with open(filepath, 'r') as f:
                content = f.read()
                
                # Extract message block counts
                for line in content.split('\n'):
                    # Parse block count lines: [hex][dec]_Block_Count= N ( description )
                    match = re.match(r'^\[([0-9A-F]+)\]\[([0-9]+)\]_Block_Count=\s+(\d+)\s+\(\s*(.+)\)$', line.strip())
                    if match:
                        hex_id = match.group(1)
                        dec_id = match.group(2)
                        count = int(match.group(3))
                        description = match.group(4).strip()
                        
                        summary['message_counts'][dec_id] = {
                            'hex_id': hex_id,
                            'count': count,
                            'description': description
                        }
                        
                        # Check if this is a health message
                        if dec_id in self.HEALTH_MESSAGE_TYPES:
                            summary['health_messages'][dec_id] = {
                                'name': self.HEALTH_MESSAGE_TYPES[dec_id]['name'],
                                'count': count,
                                'description': description
                            }
                    
                    # Extract totals
                    if 'Total of' in line and 'Different blocks found' in line:
                        match = re.search(r'Total of (\d+) Different blocks found', line)
                        if match:
                            summary['total_message_types'] = int(match.group(1))
                    
                    if 'Total of' in line and 'CRC errors found' in line:
                        match = re.search(r'Total of (\d+) CRC errors found', line)
                        if match:
                            summary['crc_errors'] = int(match.group(1))
        
        except Exception as e:
            print(f"Error parsing summary {filepath}: {e}")
            return None
            
        return summary
    
    def _gps_to_datetime(self, gps_seconds: float) -> datetime:
        """Convert GPS seconds to datetime."""
        gps_epoch = datetime(1980, 1, 6)
        return gps_epoch + pd.Timedelta(seconds=gps_seconds)
    
    def load_all_data(self) -> None:
        """Load all available health data."""
        self.load_receiver_status_data()
        self.load_message_block_data()
        self.load_message_summaries()
    
    def analyze_receiver_status(self) -> Dict:
        """Analyze ReceiverStatus data (CPU, uptime, status codes)."""
        if not self.receiver_status_data:
            return {"error": "No ReceiverStatus data available"}
        
        cpu_loads = [r['cpu_load'] for r in self.receiver_status_data]
        uptimes = [r['uptime'] for r in self.receiver_status_data]
        status_codes = [r['rx_status'] for r in self.receiver_status_data]
        
        status_counts = {}
        for status in status_codes:
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return {
            'total_records': len(self.receiver_status_data),
            'cpu_analysis': {
                'min': min(cpu_loads),
                'max': max(cpu_loads),
                'mean': sum(cpu_loads) / len(cpu_loads),
                'samples': len(cpu_loads)
            },
            'uptime_analysis': {
                'min_seconds': min(uptimes),
                'max_seconds': max(uptimes),
                'min_days': min(uptimes) / 86400,
                'max_days': max(uptimes) / 86400,
                'samples': len(uptimes)
            },
            'status_code_analysis': {
                'unique_codes': list(status_counts.keys()),
                'code_distribution': status_counts,
                'samples': len(status_codes)
            }
        }
    
    def analyze_health_message_availability(self) -> Dict:
        """Analyze availability and frequency of health-related message types."""
        if not self.message_summaries:
            return {"error": "No message summary data available"}
        
        # Aggregate data across all files
        message_availability = defaultdict(list)
        total_files = len(self.message_summaries)
        
        for filename, summary in self.message_summaries.items():
            health_messages = summary.get('health_messages', {})
            
            # Track which health messages are present in each file
            for msg_id, msg_info in self.HEALTH_MESSAGE_TYPES.items():
                if msg_id in health_messages:
                    message_availability[msg_info['name']].append(health_messages[msg_id]['count'])
                else:
                    message_availability[msg_info['name']].append(0)
        
        # Calculate statistics
        availability_stats = {}
        for msg_name, counts in message_availability.items():
            files_with_message = sum(1 for c in counts if c > 0)
            total_count = sum(counts)
            avg_count = sum(counts) / len(counts) if counts else 0
            
            availability_stats[msg_name] = {
                'files_present': files_with_message,
                'files_total': total_files,
                'availability_percentage': (files_with_message / total_files) * 100,
                'total_messages': total_count,
                'average_per_file': avg_count,
                'expected_per_file': 60  # 1-minute intervals for 1 hour
            }
        
        return {
            'message_availability': availability_stats,
            'files_analyzed': total_files,
            'health_message_types': len(self.HEALTH_MESSAGE_TYPES)
        }
    
    def generate_comprehensive_report(self) -> str:
        """Generate comprehensive health analysis report."""
        if not any([self.receiver_status_data, self.message_block_data, self.message_summaries]):
            return "No health data available for analysis"
        
        # Get analysis results
        receiver_analysis = self.analyze_receiver_status()
        availability_analysis = self.analyze_health_message_availability()
        
        # Calculate time span
        if self.receiver_status_data:
            timestamps = [r['datetime'] for r in self.receiver_status_data]
            time_span = max(timestamps) - min(timestamps)
            first_record = min(timestamps)
            last_record = max(timestamps)
        else:
            time_span = None
            first_record = None
            last_record = None
        
        report = f"""
Comprehensive PolaRX5 Health Analysis Report
==========================================

Data Summary:
- ReceiverStatus records: {len(self.receiver_status_data)}
- Message block records: {len(self.message_block_data)}
- Summary files: {len(self.message_summaries)}
- Time span: {time_span}
- First record: {first_record}
- Last record: {last_record}

ReceiverStatus Analysis:
"""
        
        if 'error' not in receiver_analysis:
            cpu = receiver_analysis['cpu_analysis']
            uptime = receiver_analysis['uptime_analysis']
            status = receiver_analysis['status_code_analysis']
            
            report += f"""- CPU Load: {cpu['min']}% - {cpu['max']}% (avg: {cpu['mean']:.1f}%)
- Uptime: {uptime['min_days']:.1f} - {uptime['max_days']:.1f} days
- Status codes: {len(status['unique_codes'])} unique codes
"""
            
            for code, count in status['code_distribution'].items():
                percentage = (count / len(self.receiver_status_data)) * 100
                report += f"  {code}: {count} occurrences ({percentage:.1f}%)\\n"
        
        report += "\\nHealth Message Availability:\\n"
        
        if 'error' not in availability_analysis:
            availability = availability_analysis['message_availability']
            
            for msg_name, stats in availability.items():
                report += f"- {msg_name}: {stats['availability_percentage']:.1f}% availability"
                report += f" ({stats['files_present']}/{stats['files_total']} files, "
                report += f"{stats['average_per_file']:.0f} msg/file)\\n"
        
        return report
    
    def save_comprehensive_csv(self, output_path: str) -> None:
        """Save comprehensive health data to CSV."""
        if not self.receiver_status_data:
            print("No ReceiverStatus data to save")
            return
        
        df = pd.DataFrame(self.receiver_status_data)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime')
        
        df.to_csv(output_path, index=False)
        print(f"Comprehensive health data saved to: {output_path}")


def main():
    """Main function for command-line usage."""
    data_base_dir = "data/2025/sep/ORFC/status_1hr"
    
    if not os.path.exists(data_base_dir):
        print(f"Data directory not found: {data_base_dir}")
        return
    
    # Initialize comprehensive analyzer
    analyzer = ComprehensiveHealthAnalyzer(data_base_dir)
    
    # Load all available health data
    analyzer.load_all_data()
    
    # Generate and display comprehensive health report
    report = analyzer.generate_comprehensive_report()
    print(report)
    
    # Save comprehensive data to CSV
    csv_output = "ORFC_comprehensive_health_analysis.csv"
    analyzer.save_comprehensive_csv(csv_output)


if __name__ == "__main__":
    main()