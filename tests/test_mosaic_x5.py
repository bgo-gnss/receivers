"""Tests for mosaic-X5 receiver support (PolaRX5-compatible subclass)."""

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
        r = PolaRX5("REYK", {"router": {"ip": "10.4.1.100"}, "receiver": {"ftpport": "21"}})
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
