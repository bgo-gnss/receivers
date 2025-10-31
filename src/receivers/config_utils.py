"""Configuration utilities for the receivers package.

This module provides utility functions for retrieving station configurations
from gps_parser with complete integration and no hardcoded fallbacks.
All configuration data comes from the centralized gps_parser package.
"""

import logging
import sys
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

# Import gtimes for path construction
try:
    import gtimes.timefunc as gt
except ImportError:
    raise ImportError("gtimes package not found. Please install gtimes: pip install gtimes")

# Import gps_parser with proper error handling
try:
    import gps_parser
except ImportError:
    try:
        # Try with path adjustment for development
        sys.path.append('../gps_parser/src')
        import gps_parser
    except ImportError:
        raise ImportError(
            "gps_parser package not found. Please install gps_parser or set up your environment:\n"
            "1. cd ../gps_parser && pip install -e .\n"
            "2. OR: export PYTHONPATH=../gps_parser/src:$PYTHONPATH\n"
            "3. Ensure config files exist: ~/.config/gpsconfig/stations.cfg"
        )

logger = logging.getLogger(__name__)


def get_station_config(station_id: str) -> Optional[Dict[str, Any]]:
    """Get complete station configuration from gps_parser.

    Args:
        station_id: Station identifier (e.g., 'ELDC', 'ORFC')

    Returns:
        Complete station configuration dictionary with all settings
        structured for receivers package compatibility, or None if not found

    Configuration includes:
        - Basic station info (name, type, connection details)
        - Timeout configuration (from TIMEOUT_CATEGORIES)
        - FTP mode configuration (from NETWORK_RULES or explicit)
        - System paths (from PATHS section)
        - Default values (from DEFAULTS section)
    """
    try:
        # Initialize gps_parser ConfigParser
        config_parser = gps_parser.ConfigParser()

        # Get raw station information
        station_info = config_parser.getStationInfo(station_id)
        if not station_info or 'station' not in station_info:
            logger.error(f"Station {station_id} not found in gps_parser configuration")
            return None

        raw_config = station_info['station']

        # Validate required fields
        required_fields = ['router_ip', 'receiver_ftpport', 'receiver_type']
        missing_fields = [field for field in required_fields if field not in raw_config]
        if missing_fields:
            logger.error(f"Station {station_id} missing required fields: {missing_fields}")
            return None

        # Get enhanced configuration data from gps_parser
        router_ip = raw_config['router_ip']

        # Get timeout configuration
        timeout_config = config_parser.getStationTimeout(station_id)

        # Get FTP mode
        ftp_mode = config_parser.getStationFtpMode(station_id, router_ip)

        # Get system paths - USE RECEIVERS.CFG AS SINGLE SOURCE OF TRUTH
        # Import ReceiversConfig to read from receivers.cfg (not postprocess.cfg!)
        from .config.receivers_config import get_receivers_config
        receivers_config = get_receivers_config()
        data_prepath = receivers_config.get_prepath()  # From receivers.cfg

        # Tool paths still from gps_parser (postprocess.cfg) until migrated
        bin2asc_path = config_parser.getSystemPath('bin2asc_path')
        receiver_base_path = config_parser.getSystemPath('receiver_base_path')

        # Get default values
        default_session = config_parser.getDefaultValue('default_session')
        default_compression = config_parser.getDefaultValue('default_compression')
        default_days_back = config_parser.getDefaultValue('default_days_back')

        # Create comprehensive configuration structure
        station_config = {
            # Basic station information
            'station_id': station_id,
            'station_name': raw_config.get('station_name', station_id),
            'receiver_type': raw_config['receiver_type'],

            # Network configuration
            'router': {
                'ip': router_ip,
                'type': raw_config.get('router_type', ''),
                'ftp_mode': ftp_mode
            },

            # Receiver configuration
            'receiver': {
                'type': raw_config['receiver_type'],
                'ftpport': raw_config['receiver_ftpport'],
                'httpport': raw_config.get('receiver_httpport', '8060'),
                'controlport': raw_config.get('receiver_controlport', '28784'),
                # Authentication credentials (for HTTP Basic Auth, FTP login, etc.)
                'user': raw_config.get('receiver_user', ''),
                'pwd': raw_config.get('receiver_pwd', ''),
                # Firmware bug handling
                'firmware_underscore_pad': raw_config.get('receiver_firmware_underscore_pad', '').lower() in ['true', '1', 'yes']
            },

            # Connection and timing configuration
            'connection': {
                'type': raw_config.get('connection_type', ''),
                'timeouts': timeout_config
            },

            # System paths
            'paths': {
                'data_prepath': data_prepath,
                'bin2asc_path': bin2asc_path,
                'receiver_base_path': receiver_base_path
            },

            # Default values
            'defaults': {
                'session': default_session,
                'compression': default_compression,
                'days_back': default_days_back
            }
        }

        logger.debug(f"Successfully loaded configuration for {station_id}")
        return station_config

    except Exception as e:
        logger.error(f"Failed to get configuration for {station_id}: {e}")
        return None


def get_session_config(session_type: str) -> Dict[str, str]:
    """Get session configuration from gps_parser.

    Args:
        session_type: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')

    Returns:
        Session configuration dictionary with letter, path, receiver_path
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getSessionConfig(session_type)
    except Exception as e:
        logger.error(f"Failed to get session config for {session_type}: {e}")
        raise


def get_system_path(path_name: str) -> str:
    """Get system tool path from gps_parser.

    Args:
        path_name: Path identifier (e.g., 'bin2asc_path', 'data_prepath')

    Returns:
        Expanded path string
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getSystemPath(path_name)
    except Exception as e:
        logger.error(f"Failed to get system path {path_name}: {e}")
        raise


def get_default_value(setting_name: str):
    """Get default value from gps_parser.

    Args:
        setting_name: Setting identifier (e.g., 'default_session', 'default_days_back')

    Returns:
        Default value with appropriate type conversion
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getDefaultValue(setting_name)
    except Exception as e:
        logger.error(f"Failed to get default value {setting_name}: {e}")
        raise


def validate_station_config(station_id: str) -> Dict[str, Any]:
    """Validate station configuration using gps_parser.

    Args:
        station_id: Station identifier

    Returns:
        Validation results dictionary
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.validateStationConfig(station_id)
    except Exception as e:
        logger.error(f"Failed to validate configuration for {station_id}: {e}")
        raise


def build_data_paths(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
    compression: str = ".gz"
) -> List[str]:
    """Build data file paths using gtimes for proper GPS time handling.

    Args:
        station_id: Station identifier (e.g., 'ELDC')
        session_type: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')
        start_time: Start datetime
        end_time: End datetime
        compression: File compression extension

    Returns:
        List of complete file paths for downloading

    Uses gtimes datepathlist for accurate GPS time-based path construction.
    """
    try:
        # Get configuration
        station_config = get_station_config(station_id)
        if not station_config:
            raise ValueError(f"No configuration found for station {station_id}")

        session_config = get_session_config(session_type)
        data_prepath = station_config['paths']['data_prepath']

        # Determine frequency based on session type
        frequency_map = {
            '15s_24hr': '1D',    # Daily files
            '1Hz_1hr': '1H',     # Hourly files
            'status_1hr': '1H'   # Hourly status files
        }
        frequency = frequency_map.get(session_type, '1D')

        # Build path format using gtimes-compatible format
        # Format: /data/YYYY/MMM/STATION/SESSION/raw/STATION_DDDF.YYT.gz
        # Where: DDD=day of year, F=file sequence, YY=year, T=session type
        path_format = f"{data_prepath}/%Y/#b/{station_id}/{session_config['session_path']}/raw/{station_id}_%j0.%y{session_config['session_letter']}{compression}"

        # Use gtimes to generate path list
        paths = gt.datepathlist(
            stringformat=path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left"
        )

        logger.debug(f"Generated {len(paths)} data paths for {station_id} session {session_type}")
        return paths

    except Exception as e:
        logger.error(f"Failed to build data paths for {station_id}: {e}")
        raise


def build_receiver_paths(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime
) -> List[str]:
    """Build receiver-side file paths using gtimes and gps_parser configuration.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_time: Start datetime
        end_time: End datetime

    Returns:
        List of receiver-side paths for FTP download

    Uses session configuration from gps_parser for receiver path structure.
    """
    try:
        # Get session configuration
        session_config = get_session_config(session_type)
        receiver_base_path = get_system_path('receiver_base_path')

        # Determine frequency
        frequency_map = {
            '15s_24hr': '1D',
            '1Hz_1hr': '1H',
            'status_1hr': '1H'
        }
        frequency = frequency_map.get(session_type, '1D')

        # Build receiver path format
        # Format: /DSK1/SSN/STATION_DDDF.YYT
        receiver_path_format = f"{receiver_base_path}/{station_id}_%j0.%y{session_config['session_letter']}"

        # Generate receiver paths using gtimes
        receiver_paths = gt.datepathlist(
            stringformat=receiver_path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left"
        )

        logger.debug(f"Generated {len(receiver_paths)} receiver paths for {station_id}")
        return receiver_paths

    except Exception as e:
        logger.error(f"Failed to build receiver paths for {station_id}: {e}")
        raise


def build_archive_paths(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
    compression: str = ".gz"
) -> List[str]:
    """Build archive destination paths using gtimes and gps_parser configuration.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_time: Start datetime
        end_time: End datetime
        compression: File compression extension

    Returns:
        List of archive destination paths

    Uses gtimes for proper GPS time-based directory structure.
    """
    try:
        # Get configuration
        station_config = get_station_config(station_id)
        session_config = get_session_config(session_type)
        data_prepath = station_config['paths']['data_prepath']

        # Determine frequency
        frequency_map = {
            '15s_24hr': '1D',
            '1Hz_1hr': '1H',
            'status_1hr': '1H'
        }
        frequency = frequency_map.get(session_type, '1D')

        # Build archive path format
        # Format: /data/YYYY/MMM/STATION/SESSION/STATION_DDDF.YYT.gz
        archive_path_format = f"{data_prepath}/%Y/#b/{station_id}/{session_config['session_path']}/{station_id}_%j0.%y{session_config['session_letter']}{compression}"

        # Generate archive paths using gtimes
        archive_paths = gt.datepathlist(
            stringformat=archive_path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left"
        )

        logger.debug(f"Generated {len(archive_paths)} archive paths for {station_id}")
        return archive_paths

    except Exception as e:
        logger.error(f"Failed to build archive paths for {station_id}: {e}")
        raise