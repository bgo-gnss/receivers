"""Time-Series Health Data Extractor for GPS Receivers.

This module extracts complete time-series health data from SBF files,
creating daily aggregations with statistical summaries.

Author: GPS Receivers Development Team
Created: 2025-11-20
Version: 2.0
"""

import logging
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import existing RxTools utilities
from ..utils.rxtools_extractor import (
    extract_channel_status,
    extract_disk_status,
    extract_power_status,
    extract_pvt_geodetic,
    extract_quality_ind,
    extract_receiver_status,
    extract_wifi_status,
)


class TimeSeriesHealthExtractor:
    """Extract time-series health data from multiple SBF files.

    This class processes all SBF files for a given date and extracts
    complete time-series health metrics, returning structured data
    suitable for JSON export and database insertion.
    """

    def __init__(self, station_id: str, receiver_type: str = "PolaRX5"):
        """Initialize time-series extractor.

        Args:
            station_id: Station identifier (e.g., "ISFS")
            receiver_type: Receiver model (e.g., "PolaRX5", "NetR9")
        """
        self.station_id = station_id
        self.receiver_type = receiver_type
        self.logger = logging.getLogger(f"receivers.health.{station_id}")

    def extract_daily_health(
        self, sbf_files: List[Path], date: datetime
    ) -> Dict[str, Any]:
        """Extract complete daily health data from SBF files.

        Args:
            sbf_files: List of SBF file paths to process
            date: Date for this extraction

        Returns:
            Dictionary containing timeseries data and aggregations
            following the v2.0 schema
        """
        self.logger.info(
            f"Extracting daily health for {self.station_id} on {date.date()}"
        )
        self.logger.info(f"Processing {len(sbf_files)} SBF files")

        # Extract all samples from all files
        all_samples = []
        file_stats = []

        for sbf_file in sbf_files:
            try:
                samples, stats = self._extract_file_samples(sbf_file)
                all_samples.extend(samples)
                file_stats.append(stats)
                self.logger.debug(
                    f"Extracted {len(samples)} samples from {sbf_file.name}"
                )
            except Exception as e:
                self.logger.warning(f"Failed to extract from {sbf_file.name}: {e}")
                file_stats.append(
                    {"filename": sbf_file.name, "status": "error", "error": str(e)}
                )

        # Sort samples by timestamp
        all_samples.sort(key=lambda x: x["time"])

        self.logger.info(f"Total samples extracted: {len(all_samples)}")

        # Build complete data structure
        result = {
            "station_id": self.station_id,
            "receiver_type": self.receiver_type,
            "date": date.strftime("%Y-%m-%d"),
            "schema_version": "2.0",
            "sample_count": len(all_samples),
            "time_range": self._get_time_range(all_samples),
            "timeseries": all_samples,
            "aggregated": self._calculate_aggregations(all_samples, date),
            "data_files": self._build_file_tracking(file_stats, date),
            "extraction_metadata": self._build_metadata(all_samples, file_stats, date),
        }

        return result

    def _extract_file_samples(self, sbf_file: Path) -> Tuple[List[Dict], Dict]:
        """Extract all samples from a single SBF file.

        Args:
            sbf_file: Path to SBF file

        Returns:
            Tuple of (samples list, file stats dict)
        """
        samples = []

        # Extract PowerStatus (voltage)
        try:
            power_data = extract_power_status(sbf_file)
        except Exception as e:
            self.logger.debug(f"No PowerStatus data in {sbf_file.name}: {e}")
            power_data = []

        # Extract ReceiverStatus (CPU, temperature, uptime)
        try:
            receiver_data = extract_receiver_status(sbf_file)
        except Exception as e:
            self.logger.debug(f"No ReceiverStatus data in {sbf_file.name}: {e}")
            receiver_data = []

        # Extract DiskStatus (disk usage)
        try:
            disk_data = extract_disk_status(sbf_file)
        except Exception as e:
            self.logger.debug(f"No DiskStatus data in {sbf_file.name}: {e}")
            disk_data = []

        # Extract QualityInd (satellites - total only)
        try:
            quality_data = extract_quality_ind(sbf_file)
        except Exception as e:
            self.logger.debug(f"No QualityInd data in {sbf_file.name}: {e}")
            quality_data = []

        # Extract ChannelStatus (satellites by GNSS system)
        try:
            channel_data = extract_channel_status(sbf_file)
        except Exception as e:
            self.logger.debug(f"No ChannelStatus data in {sbf_file.name}: {e}")
            channel_data = []

        # Extract WiFiAPStatus (WiFi enabled/disabled)
        try:
            wifi_data = extract_wifi_status(sbf_file)
        except Exception as e:
            self.logger.debug(f"No WiFiAPStatus data in {sbf_file.name}: {e}")
            wifi_data = []

        # Extract PVTGeodetic2 (position data)
        try:
            pvt_data = extract_pvt_geodetic(sbf_file)
        except Exception as e:
            self.logger.debug(f"No PVTGeodetic2 data in {sbf_file.name}: {e}")
            pvt_data = []

        # Create index by timestamp for merging
        data_by_time = defaultdict(dict)

        # Merge power data
        for record in power_data:
            dt = record.get("datetime")
            if dt:
                voltage = record.get("Vin Voltage [V]")
                if voltage is not None:
                    data_by_time[dt]["voltage"] = {"value": voltage, "unit": "V"}

        # Merge receiver data
        for record in receiver_data:
            dt = record.get("datetime")
            if dt:
                cpu = record.get("CPULoad [%]")
                if cpu is not None:
                    data_by_time[dt]["cpu_load"] = {"value": cpu, "unit": "%"}

                temp = record.get("Temperature_degC [°C]")
                if temp is not None:
                    data_by_time[dt]["temperature"] = {"value": temp, "unit": "C"}

        # Merge disk data
        for record in disk_data:
            dt = record.get("datetime")
            if dt:
                disk = record.get("DiskUsagePercent [%]")
                if disk is not None:
                    disk_size_mb = record.get("DiskSize [MB]")
                    total = float(disk_size_mb) if disk_size_mb is not None else None
                    used = round(total * float(disk) / 100, 1) if total else None
                    data_by_time[dt]["disk"] = {
                        "usage_percent": float(disk),
                        "used_mb": used,
                        "total_mb": round(total, 1) if total else None,
                    }

        # Merge quality/satellite data (total count from QualityInd)
        for record in quality_data:
            dt = record.get("datetime")
            if dt:
                n_sats = record.get("N")
                if n_sats is not None:
                    if "satellites" not in data_by_time[dt]:
                        data_by_time[dt]["satellites"] = {}
                    data_by_time[dt]["satellites"]["total"] = int(n_sats)

        # Merge channel status data (by_system from ChannelStatus)
        for record in channel_data:
            dt = record.get("datetime")
            if dt:
                # Create satellites dict if not exists
                if "satellites" not in data_by_time[dt]:
                    data_by_time[dt]["satellites"] = {}

                # Add total if available from ChannelStatus
                total = record.get("total")
                if total is not None and total > 0:
                    data_by_time[dt]["satellites"]["total"] = int(total)

                # Add by_system breakdown (only non-zero systems)
                by_system = {}
                for system in [
                    "GPS",
                    "GLONASS",
                    "Galileo",
                    "BeiDou",
                    "QZSS",
                    "IRNSS",
                    "SBAS",
                ]:
                    count = record.get(system, 0)
                    if count > 0:
                        by_system[system] = int(count)

                if by_system:
                    data_by_time[dt]["satellites"]["by_system"] = by_system

        # Merge WiFi status data
        for record in wifi_data:
            dt = record.get("datetime")
            if dt:
                wifi_enabled = record.get("wifi_enabled")
                if wifi_enabled is not None:
                    data_by_time[dt]["wifi_enabled"] = wifi_enabled

        # Merge PVT (position) data
        for record in pvt_data:
            dt = record.get("datetime")
            if dt:
                lat = record.get("latitude")
                lon = record.get("longitude")
                height = record.get("height")

                if lat is not None and lon is not None:
                    data_by_time[dt]["position"] = {
                        "latitude": lat,
                        "longitude": lon,
                        "height": height,
                        "h_accuracy": record.get("h_accuracy"),
                        "v_accuracy": record.get("v_accuracy"),
                        "fix_type": record.get("fix_type"),
                    }

                # Also store nr_sv for satellites if not already present
                nr_sv = record.get("nr_sv")
                if nr_sv is not None:
                    if "satellites" not in data_by_time[dt]:
                        data_by_time[dt]["satellites"] = {}
                    # Use nr_sv from PVT as it represents satellites used in solution
                    data_by_time[dt]["satellites"]["nr_sv"] = nr_sv

        # Convert to samples list
        for timestamp, metrics in sorted(data_by_time.items()):
            sample = {"time": timestamp.isoformat() + "Z"}
            sample.update(metrics)
            samples.append(sample)

        # File statistics
        stats = {
            "filename": sbf_file.name,
            "status": "included",
            "samples_extracted": len(samples),
            "file_size_bytes": sbf_file.stat().st_size if sbf_file.exists() else 0,
        }

        return samples, stats

    def _get_time_range(self, samples: List[Dict]) -> Dict[str, str]:
        """Get time range from samples.

        Args:
            samples: List of sample dictionaries

        Returns:
            Dictionary with 'start' and 'end' ISO timestamps
        """
        if not samples:
            return {"start": None, "end": None}

        return {"start": samples[0]["time"], "end": samples[-1]["time"]}

    def _calculate_aggregations(
        self, samples: List[Dict], date: datetime
    ) -> Dict[str, Any]:
        """Calculate daily and hourly aggregations.

        Args:
            samples: List of sample dictionaries
            date: Date for this extraction

        Returns:
            Dictionary with 'daily' and 'hourly' statistics
        """
        if not samples:
            return {"daily": {}, "hourly": []}

        # Calculate daily statistics
        daily = self._calculate_daily_stats(samples)

        # Calculate hourly statistics
        hourly = self._calculate_hourly_stats(samples, date)

        return {"daily": daily, "hourly": hourly}

    def _calculate_daily_stats(self, samples: List[Dict]) -> Dict[str, Any]:
        """Calculate daily statistics for all metrics.

        Args:
            samples: List of sample dictionaries

        Returns:
            Dictionary of metric statistics
        """
        stats = {}

        # Metrics to aggregate (simple scalar values)
        simple_metrics = ["voltage", "cpu_load", "temperature", "disk_usage"]

        for metric in simple_metrics:
            values = []
            unit = None

            for sample in samples:
                if metric in sample and "value" in sample[metric]:
                    values.append(sample[metric]["value"])
                    if unit is None and "unit" in sample[metric]:
                        unit = sample[metric]["unit"]

            if values:
                stats[metric] = {
                    "mean": round(statistics.mean(values), 2),
                    "std": round(statistics.stdev(values), 2)
                    if len(values) > 1
                    else 0.0,
                    "min": round(min(values), 2),
                    "max": round(max(values), 2),
                    "unit": unit,
                    "samples": len(values),
                }

        # Satellites (total count)
        sat_totals = []
        for sample in samples:
            if "satellites" in sample and "total" in sample["satellites"]:
                sat_totals.append(sample["satellites"]["total"])

        if sat_totals:
            stats["satellites"] = {
                "total": {
                    "mean": round(statistics.mean(sat_totals), 1),
                    "std": round(statistics.stdev(sat_totals), 1)
                    if len(sat_totals) > 1
                    else 0.0,
                    "min": min(sat_totals),
                    "max": max(sat_totals),
                    "samples": len(sat_totals),
                }
            }

            # Add by_system statistics if available
            by_system_stats = {}
            for system in [
                "GPS",
                "GLONASS",
                "Galileo",
                "BeiDou",
                "QZSS",
                "IRNSS",
                "SBAS",
            ]:
                system_counts = []
                for sample in samples:
                    if "satellites" in sample and "by_system" in sample["satellites"]:
                        count = sample["satellites"]["by_system"].get(system, 0)
                        system_counts.append(count)

                if system_counts and any(c > 0 for c in system_counts):
                    by_system_stats[system] = {
                        "mean": round(statistics.mean(system_counts), 1),
                        "std": round(statistics.stdev(system_counts), 1)
                        if len(system_counts) > 1
                        else 0.0,
                        "min": min(system_counts),
                        "max": max(system_counts),
                        "samples": len(system_counts),
                    }

            if by_system_stats:
                stats["satellites"]["by_system"] = by_system_stats

        # WiFi status (percentage of time enabled)
        wifi_samples = []
        for sample in samples:
            if "wifi_enabled" in sample:
                wifi_samples.append(sample["wifi_enabled"])

        if wifi_samples:
            enabled_count = sum(wifi_samples)
            total_count = len(wifi_samples)
            stats["wifi"] = {
                "enabled_percent": round((enabled_count / total_count) * 100, 1),
                "enabled_samples": enabled_count,
                "total_samples": total_count,
            }

        return stats

    def _calculate_hourly_stats(
        self, samples: List[Dict], date: datetime
    ) -> List[Dict[str, Any]]:
        """Calculate hourly statistics for all metrics.

        Args:
            samples: List of sample dictionaries
            date: Date for this extraction

        Returns:
            List of 24 hourly statistic dictionaries
        """
        hourly_data = []

        # Group samples by hour
        samples_by_hour = defaultdict(list)
        for sample in samples:
            try:
                timestamp = datetime.fromisoformat(
                    sample["time"].replace("Z", "+00:00")
                )
                hour = timestamp.hour
                samples_by_hour[hour].append(sample)
            except Exception as e:
                self.logger.warning(
                    f"Invalid timestamp in sample: {sample.get('time')}: {e}"
                )

        # Calculate stats for each hour
        for hour in range(24):
            hour_samples = samples_by_hour.get(hour, [])

            if not hour_samples:
                # No data for this hour - could add entry with empty stats or skip
                continue

            hour_stats = {"hour": hour}

            # Simple metrics
            simple_metrics = ["voltage", "cpu_load", "temperature", "disk_usage"]
            for metric in simple_metrics:
                values = []
                unit = None

                for sample in hour_samples:
                    if metric in sample and "value" in sample[metric]:
                        values.append(sample[metric]["value"])
                        if unit is None and "unit" in sample[metric]:
                            unit = sample[metric]["unit"]

                if values:
                    hour_stats[metric] = {
                        "mean": round(statistics.mean(values), 2),
                        "std": round(statistics.stdev(values), 2)
                        if len(values) > 1
                        else 0.0,
                        "min": round(min(values), 2),
                        "max": round(max(values), 2),
                        "unit": unit,
                        "samples": len(values),
                    }

            # Satellites
            sat_totals = []
            for sample in hour_samples:
                if "satellites" in sample and "total" in sample["satellites"]:
                    sat_totals.append(sample["satellites"]["total"])

            if sat_totals:
                hour_stats["satellites"] = {
                    "total": {
                        "mean": round(statistics.mean(sat_totals), 1),
                        "std": round(statistics.stdev(sat_totals), 1)
                        if len(sat_totals) > 1
                        else 0.0,
                        "min": min(sat_totals),
                        "max": max(sat_totals),
                        "samples": len(sat_totals),
                    }
                }

                # Add by_system statistics if available
                by_system_stats = {}
                for system in [
                    "GPS",
                    "GLONASS",
                    "Galileo",
                    "BeiDou",
                    "QZSS",
                    "IRNSS",
                    "SBAS",
                ]:
                    system_counts = []
                    for sample in hour_samples:
                        if (
                            "satellites" in sample
                            and "by_system" in sample["satellites"]
                        ):
                            count = sample["satellites"]["by_system"].get(system, 0)
                            system_counts.append(count)

                    if system_counts and any(c > 0 for c in system_counts):
                        by_system_stats[system] = {
                            "mean": round(statistics.mean(system_counts), 1),
                            "std": round(statistics.stdev(system_counts), 1)
                            if len(system_counts) > 1
                            else 0.0,
                            "min": min(system_counts),
                            "max": max(system_counts),
                            "samples": len(system_counts),
                        }

                if by_system_stats:
                    hour_stats["satellites"]["by_system"] = by_system_stats

            # WiFi status (percentage of time enabled)
            wifi_samples = []
            for sample in hour_samples:
                if "wifi_enabled" in sample:
                    wifi_samples.append(sample["wifi_enabled"])

            if wifi_samples:
                enabled_count = sum(wifi_samples)
                total_count = len(wifi_samples)
                hour_stats["wifi"] = {
                    "enabled_percent": round((enabled_count / total_count) * 100, 1),
                    "enabled_samples": enabled_count,
                    "total_samples": total_count,
                }

            hourly_data.append(hour_stats)

        return hourly_data

    def _build_file_tracking(
        self, file_stats: List[Dict], date: datetime
    ) -> Dict[str, Any]:
        """Build file tracking information.

        Args:
            file_stats: List of file statistic dictionaries
            date: Date for this extraction

        Returns:
            Dictionary with file tracking data
        """
        # For status_1hr session, expect 24 hourly files
        return {
            "status_1hr": {"expected_files": 24, "files": file_stats}
            # TODO: Add 15s_24hr and 1Hz_1hr tracking if needed
        }

    def _build_metadata(
        self, samples: List[Dict], file_stats: List[Dict], date: datetime
    ) -> Dict[str, Any]:
        """Build extraction metadata.

        Args:
            samples: List of sample dictionaries
            file_stats: List of file statistic dictionaries
            date: Date for this extraction

        Returns:
            Dictionary with extraction metadata
        """
        # Find missing hours
        hours_with_data = set()
        for sample in samples:
            try:
                timestamp = datetime.fromisoformat(
                    sample["time"].replace("Z", "+00:00")
                )
                hours_with_data.add(timestamp.hour)
            except Exception:
                pass

        missing_hours = sorted([h for h in range(24) if h not in hours_with_data])

        # Calculate completeness (expect 60 samples per hour * 24 hours = 1440)
        expected_samples = 24 * 60
        completeness = (
            (len(samples) / expected_samples * 100) if expected_samples > 0 else 0.0
        )

        # Detect gaps (consecutive missing samples > 5 minutes)
        gaps_detected = 0
        if len(samples) > 1:
            for i in range(1, len(samples)):
                try:
                    t1 = datetime.fromisoformat(
                        samples[i - 1]["time"].replace("Z", "+00:00")
                    )
                    t2 = datetime.fromisoformat(
                        samples[i]["time"].replace("Z", "+00:00")
                    )
                    gap = (t2 - t1).total_seconds() / 60  # minutes
                    if gap > 5:  # More than 5 minutes gap
                        gaps_detected += 1
                except Exception:
                    pass

        # Determine receiver capabilities based on what data we found
        has_voltage = any("voltage" in s for s in samples)
        has_cpu = any("cpu_load" in s for s in samples)
        has_temp = any("temperature" in s for s in samples)
        has_disk = any("disk_usage" in s for s in samples)
        has_sats = any("satellites" in s for s in samples)
        has_sat_by_system = any(
            "satellites" in s and "by_system" in s.get("satellites", {})
            for s in samples
        )
        has_wifi = any("wifi_enabled" in s for s in samples)

        return {
            "extracted_at": datetime.now(UTC).isoformat() + "Z",
            "extractor_version": "2.0",
            "missing_hours": missing_hours,
            "data_quality": {
                "completeness": round(completeness, 1),
                "gaps_detected": gaps_detected,
                "corrupted_samples": 0,  # TODO: Track corrupted samples
            },
            "receiver_capabilities": {
                "metrics": {
                    "voltage": has_voltage,
                    "cpu_load": has_cpu,
                    "temperature": has_temp,
                    "disk_usage": has_disk,
                },
                "satellites": {"total": has_sats, "by_system": has_sat_by_system},
                "wifi": has_wifi,
            },
        }
