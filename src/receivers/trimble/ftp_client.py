"""FTP client for Trimble receivers.

Shared FTP download functionality for NetR9/NetRS receivers,
based on the PolaRX5 implementation patterns.
"""

import logging
import os
import time
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm


class TrimbleFTPClient:
    """FTP client for Trimble NetR9/NetRS receivers."""
    
    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize FTP client.
        
        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        self.station_id = station_id.upper()
        self.logger = logging.getLogger(f"receivers.trimble.ftp.{self.station_id}")
        
        # Extract FTP connection details
        self.ip = station_config["router"]["ip"]
        self.ftp_port = station_config["receiver"].get("ftpport", 21)
        self.timeout_category = station_config["receiver"].get("timeout_category", "mobile")
        
        # Set timeouts based on category
        self.timeouts = {
            "fixed_wired": 30,
            "mobile": 60,
            "very_remote": 120
        }
        self.connection_timeout = self.timeouts.get(self.timeout_category, 60)
        
        # FTP mode configuration
        ftp_mode = station_config["receiver"].get("ftp_mode", "passive")
        self.pasv = (ftp_mode.lower() == "passive")
        
        # Authentication (NetR9/NetRS typically use anonymous FTP)
        self.username = station_config["receiver"].get("ftp_user", "anonymous")
        self.password = station_config["receiver"].get("ftp_password", "")
        
        self.logger.debug(f"Initialized FTP client for {self.station_id}: {self.ip}:{self.ftp_port}")
    
    def open_connection(self, timeout: Optional[int] = None) -> Optional[FTP]:
        """Open FTP connection to receiver.
        
        Args:
            timeout: Connection timeout in seconds
            
        Returns:
            FTP connection object or None if failed
        """
        connection_start = time.time()
        
        try:
            self.logger.info("Connecting to receiver...")
            ftp = FTP()
            
            if timeout is None:
                timeout = self.connection_timeout
                
            ftp.connect(self.ip, self.ftp_port, timeout=timeout)
            ftp.login(self.username, self.password)
            ftp.set_pasv(self.pasv)
            
            connection_time = time.time() - connection_start
            self.logger.info(f"Connection successful in {connection_time:.2f}s!")
            
            # Store connection time for performance tracking
            self._last_connection_time = connection_time
            
            return ftp
            
        except Exception as e:
            connection_time = time.time() - connection_start
            self.logger.error(f"Connection failed after {connection_time:.2f}s: {e}")
            self._last_connection_time = connection_time
            return None
    
    def download_files(self, files_dict: Dict[str, str], local_dir: Path, 
                      clean_tmp: bool = True, ftp: Optional[FTP] = None) -> List[str]:
        """Download files via FTP with progress tracking.
        
        Args:
            files_dict: Dictionary mapping filename to remote directory
            local_dir: Local directory for downloads
            clean_tmp: Whether to clean existing local files
            ftp: Existing FTP connection (optional)
            
        Returns:
            List of successfully downloaded file paths
        """
        downloaded_files = []
        own_ftp = ftp is None
        
        if own_ftp:
            ftp = self.open_connection()
            if not ftp:
                self.logger.error("Cannot download files - FTP connection failed")
                return downloaded_files
        
        try:
            # Log station connection details
            self.logger.info(f"Station connection: {self.ip}:{self.ftp_port}")
            
            # Track unique paths
            logged_paths = set()
            
            for file_name, remote_dir in sorted(files_dict.items(), reverse=True):
                # Log remote directory path only once per unique path
                if remote_dir not in logged_paths:
                    self.logger.info(f"Remote path: {remote_dir}")
                    logged_paths.add(remote_dir)
                
                success = self._download_single_file(ftp, file_name, remote_dir, local_dir, clean_tmp)
                if success:
                    downloaded_files.append(str(local_dir / file_name))
            
        finally:
            if own_ftp and ftp:
                try:
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        pass
        
        return downloaded_files
    
    def _download_single_file(self, ftp: FTP, file_name: str, remote_dir: str, 
                             local_dir: Path, clean_tmp: bool) -> bool:
        """Download a single file with progress tracking.
        
        Args:
            ftp: FTP connection
            file_name: Name of file to download
            remote_dir: Remote directory path
            local_dir: Local directory path
            clean_tmp: Whether to clean existing local files
            
        Returns:
            True if download successful, False otherwise
        """
        self.logger.info(f"Downloading {file_name}")
        
        local_file = local_dir / file_name
        remote_file = f"{remote_dir.rstrip('/')}/{file_name}"
        
        try:
            # Check if remote file exists and get size
            try:
                remote_file_size = ftp.size(remote_file)
                if remote_file_size is None or remote_file_size <= 0:
                    self.logger.warning(f"Remote file {file_name} has zero size or size unavailable")
                    return False
            except Exception as e:
                error_msg = str(e).lower()
                if "550" in error_msg or "not found" in error_msg:
                    # Check if we have a local copy we can use for archiving
                    if local_file.exists() and local_file.stat().st_size > 0:
                        self.logger.info(f"📁 Remote file {file_name} missing, using local copy")
                        return True  # Consider this "successful" for archiving
                    else:
                        self.logger.error(f"❌ Remote file {file_name} not found")
                        return False
                else:
                    self.logger.error(f"⚠️  Cannot check remote file {file_name}: {e}")
                    return False
            
            # Handle existing local file
            resume_pos = 0
            if local_file.exists():
                if clean_tmp:
                    local_file.unlink()
                    self.logger.info(f"🧹 Cleaned existing file: {file_name}")
                else:
                    local_size = local_file.stat().st_size
                    if local_size == remote_file_size:
                        self.logger.info(f"✅ File {file_name} already complete ({local_size:,} bytes)")
                        return True
                    elif local_size < remote_file_size:
                        resume_pos = local_size
                        self.logger.info(f"📄 Resuming download from {resume_pos:,} bytes (clean_tmp=False)")
                    else:
                        # Local file is larger - something's wrong
                        local_file.unlink()
                        self.logger.warning(f"🗑️ Local file larger than remote, restarting download")
            
            # Download with progress bar
            return self._download_with_progress(ftp, remote_file, local_file, 
                                              remote_file_size, resume_pos)
            
        except Exception as e:
            self.logger.error(f"Failed to download {file_name}: {e}")
            return False
    
    def _download_with_progress(self, ftp: FTP, remote_file: str, local_file: Path,
                               remote_size: int, resume_pos: int = 0) -> bool:
        """Download file with progress bar.
        
        Args:
            ftp: FTP connection
            remote_file: Remote file path
            local_file: Local file path
            remote_size: Remote file size in bytes
            resume_pos: Resume position for partial downloads
            
        Returns:
            True if download successful, False otherwise
        """
        try:
            # Open local file for writing
            mode = 'ab' if resume_pos > 0 else 'wb'
            
            with open(local_file, mode) as f:
                # Set up progress bar
                progress_bar = tqdm(
                    total=remote_size,
                    initial=resume_pos,
                    unit='B',
                    unit_scale=True,
                    desc=f"Downloading {local_file.name}"
                )
                
                def write_with_progress(data):
                    """Write data and update progress."""
                    f.write(data)
                    progress_bar.update(len(data))
                
                # Set resume position if needed
                if resume_pos > 0:
                    ftp.voidcmd(f'REST {resume_pos}')
                
                # Download the file
                ftp.retrbinary(f'RETR {remote_file}', write_with_progress)
                progress_bar.close()
            
            # Verify download
            final_size = local_file.stat().st_size
            if final_size == remote_size:
                self.logger.info(f"✅ File successfully downloaded ({final_size:,} bytes)")
                return True
            else:
                self.logger.error(f"❌ Download size mismatch: expected {remote_size:,}, got {final_size:,}")
                return False
                
        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            return False
    
    def test_connection(self) -> Dict[str, Any]:
        """Test FTP connection to receiver.
        
        Returns:
            Dictionary with connection test results
        """
        start_time = time.time()
        
        ftp = self.open_connection()
        success = ftp is not None
        
        if success:
            try:
                # Try a simple command
                ftp.pwd()
                ftp.quit()
            except Exception as e:
                self.logger.warning(f"Connection test command failed: {e}")
                success = False
        
        duration = time.time() - start_time
        
        return {
            "success": success,
            "duration": duration,
            "connection_time": getattr(self, '_last_connection_time', duration),
            "server": f"{self.ip}:{self.ftp_port}",
            "mode": "passive" if self.pasv else "active",
            "timeout_category": self.timeout_category
        }