"""Abstract base class for GPS/GNSS receivers."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Union

from .config_manager import get_config_manager
from ..config.receivers_config import get_receivers_config


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
            **kwargs: Additional receiver-specific parameters

        Returns:
            Dictionary with download results and file information
        """
        pass

    @abstractmethod
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of receiver.

        Returns:
            Dictionary with health metrics and status information
        """
        pass

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
            # Generate datetime list using start/end times and frequency
            if frequency == "1H":
                # Special handling for hourly sessions (from polarx5.py)
                from datetime import timedelta
                dt_list = []
                current = start_time
                while current <= end_time:
                    dt_list.append(current)
                    current += timedelta(hours=1)
            else:
                # Use gtimes for other frequencies
                dt_list = gt.datepathlist(
                    "#datelist",
                    frequency,
                    starttime=start_time,
                    endtime=end_time,
                    datelist=[],
                    closed="both",
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
