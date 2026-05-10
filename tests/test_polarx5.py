"""Tests for PolaRX5 receiver implementation."""

from unittest.mock import Mock, patch

import pytest

from receivers.base.exceptions import ConfigurationError
from receivers.septentrio.polarx5 import PolaRX5


class TestPolaRX5:
    """Test cases for PolaRX5 receiver class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.station_info = {
            "router": {"ip": "10.4.1.100"},
            "receiver": {"ftpport": "21"},
        }
        self.receiver = PolaRX5("REYK", self.station_info)

    def test_init(self):
        """Test receiver initialization."""
        assert self.receiver.station_id == "REYK"
        assert self.receiver.ip_number == "10.4.1.100"
        assert self.receiver.ip_port == 21
        assert not self.receiver.pasv  # 10.4.1.x should use non-passive

    def test_init_missing_config(self):
        """Test initialization with missing configuration."""
        bad_config = {"router": {"ip": "10.4.1.100"}}  # Missing receiver info

        with pytest.raises(ConfigurationError):
            PolaRX5("REYK", bad_config)

    def test_get_station_info(self):
        """Test getting station information."""
        info = self.receiver.get_station_info()

        assert info["station_id"] == "REYK"
        assert info["receiver_type"] == "PolaRX5"
        assert info["ip"] == "10.4.1.100"
        assert info["port"] == 21

    @patch("socket.socket")
    @patch("subprocess.run")
    def test_connection_status_success(self, mock_run, mock_socket):
        """Test successful connection status check."""
        # Mock successful ping
        mock_run.return_value = Mock(returncode=0)
        # Mock successful HTTP port check
        mock_sock_instance = Mock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket.return_value = mock_sock_instance

        status = self.receiver.get_connection_status()

        assert status["router"] is True
        assert status["receiver"] is True
        assert status["ip"] == "10.4.1.100"
        assert status["http_port"] == 8060
        assert status["error"] is None

    @patch("socket.socket")
    @patch("subprocess.run")
    def test_connection_status_failure(self, mock_run, mock_socket):
        """Test failed connection status check."""
        # Mock failed ping
        mock_run.return_value = Mock(returncode=1)
        # Mock failed HTTP port check
        mock_sock_instance = Mock()
        mock_sock_instance.connect_ex.return_value = 1
        mock_socket.return_value = mock_sock_instance

        status = self.receiver.get_connection_status()

        assert status["router"] is False
        assert status["receiver"] is False
        assert status["error"] is not None

    def test_get_health_status(self):
        """Test health status reporting."""
        with patch.object(self.receiver, "get_connection_status") as mock_conn:
            mock_conn.return_value = {"receiver": True, "router": True}

            health = self.receiver.get_health_status()

            assert health["station_id"] == "REYK"
            assert health["receiver_type"] == "PolaRX5"
            assert health["overall_status"] == "healthy"

    def test_is_gz_file(self):
        """Test gzip file detection."""
        # Test with non-existent file
        assert not PolaRX5.is_gz_file("/nonexistent/file")

    def test_str_repr(self):
        """Test string representations."""
        assert str(self.receiver) == "PolaRX5(REYK)"
        assert repr(self.receiver) == "PolaRX5(station_id='REYK')"


class TestPolaRX5FtpModeFromStationConfig:
    """Verify _setup_connection_info reads ftp_mode from the same dict where
    config_utils.get_station_config writes it (router.ftp_mode), so the
    cfg_discrepancy override actually reaches self.pasv.
    """

    def _make(self, ftp_mode_value=None):
        info = {
            "router": {"ip": "10.4.1.100"},
            "receiver": {"ftpport": "21"},
        }
        if ftp_mode_value is not None:
            info["router"]["ftp_mode"] = ftp_mode_value
        return PolaRX5("TEST", info)

    def test_router_ftp_mode_active_sets_pasv_false(self):
        """router.ftp_mode='active' (the override target) → self.pasv = False."""
        r = self._make("active")
        assert r.pasv is False

    def test_router_ftp_mode_passive_sets_pasv_true(self):
        """router.ftp_mode='passive' → self.pasv = True."""
        r = self._make("passive")
        assert r.pasv is True

    def test_router_ftp_mode_auto_defaults_to_passive(self):
        """router.ftp_mode='auto' → defaults to passive (NAT-friendly)."""
        r = self._make("auto")
        assert r.pasv is True

    def test_router_ftp_mode_missing_defaults_to_passive(self):
        """No ftp_mode anywhere → defaults to passive (NAT-friendly)."""
        r = self._make(None)
        assert r.pasv is True

    def test_receiver_ftp_mode_is_ignored(self):
        """Regression guard: ftp_mode under 'receiver' must NOT be read.

        config_utils.get_station_config writes the cfg_discrepancy override
        to router.ftp_mode (line 159), not receiver.ftp_mode. Reading from
        receiver.ftp_mode silently ignored the override on every run.
        """
        info = {
            "router": {"ip": "10.4.1.100"},  # no ftp_mode → defaults
            "receiver": {"ftpport": "21", "ftp_mode": "active"},  # decoy
        }
        r = PolaRX5("TEST", info)
        # ftp_mode under receiver is ignored, falls through to default passive
        assert r.pasv is True


@pytest.mark.integration
class TestPolaRX5Integration:
    """Integration tests for PolaRX5 (require actual configuration)."""

    def test_download_dry_run(self):
        """Test download in dry-run mode."""
        # This would require actual station configuration
        pass

    @pytest.mark.ftp
    def test_real_connection(self):
        """Test connection to real receiver."""
        # This would require access to actual receiver
        pass
