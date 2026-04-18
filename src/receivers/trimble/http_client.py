"""Modern HTTP client for Trimble receivers.

Replaces the old sCurl.py implementation with modern requests-based HTTP communication.
Handles authentication, timeouts, retries, and integrates with adaptive timeout system.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry


class TrimbleHTTPClient:
    """HTTP client for Trimble NetR9/NetRS receivers."""

    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize HTTP client with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        self.station_id = station_id.upper()

        # Set up logging (matching PolaRX5 pattern)
        self.logger = self._get_logger()

        # Extract connection details
        self.ip = station_config["router"]["ip"]
        self.http_port = station_config["receiver"].get("httpport", 8060)
        self.timeout_category = station_config["receiver"].get(
            "timeout_category", "mobile"
        )

        # Get NetR9-specific timeout settings if available
        from ..config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()
        netr9_config = receivers_config.get_receiver_config("netr9")
        self.connect_timeout = netr9_config.get("http_timeout_connect", 15)
        self.read_timeout = netr9_config.get("http_timeout_read", 120)

        # Build base URL
        self.base_url = f"http://{self.ip}:{self.http_port}/"

        # Authentication
        self.auth = None
        receiver_config = station_config.get("receiver", {})
        username = receiver_config.get("user")
        password = receiver_config.get("pwd")
        if username and password:
            self.auth = HTTPBasicAuth(username, password)

        # Create session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],  # Updated parameter name
            backoff_factor=1,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set timeout based on timeout category
        self.timeouts = {
            "fixed_wired": {"connect": 5, "read": 10},
            "mobile": {"connect": 10, "read": 30},
            "very_remote": {"connect": 15, "read": 60},
        }
        self.timeout = self.timeouts.get(self.timeout_category, self.timeouts["mobile"])

    def get_url(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Make HTTP GET request to receiver endpoint.

        Args:
            endpoint: API endpoint path (e.g., '/status/voltage')
            params: Optional query parameters

        Returns:
            Tuple of (success, response_text, error_message)
        """
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        start_time = time.time()

        try:
            self.logger.debug(f"HTTP GET {url}")

            response = self.session.get(
                url,
                params=params,
                auth=self.auth,
                timeout=(self.connect_timeout, self.read_timeout),
                stream=True,  # Enable streaming for large downloads
            )

            duration = time.time() - start_time
            self.logger.debug(
                f"HTTP response: {response.status_code} in {duration:.2f}s"
            )

            # Check for HTTP errors
            response.raise_for_status()

            return True, response.text, None

        except requests.exceptions.Timeout as e:
            duration = time.time() - start_time
            error_msg = f"HTTP timeout after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except requests.exceptions.ConnectionError as e:
            duration = time.time() - start_time
            error_msg = f"HTTP connection error after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except requests.exceptions.HTTPError as e:
            duration = time.time() - start_time
            error_msg = f"HTTP error {response.status_code} after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Unexpected HTTP error after {duration:.2f}s: {e}"
            self.logger.error(error_msg)
            return False, None, error_msg

    def post_url(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Make HTTP POST request to receiver endpoint.

        Args:
            endpoint: API endpoint path
            data: Form data to send
            json_data: JSON data to send

        Returns:
            Tuple of (success, response_text, error_message)
        """
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        start_time = time.time()

        try:
            self.logger.debug(f"HTTP POST {url}")

            response = self.session.post(
                url,
                data=data,
                json=json_data,
                auth=self.auth,
                timeout=(self.connect_timeout, self.read_timeout),
            )

            duration = time.time() - start_time
            self.logger.debug(
                f"HTTP response: {response.status_code} in {duration:.2f}s"
            )

            # Check for HTTP errors
            response.raise_for_status()

            return True, response.text, None

        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            error_msg = f"HTTP POST error after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Unexpected HTTP POST error after {duration:.2f}s: {e}"
            self.logger.error(error_msg)
            return False, None, error_msg

    def test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to receiver.

        Returns:
            Dictionary with connection test results
        """
        start_time = time.time()

        # Try to fetch a simple endpoint to test connectivity
        success, response, error = self.get_url("/")

        duration = time.time() - start_time

        return {
            "success": success,
            "duration": duration,
            "response_size": len(response) if response else 0,
            "error": error,
            "base_url": self.base_url,
            "timeout_category": self.timeout_category,
        }

    def download_file(
        self, endpoint: str, expected_size: Optional[int] = None
    ) -> Tuple[bool, Any, Optional[str]]:
        """Download a file with streaming and progress-aware timeouts.

        Args:
            endpoint: API endpoint path for file download
            expected_size: Expected file size for progress tracking

        Returns:
            Tuple of (success, response_object, error_message)
        """
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        start_time = time.time()

        try:
            self.logger.debug(f"Downloading file from {url}")

            # Use longer timeout for file downloads
            connect_timeout = self.connect_timeout
            read_timeout = max(
                self.read_timeout, 300
            )  # At least 5 minutes for large files

            response = self.session.get(
                url,
                auth=self.auth,
                timeout=(connect_timeout, read_timeout),
                stream=True,  # Always stream for file downloads
            )

            # Check for HTTP errors
            response.raise_for_status()

            duration = time.time() - start_time
            content_length = response.headers.get("content-length")
            self.logger.debug(
                f"File download started, content-length: {content_length}, response time: {duration:.2f}s"
            )

            return True, response, None

        except requests.exceptions.Timeout as e:
            duration = time.time() - start_time
            error_msg = f"File download timeout after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except requests.exceptions.ConnectionError as e:
            duration = time.time() - start_time
            error_msg = f"File download connection error after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except requests.exceptions.HTTPError as e:
            duration = time.time() - start_time
            error_msg = f"File download HTTP error {response.status_code} after {duration:.2f}s: {e}"
            self.logger.warning(error_msg)
            return False, None, error_msg

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Unexpected file download error after {duration:.2f}s: {e}"
            self.logger.error(error_msg)
            return False, None, error_msg

    def close(self):
        """Close HTTP session."""
        if hasattr(self, "session"):
            self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def _get_logger(self, level: int = logging.WARNING) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = False

        return logger
