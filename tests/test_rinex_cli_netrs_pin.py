"""NetRS auto-pin in the manual `receivers rinex` path (_create_rinex_converter).

NetRS L2 is codeless; RINEX 3 codes the L2 range as C2D, which GAMIT deletes
("no P2 range"). The manual conversion command must pin NetRS to RINEX 2.11
unless the user explicitly passes --version, mirroring the scheduler dispatch.
"""

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from receivers.cli.main import _create_rinex_converter
from receivers.rinex import NamingConvention, RinexVersion

_NETRS_CONFIG = {"receiver": {"type": "NetRS"}}


def _args(rinex_version):
    """Minimal args namespace for _create_rinex_converter."""
    return Namespace(
        rinex_version=rinex_version,
        native_trimble=False,
        no_header_correction=False,
        keep_intermediate=False,
        session="15s_24hr",
        dry_run=True,  # skip validate_tools
        loglevel=20,
    )


@pytest.mark.unit
class TestNetrsManualPin:
    @patch("receivers.rinex.TrimbleConverter")
    @patch("receivers.cli.main.get_station_config", return_value=_NETRS_CONFIG)
    def test_netrs_pinned_to_rinex2_without_explicit_version(self, _cfg, mock_trimble):
        """No --version → NetRS pinned to RINEX 2.11 + SHORT, even though the
        incoming (config-default) version is 3 and naming is LONG."""
        mock_trimble.return_value = MagicMock()
        _create_rinex_converter(
            "FEDG",
            _args(rinex_version=None),
            RinexVersion.RINEX_3,  # config default handed in by cmd_rinex
            None,
            None,
            NamingConvention.LONG,
            None,
            MagicMock(),
            rinex_config={"netrs_rinex_version": 2},
        )
        _, kwargs = mock_trimble.call_args
        assert kwargs["rinex_version"] == RinexVersion.RINEX_2
        assert kwargs["naming_convention"] == NamingConvention.SHORT

    @patch("receivers.rinex.TrimbleConverter")
    @patch("receivers.cli.main.get_station_config", return_value=_NETRS_CONFIG)
    def test_explicit_version3_overrides_pin(self, _cfg, mock_trimble):
        """An explicit --version 3 still produces RINEX 3 for NetRS."""
        mock_trimble.return_value = MagicMock()
        _create_rinex_converter(
            "FEDG",
            _args(rinex_version=3),
            RinexVersion.RINEX_3,
            None,
            None,
            NamingConvention.LONG,
            None,
            MagicMock(),
            rinex_config={"netrs_rinex_version": 2},
        )
        _, kwargs = mock_trimble.call_args
        assert kwargs["rinex_version"] == RinexVersion.RINEX_3
