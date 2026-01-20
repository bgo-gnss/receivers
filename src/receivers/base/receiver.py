"""Abstract base class for GPS/GNSS receivers."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Union, Optional

from .config_manager import get_config_manager
from ..config.receivers_config import get_receivers_config
from ..health.connection_checker import ConnectionChecker, HealthStatus


class BaseReceiver(ABC):
    """Abstract base class for GPS/GNSS receivers.

    This class defines the common interface that all receiver implementations
    must follow to ensure consistency across different receiver types.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize receiver with station information.

        Args:
            station_id: Station identifier (e.g., 'REYK', 'HOFN')
            station_info: Station configuration dictionary
        """
        self.station_id = station_id.upper()
        self.station_info = station_info
        self.connection_status = {"router": None, "receiver": None}

        # Initialize shared configuration managers
        self.config_manager = get_config_manager()
        self.receivers_config = get_receivers_config()

        # Get common configuration values that all receivers need
        self.data_prepath = self.receivers_config.get_data_prepath()

    @abstractmethod
    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.

        Returns:
            Dictionary with connection status information
        """
        pass

    @abstractmethod
    def download_data(
        self,
        start: Union[datetime, str],
        end: Union[datetime, str],
        session: str = "15s_24hr",
        sync: bool = True,
        clean_tmp: bool = True,
        archive: bool = True,
        reverse_chronological: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download data from receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            reverse_chronological: Download newest files first (True for -D flag routine downloads,
                                  False for --start/--end backfilling). Default True.
            **kwargs: Additional receiver-specific parameters

        Returns:
            Dictionary with download results and file information
        """
        pass

    @abstractmethod
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of receiver.

        Subclasses should implement this to provide receiver-specific health metrics.
        Use build_health_status() helper to create standardized format.

        Returns:
            Dictionary with health metrics and status information following
            health-data-spec.md format
        """
        pass

    def check_connection_health(
        self,
        http_port: int = 80,
        protocol_type: str = "ftp",
        protocol_port: Optional[int] = None,
        host: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Check connection health at all levels using ConnectionChecker.

        This is a helper method that subclasses can use in their get_health_status()
        implementation to get standardized connection health data.

        Args:
            http_port: HTTP port to test (default: 80)
            protocol_type: Protocol type (ftp, http, tcp)
            protocol_port: Protocol-specific port (if different from http_port)
            host: Receiver IP/hostname (if not provided, attempts to get from station_info)

        Returns:
            Dictionary with connection health data in standardized format
        """
        # Get receiver IP/hostname - use provided host or try common station_info locations
        if host is None:
            host = (
                self.station_info.get("ip")
                or self.station_info.get("host")
                or self.station_info.get("router", {}).get("ip")
                or "unknown"
            )

        # Ensure host is always a string for ConnectionChecker
        resolved_host: str = host if host else "unknown"

        # Create connection checker
        checker = ConnectionChecker(host=resolved_host, station_id=self.station_id)

        # Run all connection checks
        results = checker.check_all_levels(
            http_port=http_port,
            protocol_type=protocol_type,
            protocol_port=protocol_port,
        )

        # Convert to standardized format
        connection_data = {}
        for level, status in results.items():
            connection_data[level] = status.to_dict()

        return connection_data

    def build_health_status(
        self,
        connection_data: Dict[str, Any],
        metrics: Optional[Dict[str, Any]] = None,
        data_quality: Optional[Dict[str, Any]] = None,
        network: Optional[Dict[str, Any]] = None,
        receiver_specific: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build standardized health status structure.

        Helper method for subclasses to create health status in format
        specified by health-data-spec.md.

        Args:
            connection_data: Connection health data from check_connection_health()
            metrics: Health metrics (power, temperature, cpu, etc.)
            data_quality: Data logging and quality metrics
            network: Network service status (NTRIP, WiFi, etc.)
            receiver_specific: Receiver-specific additional data

        Returns:
            Standardized health status dictionary
        """
        # Calculate overall status from connection and metrics
        statuses = []

        # Check connection statuses
        for level_data in connection_data.values():
            status_str = level_data.get("status", "unknown")
            statuses.append(HealthStatus(status_str))

        # Check metrics statuses if provided
        if metrics:
            for metric_name, metric_data in metrics.items():
                if isinstance(metric_data, dict):
                    # Handle nested 'ports' structure specially
                    if metric_name == "ports":
                        for port_name, port_data in metric_data.items():
                            if isinstance(port_data, dict) and "status" in port_data:
                                statuses.append(HealthStatus(port_data["status"]))
                    elif "status" in metric_data:
                        statuses.append(HealthStatus(metric_data["status"]))

        # Determine overall status (worst status wins)
        if HealthStatus.CRITICAL in statuses:
            overall_status = "critical"
        elif HealthStatus.ERROR in statuses:
            overall_status = "critical"  # Treat errors as critical
        elif HealthStatus.WARNING in statuses:
            overall_status = "warning"
        elif all(s == HealthStatus.OK for s in statuses):
            overall_status = "healthy"
        else:
            overall_status = "unknown"

        # Count statuses
        status_summary = {
            "healthy": sum(1 for s in statuses if s == HealthStatus.OK),
            "warning": sum(1 for s in statuses if s == HealthStatus.WARNING),
            "critical": sum(
                1 for s in statuses if s in [HealthStatus.CRITICAL, HealthStatus.ERROR]
            ),
            "unknown": sum(1 for s in statuses if s == HealthStatus.UNKNOWN),
        }

        # Build health status structure
        now = datetime.now(timezone.utc)
        health_status = {
            "station_id": self.station_id,
            "receiver_type": self.get_receiver_type(),
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "schema_version": "1.0",
            "connection": connection_data,
            "overall_status": overall_status,
            "status_summary": status_summary,
            "extraction_metadata": {
                "extraction_time": now.isoformat().replace("+00:00", "Z"),
                "data_source": "receiver_direct",
                "tool_version": "0.1.0",
            },
        }

        # Add optional sections if provided
        if metrics:
            health_status["metrics"] = metrics
        if data_quality:
            health_status["data_quality"] = data_quality
        if network:
            health_status["network"] = network
        if receiver_specific:
            # Add receiver-specific data under receiver type key
            receiver_key = self.get_receiver_type().lower()
            health_status[receiver_key] = receiver_specific

        return health_status

    def save_health_to_json(self, health_data: Dict[str, Any]) -> Optional[str]:
        """Save health data to JSON file.

        Helper method to save health data to status_1hr/json/ directory.

        Args:
            health_data: Health data dictionary to save

        Returns:
            Path to saved JSON file or None if save failed
        """
        from ..health import HealthJSONWriter

        try:
            # Build base path for current year/month
            now = datetime.now(timezone.utc)
            year = now.year
            month = now.strftime("%b").lower()
            base_path = f"{self.data_prepath}/{year}/{month}"

            # Write JSON file
            writer = HealthJSONWriter(base_path=base_path, station_id=self.station_id)
            json_path = writer.write_health_data(health_data)

            # Update latest.json symlink
            writer.write_latest_symlink(json_path)

            return str(json_path)

        except Exception as e:
            self.logger.error(f"Failed to save health data to JSON: {e}")
            return None

    def save_health_to_database(
        self, health_data: Dict[str, Any], connection_string: Optional[str] = None
    ) -> bool:
        """Save health data to PostgreSQL database.

        Helper method to save health data to checkcomm table.

        Args:
            health_data: Health data dictionary to save
            connection_string: Optional PostgreSQL connection string

        Returns:
            True if save successful, False otherwise
        """
        from ..health import HealthDatabaseWriter

        try:
            with HealthDatabaseWriter(connection_string) as db:
                return db.write_health_data(health_data)

        except Exception as e:
            self.logger.error(f"Failed to save health data to database: {e}")
            return False

    @abstractmethod
    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station information
        """
        pass

    def get_receiver_type(self) -> str:
        """Get receiver type identifier.

        Returns:
            String identifier for receiver type
        """
        return self.__class__.__name__

    def get_station_id(self) -> str:
        """Get station identifier.

        Returns:
            Station ID string
        """
        return self.station_id

    def __str__(self) -> str:
        """String representation of receiver."""
        return f"{self.get_receiver_type()}({self.station_id})"

    def __repr__(self) -> str:
        """Detailed string representation of receiver."""
        return f"{self.__class__.__name__}(station_id='{self.station_id}')"

    def build_archive_path(self, dt, session: str) -> str:
        """Build archive path using unified path builder.

        Args:
            dt: datetime object for the file
            session: Session type (e.g., '15s_24hr')

        Returns:
            Complete archive path for the file
        """
        # Get archive template from configuration
        template = self.receivers_config.get_archive_template()
        data_prepath = self.receivers_config.get_data_prepath()
        extension = self.get_file_extension()

        # Create template with data_prepath and extension
        full_template = template.format(
            data_prepath=data_prepath,
            station='{station}',
            session='{session}',
            extension=extension,
            session_letter='{session_letter}'
        )

        # Use unified build_path method with "1D" frequency for archives
        archive_paths = self.build_path(dt, full_template, session, "1D")
        return archive_paths[0]

    def build_path(self, dt_input, path_template: str, session: str, frequency: str = "1H", start_time=None, end_time=None) -> list:
        """Unified path builder using gtimes datepathlist with comprehensive parameters.

        This method consolidates all path generation logic from receivers_config,
        polarx5 hourly handling, and general path building into one unified approach.

        Args:
            dt_input: datetime object, list of datetimes, or None (use start_time/end_time)
            path_template: gtimes template string (e.g., 'path/%Y/%m/file%j%H.ext')
            session: Session type for session-specific formatting
            frequency: Time frequency ('1H', '1D', etc.) for gtimes datepathlist
            start_time: Start datetime (used when dt_input is None)
            end_time: End datetime (used when dt_input is None)

        Returns:
            List of formatted path strings

        Examples:
            # Archive path for single datetime
            archive_path = self.build_path(dt, archive_template, session, "1D")

            # Remote paths for datetime list
            remote_paths = self.build_path(dt_list, remote_template, session, "1H")

            # Generate datetime list with frequency
            paths = self.build_path(None, template, session, "1H", start, end)
        """
        import gtimes.timefunc as gt

        # Handle different input types
        if dt_input is None:
            # Generate datetime list manually (gtimes closed parameter doesn't work reliably)
            from datetime import timedelta
            dt_list = []
            current = start_time

            if frequency == "1H":
                # Hourly sessions
                while current < end_time:  # end_time is exclusive
                    dt_list.append(current)
                    current += timedelta(hours=1)
            elif frequency == "1D":
                # Daily sessions
                while current < end_time:  # end_time is exclusive (don't include today)
                    dt_list.append(current)
                    current += timedelta(days=1)
            else:
                # Fallback to gtimes for other frequencies
                dt_list = gt.datepathlist(
                    "#datelist",
                    frequency,
                    starttime=start_time,
                    endtime=end_time,
                    datelist=[],
                    closed="left",
                )
        elif isinstance(dt_input, list):
            dt_list = dt_input
        else:
            # Single datetime
            dt_list = [dt_input]

        # Substitute receiver-specific placeholders
        if '{station}' in path_template or '{session_letter}' in path_template or '{session}' in path_template:
            session_letter = self.get_session_letter(session)
            path_template = path_template.format(
                station=self.station_id,
                session=session,
                session_letter=session_letter
            )

        # Use gtimes datepathlist for consistent datetime formatting
        return gt.datepathlist(
            path_template,
            frequency,
            datelist=dt_list,
            closed="both"
        )

    @abstractmethod
    def get_file_extension(self) -> str:
        """Get file extension for this receiver type.

        Returns:
            File extension including compression (e.g., '.sbf.gz', '.obs.gz')
        """
        pass

    @abstractmethod
    def get_session_letter(self, session: str) -> str:
        """Get session letter for this receiver type and session.

        Args:
            session: Session type (e.g., '15s_24hr')

        Returns:
            Session letter code (e.g., 'a', 'b', 'c')
        """
        pass
