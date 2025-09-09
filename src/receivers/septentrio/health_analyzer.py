#!/usr/bin/env python3
"""
Health Data Analyzer for Septentrio PolaRX5 Status Sessions

This module analyzes health data extracted from SBF status files converted to ASCII format.
Focuses on ReceiverStatus blocks that contain CPU load, uptime, and receiver status information.
"""

import os
import glob
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import pandas as pd


class HealthDataAnalyzer:
    """Analyzes health data from Septentrio PolaRX5 status session ASCII files."""
    
    def __init__(self, ascii_dir: str):
        """
        Initialize health analyzer.
        
        Args:
            ascii_dir: Directory containing ASCII status files
        """
        self.ascii_dir = ascii_dir
        self.health_data = []
        
    def parse_receiver_status_line(self, line: str) -> Optional[Dict]:
        """
        Parse a ReceiverStatus line from ASCII output.
        
        Args:
            line: ASCII line containing ReceiverStatus data
            
        Returns:
            Dictionary with parsed health data or None if not a ReceiverStatus line
        """
        parts = line.strip().split()
        
        # Check if this is a ReceiverStatus block (starts with -7)
        if len(parts) >= 5 and parts[0] == '-7':
            try:
                return {
                    'block_type': int(parts[0]),  # -7 for ReceiverStatus
                    'gps_time': float(parts[1]),  # GPS seconds since Jan 06, 1980
                    'cpu_load': int(parts[2]),    # CPU load percentage
                    'uptime': int(parts[3]),      # Uptime in seconds
                    'rx_status': parts[4],        # Receiver status in hex
                }
            except (ValueError, IndexError):
                return None
        return None
    
    def load_ascii_file(self, filepath: str) -> List[Dict]:
        """
        Load and parse a single ASCII status file.
        
        Args:
            filepath: Path to ASCII file
            
        Returns:
            List of parsed health records
        """
        records = []
        
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    health_record = self.parse_receiver_status_line(line)
                    if health_record:
                        # Add file metadata
                        health_record['source_file'] = os.path.basename(filepath)
                        health_record['datetime'] = self.gps_to_datetime(health_record['gps_time'])
                        records.append(health_record)
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            
        return records
    
    def gps_to_datetime(self, gps_seconds: float) -> datetime:
        """
        Convert GPS seconds to datetime.
        
        Args:
            gps_seconds: GPS seconds since Jan 06, 1980
            
        Returns:
            datetime object
        """
        # GPS epoch is January 6, 1980, 00:00:00 UTC
        gps_epoch = datetime(1980, 1, 6)
        return gps_epoch + pd.Timedelta(seconds=gps_seconds)
    
    def load_all_files(self) -> None:
        """Load and parse all ASCII files in the directory."""
        ascii_files = glob.glob(os.path.join(self.ascii_dir, "*.asc"))
        ascii_files.sort()  # Sort for chronological order
        
        print(f"Found {len(ascii_files)} ASCII files to process")
        
        for filepath in ascii_files:
            print(f"Processing: {os.path.basename(filepath)}")
            records = self.load_ascii_file(filepath)
            self.health_data.extend(records)
            print(f"  Loaded {len(records)} health records")
        
        print(f"Total health records loaded: {len(self.health_data)}")
    
    def get_dataframe(self) -> pd.DataFrame:
        """
        Get health data as a pandas DataFrame.
        
        Returns:
            DataFrame with health data
        """
        if not self.health_data:
            return pd.DataFrame()
        
        df = pd.DataFrame(self.health_data)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime')
        return df
    
    def analyze_cpu_load(self) -> Dict:
        """
        Analyze CPU load statistics.
        
        Returns:
            Dictionary with CPU load analysis
        """
        if not self.health_data:
            return {}
        
        cpu_loads = [record['cpu_load'] for record in self.health_data]
        
        return {
            'min_cpu': min(cpu_loads),
            'max_cpu': max(cpu_loads),
            'mean_cpu': sum(cpu_loads) / len(cpu_loads),
            'total_samples': len(cpu_loads)
        }
    
    def analyze_uptime(self) -> Dict:
        """
        Analyze receiver uptime.
        
        Returns:
            Dictionary with uptime analysis
        """
        if not self.health_data:
            return {}
        
        uptimes = [record['uptime'] for record in self.health_data]
        
        return {
            'min_uptime_sec': min(uptimes),
            'max_uptime_sec': max(uptimes),
            'min_uptime_days': min(uptimes) / 86400,
            'max_uptime_days': max(uptimes) / 86400,
            'total_samples': len(uptimes)
        }
    
    def analyze_rx_status(self) -> Dict:
        """
        Analyze receiver status codes.
        
        Returns:
            Dictionary with receiver status analysis
        """
        if not self.health_data:
            return {}
        
        status_codes = [record['rx_status'] for record in self.health_data]
        status_counts = {}
        
        for status in status_codes:
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return {
            'unique_status_codes': list(status_counts.keys()),
            'status_distribution': status_counts,
            'total_samples': len(status_codes)
        }
    
    def generate_health_report(self) -> str:
        """
        Generate a comprehensive health report.
        
        Returns:
            Formatted health report string
        """
        if not self.health_data:
            return "No health data available"
        
        cpu_analysis = self.analyze_cpu_load()
        uptime_analysis = self.analyze_uptime()
        status_analysis = self.analyze_rx_status()
        
        df = self.get_dataframe()
        time_span = df['datetime'].max() - df['datetime'].min()
        
        report = f"""
PolaRX5 Health Status Report
============================

Data Summary:
- Total records: {len(self.health_data)}
- Time span: {time_span}
- First record: {df['datetime'].min()}
- Last record: {df['datetime'].max()}

CPU Load Analysis:
- Minimum: {cpu_analysis['min_cpu']}%
- Maximum: {cpu_analysis['max_cpu']}%
- Average: {cpu_analysis['mean_cpu']:.1f}%

Uptime Analysis:
- Minimum uptime: {uptime_analysis['min_uptime_days']:.1f} days
- Maximum uptime: {uptime_analysis['max_uptime_days']:.1f} days
- Uptime range: {uptime_analysis['min_uptime_sec']} - {uptime_analysis['max_uptime_sec']} seconds

Receiver Status Analysis:
- Unique status codes: {len(status_analysis['unique_status_codes'])}
- Status distribution:
"""
        
        for status, count in status_analysis['status_distribution'].items():
            percentage = (count / len(self.health_data)) * 100
            report += f"  {status}: {count} occurrences ({percentage:.1f}%)\n"
        
        return report
    
    def save_health_data_csv(self, output_path: str) -> None:
        """
        Save health data to CSV file.
        
        Args:
            output_path: Path for output CSV file
        """
        df = self.get_dataframe()
        if not df.empty:
            df.to_csv(output_path, index=False)
            print(f"Health data saved to: {output_path}")
        else:
            print("No health data to save")


def main():
    """Main function for command-line usage."""
    ascii_dir = "data/2025/sep/ORFC/status_1hr/ascii"
    
    if not os.path.exists(ascii_dir):
        print(f"ASCII directory not found: {ascii_dir}")
        return
    
    # Initialize analyzer
    analyzer = HealthDataAnalyzer(ascii_dir)
    
    # Load all health data
    analyzer.load_all_files()
    
    # Generate and display health report
    report = analyzer.generate_health_report()
    print(report)
    
    # Save health data to CSV
    csv_output = "data/2025/sep/ORFC/status_1hr/health_analysis.csv"
    analyzer.save_health_data_csv(csv_output)


if __name__ == "__main__":
    main()