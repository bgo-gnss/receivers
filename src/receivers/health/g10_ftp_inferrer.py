"""FTP-based health data inferrer for Leica G10 receivers.

The Leica G10 has no direct health API, so health status is inferred from:
- FTP connection stability
- Data file availability and timestamps
- File upload frequency and gaps

This provides basic health monitoring for receivers with limited capabilities.
"""

import logging
from datetime import datetime, timedelta
from ftplib import FTP, error_perm
from typing import Dict, Any, Optional, List
from pathlib import Path


class G10FTPHealthInferrer:
    """Infer health data from FTP file availability for Leica G10."""

    def __init__(self, host: str, station_id: str = "UNKNOWN", timeout: int = 10):
        """Initialize FTP health inferrer.

        Args:
            host: FTP server hostname or IP address
            station_id: Station identifier for logging
            timeout: FTP connection timeout in seconds
        """
        self.host = host
        self.station_id = station_id
        self.timeout = timeout
        self.logger = logging.getLogger(f"receivers.health.g10.{station_id}")

    def infer_health_from_ftp(
        self, ftp_path: str = "/data", username: str = "anonymous", password: str = ""
    ) -> Dict[str, Any]:
        """Infer health status from FTP file availability.

        Args:
            ftp_path: FTP directory path to check
            username: FTP username
            password: FTP password

        Returns:
            Dictionary with inferred health data
        """
        health_data = {
            "extraction_time": datetime.utcnow().isoformat() + "Z",
            "data_quality": {},
            "receiver_specific": {
                "health_inference_method": "ftp_file_analysis",
                "note": "G10 has no direct health API - status inferred from data flow",
            },
        }

        try:
            # Connect to FTP server
            ftp = FTP(timeout=self.timeout)
            ftp.connect(self.host)
            ftp.login(username, password)

            try:
                # Change to data directory
                ftp.cwd(ftp_path)

                # Get file list with modification times
                files_info = self._get_files_with_times(ftp)

                if files_info:
                    # Analyze file timestamps for data flow health
                    data_flow_status = self._analyze_data_flow(files_info)
                    health_data["data_quality"]["data_flow"] = data_flow_status

                    # Check file count as indicator of logging activity
                    health_data["data_quality"]["file_count"] = {
                        "total_files": len(files_info),
                        "status": "ok" if len(files_info) > 0 else "warning",
                    }

                    self.logger.info(
                        f"Analyzed {len(files_info)} files on FTP server {self.host}"
                    )
                else:
                    health_data["data_quality"]["data_flow"] = {
                        "status": "warning",
                        "message": "No files found in data directory",
                    }

            except error_perm as e:
                self.logger.error(f"FTP permission error: {e}")
                health_data["data_quality"]["ftp_access"] = {
                    "status": "critical",
                    "error": f"Cannot access {ftp_path}: {str(e)}",
                }
            finally:
                ftp.quit()

        except Exception as e:
            self.logger.error(f"FTP connection error: {e}")
            health_data["data_quality"]["ftp_connection"] = {
                "status": "critical",
                "error": str(e),
            }

        return health_data

    def _get_files_with_times(self, ftp: FTP) -> List[Dict[str, Any]]:
        """Get file list with modification times from FTP server.

        Args:
            ftp: Connected FTP object

        Returns:
            List of dictionaries with file info (name, size, mtime)
        """
        files_info = []

        try:
            # Use MLSD if available (more reliable than LIST)
            for name, facts in ftp.mlsd():
                if facts.get("type") == "file":
                    # Parse modify time (YYYYMMDDHHMMSS format)
                    modify_str = facts.get("modify")
                    mtime = None

                    if modify_str:
                        try:
                            mtime = datetime.strptime(modify_str, "%Y%m%d%H%M%S")
                        except ValueError:
                            pass

                    files_info.append(
                        {
                            "name": name,
                            "size": int(facts.get("size", 0)),
                            "mtime": mtime,
                        }
                    )

        except Exception as e:
            # Fallback to basic LIST if MLSD not supported
            self.logger.debug(f"MLSD not supported, using LIST: {e}")

            lines = []
            ftp.retrlines("LIST", lines.append)

            for line in lines:
                # Basic parsing (may not work for all FTP servers)
                parts = line.split()
                if len(parts) >= 9:
                    name = " ".join(parts[8:])
                    size = int(parts[4]) if parts[4].isdigit() else 0

                    files_info.append({"name": name, "size": size, "mtime": None})

        return files_info

    def _analyze_data_flow(self, files_info: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze file timestamps to infer data flow health.

        Args:
            files_info: List of file information dictionaries

        Returns:
            Dictionary with data flow health status
        """
        now = datetime.utcnow()

        # Get files with valid timestamps
        files_with_time = [f for f in files_info if f.get("mtime")]

        if not files_with_time:
            return {
                "status": "unknown",
                "message": "No file timestamps available for analysis",
            }

        # Sort by modification time
        files_with_time.sort(key=lambda f: f["mtime"], reverse=True)

        # Check most recent file age
        most_recent = files_with_time[0]
        age_hours = (now - most_recent["mtime"]).total_seconds() / 3600

        # Determine status based on file age
        if age_hours < 2:
            status = "ok"
            message = f"Recent data file from {age_hours:.1f} hours ago"
        elif age_hours < 24:
            status = "warning"
            message = f"No recent data - last file {age_hours:.1f} hours ago"
        else:
            status = "critical"
            age_days = age_hours / 24
            message = f"No recent data - last file {age_days:.1f} days ago"

        # Check for data gaps (if we have enough files)
        gaps_detected = False
        if len(files_with_time) >= 2:
            # Check time difference between consecutive files
            for i in range(min(5, len(files_with_time) - 1)):
                gap_hours = (
                    files_with_time[i]["mtime"] - files_with_time[i + 1]["mtime"]
                ).total_seconds() / 3600

                # Flag if gap > 3 hours (assuming hourly data)
                if gap_hours > 3:
                    gaps_detected = True
                    break

        return {
            "status": status,
            "message": message,
            "most_recent_file": most_recent["name"],
            "most_recent_age_hours": round(age_hours, 1),
            "file_count_checked": len(files_with_time),
            "gaps_detected": gaps_detected,
        }
