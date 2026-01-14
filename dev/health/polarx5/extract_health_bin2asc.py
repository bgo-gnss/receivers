#!/usr/bin/env python3
"""
Extract health message values from Septentrio SBF files using bin2asc.

This script uses RxTools bin2asc to extract health data values and converts
them to both CSV and JSON Lines formats, consolidating all data from multiple
SBF files into single files per message type.
"""

import os
import sys
import gzip
import json
import subprocess
import tempfile
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd

# Add gtimes module to path for GPS time conversions
sys.path.insert(0, '../gtimes/src')
from gtimes.gpstime import UTCFromGps


class Bin2AscHealthExtractor:
    """Extract health data using RxTools bin2asc tool."""
    
    # Health message types to extract
    HEALTH_MESSAGES = [
        'PowerStatus',
        'DiskStatus', 
        'ReceiverStatus1',
        'ReceiverStatus2',
        'WiFiAPStatus',
        'LogStatus',
        'NTRIPServerStatus',
        'NTRIPClientStatus'
    ]
    
    def __init__(self, output_dir: str):
        """Initialize extractor with output directory."""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.bin2asc_path = '/opt/rxtools/bin/bin2asc'
        
        # Check if bin2asc is available
        if not os.path.exists(self.bin2asc_path):
            raise FileNotFoundError(f"bin2asc not found at {self.bin2asc_path}")
    
    def extract_from_sbf_file(self, sbf_file_path: str) -> dict:
        """
        Extract health messages from a single SBF file.
        
        Args:
            sbf_file_path: Path to SBF file (can be .gz compressed)
            
        Returns:
            Dictionary with message counts extracted
        """
        stats = {}
        
        # Handle compressed files
        if sbf_file_path.endswith('.gz'):
            print(f"  Processing compressed file: {os.path.basename(sbf_file_path)}")
            with gzip.open(sbf_file_path, 'rb') as gz_file:
                data = gz_file.read()
            
            # Create temporary uncompressed file
            with tempfile.NamedTemporaryFile(suffix='.sbf', delete=False) as temp_file:
                temp_file.write(data)
                temp_sbf_path = temp_file.name
        else:
            print(f"  Processing SBF file: {os.path.basename(sbf_file_path)}")
            temp_sbf_path = sbf_file_path
        
        try:
            # Create temporary directory for bin2asc output
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir_path = Path(temp_dir)
                
                # Run bin2asc for each health message type
                for message_type in self.HEALTH_MESSAGES:
                    try:
                        # Run bin2asc to extract specific message type
                        # Convert to absolute path for bin2asc
                        abs_sbf_path = os.path.abspath(temp_sbf_path)
                        cmd = [
                            self.bin2asc_path,
                            '-f', abs_sbf_path,
                            '-m', message_type,
                            '-p', str(temp_dir_path)
                        ]
                        
                        result = subprocess.run(
                            cmd, 
                            capture_output=True, 
                            text=True, 
                            cwd=temp_dir_path
                        )
                        
                        if result.returncode == 0:
                            # Find the output file
                            sbf_filename = os.path.basename(temp_sbf_path)
                            output_file = temp_dir_path / f"{sbf_filename}_SBF_{message_type}.txt"
                            
                            if output_file.exists():
                                # Process the extracted data
                                count = self.process_message_file(output_file, message_type)
                                if count > 0:
                                    stats[message_type] = count
                                    print(f"    {message_type}: {count} messages")
                        
                    except Exception as e:
                        print(f"    Warning: Failed to extract {message_type}: {e}")
                        continue
        
        finally:
            # Clean up temporary file if we created one
            if sbf_file_path.endswith('.gz') and os.path.exists(temp_sbf_path):
                os.unlink(temp_sbf_path)
        
        return stats
    
    def process_message_file(self, message_file: Path, message_type: str) -> int:
        """
        Process bin2asc output file and append to consolidated CSV/JSON files.
        
        Args:
            message_file: Path to bin2asc output file
            message_type: Type of health message
            
        Returns:
            Number of messages processed
        """
        # Read the bin2asc output
        with open(message_file, 'r') as f:
            lines = f.readlines()
        
        if not lines:
            return 0
        
        # Prepare output files
        csv_file = self.output_dir / f"{message_type}.csv"
        json_file = self.output_dir / f"{message_type}.jsonl"
        
        csv_rows = []
        json_rows = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse bin2asc output format: time,week,field1,value1,field2,value2,...
            parts = line.split(',')
            if len(parts) < 3:
                continue
                
            try:
                time_seconds = float(parts[0])
                gps_week = int(parts[1])
                
                # Convert GPS time to proper UTC timestamp
                utc_datetime = UTCFromGps(gps_week, time_seconds, dtimeObj=True)
                utc_timestamp = utc_datetime.isoformat() + 'Z'
                
                # Create base record with proper timestamp
                base_record = {
                    'gps_week': gps_week,
                    'gps_sow': round(time_seconds, 3),
                    'timestamp': utc_timestamp
                }
                
                # Parse message-specific data
                if message_type == 'PowerStatus':
                    # Format: time,week,Vin,voltage_value,Vin,voltage_value
                    voltage = float(parts[3]) if len(parts) > 3 else 0.0
                    
                    # CSV: timestamp,voltage format for easy analysis
                    csv_rows.append(f"{utc_timestamp},{voltage:.2f}")
                    
                    # JSON: structured format
                    json_record = base_record.copy()
                    json_record.update({
                        'voltage': round(voltage, 2),
                        'message_type': 'PowerStatus'
                    })
                    json_rows.append(json_record)
                
                elif message_type == 'DiskStatus':
                    # DiskStatus has many fields, extract key ones
                    if len(parts) >= 12:
                        total_space = int(parts[10]) if parts[10].isdigit() else 0
                        total_gb = round(total_space / (1024**3), 2)
                        usage_pct = float(parts[12]) if len(parts) > 12 and parts[12].replace('.','').isdigit() else 0
                        
                        # CSV: timestamp,total_gb,usage_pct
                        csv_rows.append(f"{utc_timestamp},{total_gb:.1f},{usage_pct:.1f}")
                        
                        # JSON: detailed format  
                        json_record = base_record.copy()
                        json_record.update({
                            'total_space_bytes': total_space,
                            'total_gb': total_gb,
                            'usage_percent': round(usage_pct, 1),
                            'message_type': 'DiskStatus'
                        })
                        json_rows.append(json_record)
                
                else:
                    # Generic handling for other message types
                    csv_rows.append(f"{utc_timestamp},{message_type}")
                    
                    json_record = base_record.copy()
                    json_record.update({
                        'message_type': message_type,
                        'raw_data': parts[2:] if len(parts) > 2 else []
                    })
                    json_rows.append(json_record)
                    
            except (ValueError, IndexError) as e:
                print(f"    Warning: Failed to parse line: {line[:50]}... ({e})")
                continue
        
        # Append to output files
        if csv_rows:
            with open(csv_file, 'a') as f:
                f.write('\n'.join(csv_rows) + '\n')
        
        if json_rows:
            with open(json_file, 'a') as f:
                for record in json_rows:
                    f.write(json.dumps(record) + '\n')
        
        return len(csv_rows)
    
    def extract_from_directory(self, raw_dir: str, pattern: str = "*.sbf*") -> dict:
        """
        Extract health data from all SBF files in a directory.
        
        Args:
            raw_dir: Directory containing SBF files
            pattern: File pattern to match
            
        Returns:
            Dictionary with total extraction statistics
        """
        raw_path = Path(raw_dir)
        if not raw_path.exists():
            raise FileNotFoundError(f"Raw data directory not found: {raw_path}")
        
        # Find all SBF files
        sbf_files = sorted(raw_path.glob(pattern))
        if not sbf_files:
            print(f"No SBF files found in {raw_path} matching {pattern}")
            return {}
        
        print(f"Processing {len(sbf_files)} SBF files from {raw_path}")
        print(f"Output directory: {self.output_dir}")
        print()
        
        # Process each file and accumulate statistics
        total_stats = {}
        for sbf_file in sbf_files:
            print(f"Processing: {sbf_file.name}")
            file_stats = self.extract_from_sbf_file(str(sbf_file))
            
            # Accumulate totals
            for msg_type, count in file_stats.items():
                total_stats[msg_type] = total_stats.get(msg_type, 0) + count
            
            print()
        
        return total_stats
    
    def create_summary(self, stats: dict, files_processed: int):
        """Create summary metadata file."""
        summary_file = self.output_dir / 'summary.json'
        
        summary_data = {
            'extraction_method': 'bin2asc',
            'processing_time': datetime.now().isoformat(),
            'files_processed': files_processed,
            'message_counts': stats,
            'formats': ['csv', 'jsonl'],
            'tools_used': ['RxTools bin2asc v25.0.0']
        }
        
        with open(summary_file, 'w') as f:
            json.dump(summary_data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description='Extract health values using bin2asc')
    parser.add_argument('--station', '-s', required=True, help='Station code (e.g., ORFC)')
    parser.add_argument('--session', default='status_1hr', help='Session type (default: status_1hr)')
    parser.add_argument('--pattern', default='*.sbf*', help='File pattern to process (default: *.sbf*)')
    parser.add_argument('--output', '-o', help='Output directory (default: health_data/)')
    
    args = parser.parse_args()
    
    # Set up paths
    station_code = args.station.upper()
    session = args.session
    
    # Find the data directory
    base_path = Path('data/2025/sep') / station_code / session / 'raw'
    if not base_path.exists():
        print(f"Error: Data directory not found: {base_path}")
        sys.exit(1)
    
    # Set up output directory
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = base_path.parent / 'health_data'
    
    print(f"Extracting health values using bin2asc")
    print(f"Station: {station_code} | Session: {session}")
    print(f"Input path: {base_path}")
    print(f"Output path: {output_dir}")
    print()
    
    # Initialize extractor and process files
    try:
        extractor = Bin2AscHealthExtractor(str(output_dir))
        stats = extractor.extract_from_directory(str(base_path), args.pattern)
        
        # Count files processed
        sbf_files = list(base_path.glob(args.pattern))
        files_processed = len(sbf_files)
        
        # Create summary
        extractor.create_summary(stats, files_processed)
        
        print("=== Extraction Complete ===")
        print(f"Files processed: {files_processed}")
        print("Message counts:")
        for msg_type, count in sorted(stats.items()):
            print(f"  {msg_type}: {count} messages")
        
        # List output files
        print(f"\nOutput files created in {output_dir}:")
        for file_ext in ['*.csv', '*.jsonl']:
            files = list(output_dir.glob(file_ext))
            if files:
                format_name = 'CSV' if file_ext == '*.csv' else 'JSON Lines'
                print(f"  {format_name}:")
                for file_path in sorted(files):
                    size_kb = file_path.stat().st_size / 1024
                    print(f"    {file_path.name} ({size_kb:.1f} KB)")
        
        summary_file = output_dir / 'summary.json'
        if summary_file.exists():
            print(f"  Metadata: summary.json")
        
        print(f"\n=== Usage Examples ===")
        if 'PowerStatus' in stats:
            print(f"Grep voltage values:    grep '12\\.' {output_dir}/PowerStatus.csv")
            print(f"JSON query voltage:     jq '.voltage' {output_dir}/PowerStatus.jsonl")
        print(f"Count total messages:   wc -l {output_dir}/*.csv")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()