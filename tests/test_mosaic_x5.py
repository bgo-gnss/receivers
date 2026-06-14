"""Tests for mosaic-X5 receiver support (PolaRX5-compatible subclass)."""

import logging

from receivers.base.receiver_factory import get_receiver_factory
from receivers.config.receiver_registry import get_capability
from receivers.septentrio.mosaic_x5 import RECEIVER_TYPE, MosaicX5
from receivers.septentrio.polarx5 import PolaRX5


def _info(**receiver_extra):
    info = {"router": {"ip": "10.4.3.28"}, "receiver": {"ftpport": "2160"}}
    info["receiver"].update(receiver_extra)
    return info


class TestMosaicX5Registration:
    def test_factory_creates_mosaic(self):
        """Factory maps the 'mosaic-X5' type string to the MosaicX5 class."""
        factory = get_receiver_factory()
        assert factory.is_supported("mosaic-X5")
        receiver = factory.create_receiver_from_type("mosaic-X5", "GONH", _info())
        assert isinstance(receiver, MosaicX5)
        assert isinstance(receiver, PolaRX5)  # reuses PolaRX5 mechanics

    def test_registry_capability_is_sbf(self):
        """mosaic-X5 shares PolaRX5 capability (SBF + SBF converter)."""
        cap = get_capability("mosaic-X5")
        assert cap is not None
        assert cap.raw_extension == ".sbf.gz"
        assert cap.rinex_converter == "rinex.sbf_converter.SBFConverter"


class TestMosaicX5Identity:
    def test_get_receiver_type(self):
        r = MosaicX5("GONH", _info())
        assert r.get_receiver_type() == RECEIVER_TYPE == "mosaic-X5"

    def test_station_info_reports_mosaic_identity(self):
        """get_station_info() must report mosaic-X5, not the inherited PolaRX5."""
        r = MosaicX5("GONH", _info())
        assert r.get_station_info()["receiver_type"] == "mosaic-X5"

    def test_polarx5_identity_unchanged(self):
        """The literal->get_receiver_type() refactor keeps PolaRX5 reporting itself."""
        r = PolaRX5(
            "REYK", {"router": {"ip": "10.4.1.100"}, "receiver": {"ftpport": "21"}}
        )
        assert r.get_station_info()["receiver_type"] == "PolaRX5"


class TestMosaicX5Layout:
    def test_default_layout_matches_polarx5(self):
        """With no overrides, the remote template is the standard fleet layout."""
        r = MosaicX5("GONH", _info())
        tmpl = r._build_remote_template("15s_24hr", ".gz")
        assert "#Rin2" in tmpl
        assert "GRB0051" not in tmpl

    def test_nonstandard_layout_override(self):
        """Override keys produce a custom session dir + filename pattern (GONH case)."""
        r = MosaicX5(
            "GONH",
            _info(
                remote_session_dir="GRB0051",
                remote_filename_pattern="{marker_lc}%j0.%y_.A",
                remote_sessions="15s_24hr",
            ),
        )
        tmpl = r._build_remote_template("15s_24hr", ".gz")
        assert tmpl == "/DSK1/SSN/GRB0051/%y%j/gonh%j0.%y_.A"

    def test_cfg_override_lookup_sections(self):
        """Override keys are found under receiver / station / top-level."""
        assert MosaicX5._cfg_override({"receiver": {"k": "v"}}, "k") == "v"
        assert MosaicX5._cfg_override({"station": {"k": "v"}}, "k") == "v"
        assert MosaicX5._cfg_override({"k": "v"}, "k") == "v"
        assert MosaicX5._cfg_override({"receiver": {}}, "missing") is None


class TestMosaicX5SessionGate:
    """The CLI download capability gate must be fail-open for receiver types
    with no ``[<type>]`` section in receivers.cfg.

    mosaic-X5 has no such section — it reuses the PolaRX5 ``session_map`` at the
    receiver level and declares the sessions it actually logs per-station via
    ``remote_sessions``. The gate must therefore pass it through rather than
    reject it (which previously produced "supported: none" and skipped every
    download). This mirrors the scheduler's fail-open gate
    (``bulk_scheduler._get_stations_for_session``).
    """

    def test_mosaic_has_no_declared_sessions(self):
        from receivers.config.receivers_config import get_receivers_config

        # No [mosaic-x5] section ⇒ empty list ⇒ gate falls open.
        assert get_receivers_config().get_supported_sessions("mosaic-x5") == []

    def test_polarx5_still_declares_sessions(self):
        from receivers.config.receivers_config import get_receivers_config

        supported = get_receivers_config().get_supported_sessions("polarx5")
        assert "15s_24hr" in supported

    def test_gate_passes_mosaic_for_every_session(self):
        """_validate_station_for_download returns a receiver (no skip) for a
        mosaic-X5 station regardless of session."""
        import importlib
        from unittest.mock import patch

        # receivers.cli re-exports the ``main`` function, shadowing the submodule
        # attribute — import the module object explicitly.
        cli_main = importlib.import_module("receivers.cli.main")

        station_cfg = {
            "router": {"ip": "10.4.3.28"},
            "receiver": {"ftpport": "2160"},
            "receiver_type": "mosaic-X5",
        }
        sentinel = object()
        for session in ("15s_24hr", "1Hz_1hr", "status_1hr"):
            with (
                patch.object(cli_main, "get_station_config", return_value=station_cfg),
                patch.object(cli_main, "create_receiver", return_value=sentinel),
            ):
                result = cli_main._validate_station_for_download(
                    "GONH", logging.getLogger("test"), session=session
                )
            assert result is sentinel, f"mosaic-X5 wrongly skipped for {session}"

    def test_gate_still_skips_known_type_unsupported_session(self):
        """A known type (declares a session_map) is still skipped for a session
        it does not support — fail-open must not become fail-never."""
        import importlib
        from unittest.mock import patch

        # receivers.cli re-exports the ``main`` function, shadowing the submodule
        # attribute — import the module object explicitly.
        cli_main = importlib.import_module("receivers.cli.main")

        station_cfg = {
            "router": {"ip": "10.4.1.100"},
            "receiver": {"ftpport": "21"},
            "receiver_type": "polarx5",
        }
        with (
            patch.object(cli_main, "get_station_config", return_value=station_cfg),
            patch.object(cli_main, "create_receiver", return_value=object()),
        ):
            result = cli_main._validate_station_for_download(
                "REYK", logging.getLogger("test"), session="nonexistent_session"
            )
        assert result is None
