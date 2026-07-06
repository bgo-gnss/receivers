"""Base download manager with common download logic for all receiver types.

Enhanced with Phase 1 utilities for unified validation, archiving, and retry logic.
"""

import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import gtimes.timefunc as gt

# Phase 1 utilities - unified validation and archiving
from ..utils.archive_validator import ArchiveValidator
from ..utils.file_archiver import ArchiveMode, FileArchiver
from ..utils.time_processor import TimeParameterProcessor
from .exceptions import ConfigurationError, ConnectionError


class BaseDownloadManager(ABC):
    """Abstract base class for receiver download management.

    This class contains common download logic that is shared across
    all receiver types, while allowing receiver-specific implementations
    for connection and file handling.
    """

    def __init__(
        self,
        station_id: str,
        station_config: Dict[str, Any],
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize download manager.

        Args:
            station_id: Station identifier
            station_config: Station configuration
            logger: Optional logger instance
        """
        self.station_id = station_id.upper()
        self.station_config = station_config
        self.logger = logger or logging.getLogger(
            f"{__class__.__name__}.{self.station_id}"
        )

        # Initialize common configuration
        self._setup_common_config()

        # Initialize Phase 1 utilities for unified validation and archiving
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)

    def _setup_common_config(self) -> None:
        """Set up common configuration shared across receivers."""
        # Connection information
        try:
            self.ip_address = self.station_config["router"]["ip"]
            self.port = int(self.station_config["receiver"]["ftpport"])
        except KeyError as e:
            raise ConfigurationError(
                f"Missing configuration key: {e}",
                station_id=self.station_id,
                config_field=str(e),
            )

        # Timeout configuration
        timeouts = self.station_config.get("connection", {}).get("timeouts", {})
        self.connection_timeout = timeouts.get("connection_timeout", 20)
        self.inactivity_timeout = timeouts.get("inactivity_timeout", 60)
        self.progress_timeout = timeouts.get("progress_timeout", 300)
        self.min_speed_threshold = timeouts.get("min_speed_threshold", 2048)

        # Path configuration
        paths = self.station_config.get("paths", {})
        self.data_prepath = paths.get("data_prepath", "/data/")
        self.receiver_base_path = paths.get("receiver_base_path", "/DSK1/SSN/")

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """Test connection to receiver.

        Returns:
            Dictionary with connection test results
        """
        pass

    @abstractmethod
    def establish_connection(self) -> Any:
        """Establish connection to receiver.

        Returns:
            Connection object (receiver-specific type)
        """
        pass

    @abstractmethod
    def close_connection(self, connection: Any) -> None:
        """Close connection to receiver.

        Args:
            connection: Connection object to close
        """
        pass

    @abstractmethod
    def get_remote_file_list(self, connection: Any, remote_path: str) -> List[str]:
        """Get list of files in remote directory.

        Args:
            connection: Active connection object
            remote_path: Remote directory path

        Returns:
            List of filenames in remote directory
        """
        pass

    @abstractmethod
    def download_file(
        self,
        connection: Any,
        remote_file_path: str,
        local_file_path: str,
        resume_offset: int = 0,
    ) -> Dict[str, Any]:
        """Download a single file from receiver.

        Args:
            connection: Active connection object
            remote_file_path: Full path to remote file
            local_file_path: Full path for local file
            resume_offset: Byte offset to resume from

        Returns:
            Dictionary with download results including 'success' key
        """
        pass

    def download_with_retry(
        self,
        connection: Any,
        remote_file_path: str,
        local_file_path: str,
        remote_file_size: Optional[int] = None,
        resume_offset: int = 0,
        max_retries: int = 3,
        initial_delay: float = 0.5,
    ) -> Tuple[Any, Dict[str, Any]]:
        """Download file with automatic retry and reconnection on timeout/connection errors.

        This implements Fix #2: protocol-agnostic retry with reconnection.
        When a timeout occurs, the connection is closed and re-established before retrying.

        Args:
            connection: Active connection object
            remote_file_path: Full path to remote file
            local_file_path: Full path for local file
            remote_file_size: Optional remote file size for validation
            resume_offset: Byte offset to resume from
            max_retries: Maximum number of retry attempts (default: 3)
            initial_delay: Initial retry delay in seconds (default: 0.5)

        Returns:
            Tuple of (connection, result_dict):
            - connection: Connection object (may be reconnected)
            - result_dict: Download result with 'success' key

        Raises:
            Non-retryable errors (authentication, file not found)
        """
        # Timeout/connection error patterns that need reconnection
        timeout_patterns = [
            "timed out",
            "timeout",
            "cannot read from timed out",
            "connection reset",
            "broken pipe",
            "connection refused",
        ]

        # Non-retryable error patterns
        non_retryable_patterns = [
            "530",  # Authentication failed
            "550",  # File not found
            "not found",
            "no such file",
            "authentication",
            "login",
        ]

        last_exception = None

        for attempt in range(max_retries + 1):  # +1 for initial attempt
            try:
                # Attempt download
                result = self.download_file(
                    connection, remote_file_path, local_file_path, resume_offset
                )

                # Success - return connection and result
                return connection, result

            except Exception as e:
                error_msg = str(e).lower()
                last_exception = e

                # Check if this is a non-retryable error
                if any(pattern in error_msg for pattern in non_retryable_patterns):
                    # File not found or authentication - don't retry
                    raise

                # This is a retryable error
                if attempt < max_retries:
                    # Calculate delay with increasing backoff
                    delay = initial_delay * (attempt + 1)
                    self.logger.warning(
                        f"⚠️  Download attempt {attempt + 1} failed: {e}"
                    )

                    # Check if we need to reconnect (timeout/connection errors)
                    if any(pattern in error_msg for pattern in timeout_patterns):
                        self.logger.info(
                            "🔄 Closing dead connection and reconnecting..."
                        )
                        try:
                            self.close_connection(connection)
                        except:
                            pass  # Ignore errors closing dead connection

                        # Reconnect
                        connection = self.establish_connection()
                        if not connection:
                            self.logger.error(
                                "❌ Failed to reconnect - aborting retries"
                            )
                            raise ConnectionError("Could not reconnect to receiver")

                        self.logger.info("✅ Reconnected successfully")

                    self.logger.info(
                        f"🔄 Retrying in {delay:.1f}s (attempt {attempt + 2}/{max_retries + 1})..."
                    )
                    time.sleep(delay)
                else:
                    # Final attempt failed
                    self.logger.error(
                        f"❌ Download failed after {max_retries + 1} attempts: {e}"
                    )
                    raise last_exception

    def process_time_parameters(
        self,
        start: Optional[Union[datetime, str]],
        end: Optional[Union[datetime, str]],
        session: str,
        frequency: str,
    ) -> tuple[datetime, datetime]:
        """Process and validate time parameters.

        Args:
            start: Start time
            end: End time
            session: Session type
            frequency: File frequency

        Returns:
            Tuple of (start_datetime, end_datetime)
        """
        # Handle hourly vs daily sessions
        is_hourly = "1h" in session.lower() or frequency.lower() == "1h"

        if is_hourly:
            # Hourly data processing
            if end is None:
                end = datetime.now() - timedelta(hours=1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.replace(minute=0, second=0, microsecond=0)

            if start is None:
                start = end - timedelta(hours=24)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.replace(minute=0, second=0, microsecond=0)
        else:
            # Daily data processing
            if end is None:
                end = gt.currDatetime(-1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.date()

            if start is None:
                start = end - timedelta(days=10)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.date()

        return start, end

    def generate_file_list(
        self, start: datetime, end: datetime, session: str, frequency: str
    ) -> Dict[datetime, tuple[str, str]]:
        """Generate list of files to download with archive paths.

        Args:
            start: Start time
            end: End time
            session: Session type
            frequency: File frequency

        Returns:
            Dictionary mapping datetime to (archive_path, remote_filename) tuples
        """
        # Generate datetime list
        if frequency == "1H":
            file_datetime_list = []
            current = start
            while current <= end:
                file_datetime_list.append(current)
                current += timedelta(hours=1)
        else:
            file_datetime_list = gt.datepathlist(
                "#datelist",
                frequency,
                starttime=start,
                endtime=end,
                datelist=[],
                closed="both",
            )

        # Generate archive paths and remote filenames
        file_dict = {}
        for dt in file_datetime_list:
            archive_path = self._generate_archive_path(dt, session)
            remote_filename = self._generate_remote_filename(dt, session)
            file_dict[dt] = (archive_path, remote_filename)

        return file_dict

    @abstractmethod
    def _generate_archive_path(self, dt: datetime, session: str) -> str:
        """Generate archive path for a file.

        Args:
            dt: File datetime
            session: Session type

        Returns:
            Full archive path
        """
        pass

    @abstractmethod
    def _generate_remote_filename(self, dt: datetime, session: str) -> str:
        """Generate remote filename for a file.

        Args:
            dt: File datetime
            session: Session type

        Returns:
            Remote filename
        """
        pass

    def identify_missing_files(
        self, file_dict: Dict[datetime, Tuple[str, str]], tmp_dir: Optional[Path] = None
    ) -> Tuple[Dict[datetime, Tuple[str, str]], Dict[str, Path], int]:
        """Identify files that need to be downloaded using Phase 1 validation.

        This method uses the Phase 1 ArchiveValidator to check for files in:
        1. Archive directory (properly archived)
        2. Archive directory with compression (.gz)
        3. Tmp directory (downloaded but not archived)

        Args:
            file_dict: Dictionary mapping datetime -> (archive_path, remote_filename)
            tmp_dir: Optional tmp directory to check for unarchived files

        Returns:
            Tuple of (missing_files, files_in_tmp, files_found):
            - missing_files: Files that need to be downloaded
            - files_in_tmp: Files in tmp that need archiving (filename -> path)
            - files_found: Count of files found in archive
        """
        # Convert to format expected by ArchiveValidator
        files_dict = {}  # filename -> archive_path
        archive_paths_dict = {}  # filename -> archive_path

        for dt, (archive_path, remote_filename) in file_dict.items():
            files_dict[remote_filename] = archive_path
            archive_paths_dict[remote_filename] = archive_path

        # Use Phase 1 batch validation
        missing_files_dict, found_count, validated_count, files_in_tmp_dict = (
            self.archive_validator.batch_validate_archives(
                files_dict, archive_paths_dict, tmp_dir
            )
        )

        # Convert back to datetime-keyed format
        missing_files = {}
        for remote_filename in missing_files_dict.keys():
            for dt, (arch_path, remote_file) in file_dict.items():
                if remote_file == remote_filename:
                    missing_files[dt] = (arch_path, remote_file)
                    break

        self.logger.info(
            f"Phase 1 validation: {validated_count} files checked, "
            f"{found_count} found, {len(missing_files_dict)} missing"
        )

        return missing_files, files_in_tmp_dict, found_count

    def archive_tmp_files(
        self, files_in_tmp_dict: Dict[str, Path], archive_paths_dict: Dict[str, str]
    ) -> int:
        """Archive files from tmp directory using Phase 1 FileArchiver.

        Args:
            files_in_tmp_dict: Mapping of filename -> tmp_path for files to archive
            archive_paths_dict: Mapping of filename -> archive_path destinations

        Returns:
            Number of files successfully archived
        """
        if not files_in_tmp_dict:
            return 0

        self.logger.info(
            f"Archiving {len(files_in_tmp_dict)} files from tmp directory..."
        )

        with FileArchiver(mode=ArchiveMode.BULK, logger=self.logger) as archiver:
            for filename, tmp_path in files_in_tmp_dict.items():
                archive_dest = archive_paths_dict.get(filename)
                if archive_dest:
                    archiver.archive_file(
                        tmp_path,
                        Path(archive_dest),
                        compress=False,  # Files are already compressed
                        remove_tmp=True,
                    )

        stats = archiver.get_statistics()
        self.logger.info(
            f"Archived {stats['successful']}/{len(files_in_tmp_dict)} files from tmp to archive"
        )
        return stats["successful"]

    def archive_file(self, tmp_file_path: str, archive_path: str) -> bool:
        """Archive downloaded file to final location.

        Args:
            tmp_file_path: Path to temporary downloaded file
            archive_path: Final archive path

        Returns:
            True if archiving succeeded, False otherwise
        """
        if not os.path.isfile(tmp_file_path):
            self.logger.warning(f"Cannot archive - file not found: {tmp_file_path}")
            return False

        tmp_size = os.path.getsize(tmp_file_path)
        archive_dir = os.path.dirname(archive_path)

        # Create archive directory
        os.makedirs(archive_dir, exist_ok=True)

        # Check if archive file already exists
        if os.path.isfile(archive_path):
            archive_size = os.path.getsize(archive_path)
            if tmp_size == archive_size:
                self.logger.info(
                    f"Archive file already exists with same size: {archive_path}"
                )
                os.unlink(tmp_file_path)
                return True

        # Atomic move to archive location
        try:
            self.logger.info(
                f"📦 Archiving {os.path.basename(archive_path)} ({tmp_size:,} bytes)"
            )
            # shutil.move, not os.rename: tmp staging and the archive live on
            # different filesystems (/tmp LV vs /mnt/data) — rename would EXDEV.
            shutil.move(tmp_file_path, archive_path)

            # Verify successful archive
            if (
                os.path.isfile(archive_path)
                and os.path.getsize(archive_path) == tmp_size
            ):
                self.logger.info(f"✅ Archived to: {archive_path}")
                return True
            else:
                self.logger.error("❌ Archive verification failed")
                return False

        except Exception as e:
            self.logger.error(f"❌ Archive error: {e}")
            if os.path.isfile(tmp_file_path):
                os.unlink(tmp_file_path)
            return False

    def download_session(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        session: str = "15s_24hr",
        frequency: str = "1D",
        sync: bool = False,
        clean_tmp: bool = True,
        archive: bool = True,
        immediate_archive: bool = True,
        tmp_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download data session with common logic.

        Args:
            start: Start time
            end: End time
            session: Session type
            frequency: File frequency
            sync: Whether to actually download files
            clean_tmp: Whether to clean temporary files
            archive: Whether to archive files
            immediate_archive: Whether to archive immediately after each download
            tmp_dir: Temporary download directory (uses instance tmp_dir if not provided)

        Returns:
            Dictionary with download results
        """
        start_time = time.time()
        self.logger.info(f"Starting download session: {session}")

        # Process time parameters
        start_dt, end_dt = self.process_time_parameters(start, end, session, frequency)
        self.logger.info(f"Time range: {start_dt} to {end_dt}")

        # Set up temporary directory - use instance tmp_dir if not provided
        if tmp_dir is None:
            tmp_dir = getattr(self, "tmp_dir", "/tmp/download/")
        tmp_dir_path = Path(tmp_dir) / self.station_id
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

        # Generate file list
        file_dict = self.generate_file_list(start_dt, end_dt, session, frequency)

        # Use Phase 1 validation - returns missing files AND files in tmp
        missing_files, files_in_tmp_dict, files_found = self.identify_missing_files(
            file_dict, tmp_dir_path
        )

        # Archive files from tmp if found and archive flag is set
        files_archived_from_tmp = 0
        if files_in_tmp_dict and archive:
            # Build archive paths dict for tmp files
            archive_paths_dict = {
                filename: file_dict[dt][0]  # archive_path from file_dict
                for filename, tmp_path in files_in_tmp_dict.items()
                for dt, (arch_path, remote_file) in file_dict.items()
                if remote_file == filename
            }
            files_archived_from_tmp = self.archive_tmp_files(
                files_in_tmp_dict, archive_paths_dict
            )

        if not missing_files:
            self.logger.info("All files up to date")
            return {
                "status": "up_to_date",
                "files_checked": len(file_dict),
                "files_missing": 0,
                "files_downloaded": 0,
                "files_archived_from_tmp": files_archived_from_tmp,
                "duration": time.time() - start_time,
            }

        self.logger.info(f"Missing files: {len(missing_files)}")

        downloaded_files = []
        total_bytes = 0

        if sync:
            try:
                # Establish connection
                connection = self.establish_connection()

                try:
                    # Download each missing file
                    for dt, (archive_path, remote_filename) in missing_files.items():
                        result = self._download_single_file(
                            connection,
                            dt,
                            archive_path,
                            remote_filename,
                            tmp_dir_path,
                            clean_tmp,
                            archive,
                            immediate_archive,
                        )

                        if result["success"]:
                            downloaded_files.append(result["final_path"])
                            total_bytes += result["bytes_downloaded"]

                finally:
                    self.close_connection(connection)

            except Exception as e:
                self.logger.error(f"Download session failed: {e}")
                return {
                    "status": "failed",
                    "error": str(e),
                    "files_checked": len(file_dict),
                    "files_missing": len(missing_files),
                    "files_downloaded": len(downloaded_files),
                    "duration": time.time() - start_time,
                }

        return {
            "status": "completed" if sync else "dry_run",
            "files_checked": len(file_dict),
            "files_missing": len(missing_files),
            "files_downloaded": len(downloaded_files),
            "downloaded_files": downloaded_files,
            "total_bytes": total_bytes,
            "duration": time.time() - start_time,
        }

    def _download_single_file(
        self,
        connection: Any,
        dt: datetime,
        archive_path: str,
        remote_filename: str,
        tmp_dir: Path,
        clean_tmp: bool,
        archive: bool,
        immediate_archive: bool,
    ) -> Dict[str, Any]:
        """Download a single file with proper error handling.

        Returns:
            Dictionary with download results including success status
        """
        local_file = tmp_dir / remote_filename

        # Handle existing files
        if local_file.exists() and clean_tmp:
            local_file.unlink()

        try:
            # Get remote file path
            remote_path = self._get_remote_file_path(dt)
            remote_file_path = f"{remote_path}{remote_filename}"

            # Download file
            result = self.download_file(connection, remote_file_path, str(local_file))

            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error", "Download failed"),
                    "bytes_downloaded": 0,
                    "final_path": None,
                }

            bytes_downloaded = os.path.getsize(local_file) if local_file.exists() else 0

            # Archive file if requested
            final_path = str(local_file)
            if archive and immediate_archive:
                if self.archive_file(str(local_file), archive_path):
                    final_path = archive_path

            return {
                "success": True,
                "bytes_downloaded": bytes_downloaded,
                "final_path": final_path,
            }

        except Exception as e:
            self.logger.error(f"Failed to download {remote_filename}: {e}")
            return {
                "success": False,
                "error": str(e),
                "bytes_downloaded": 0,
                "final_path": None,
            }

    @abstractmethod
    def _get_remote_file_path(self, dt: datetime) -> str:
        """Get remote directory path for a given datetime.

        Args:
            dt: File datetime

        Returns:
            Remote directory path
        """
        pass
