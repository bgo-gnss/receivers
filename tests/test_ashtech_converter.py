"""AshtechConverter: content-dispatched teqc decode for pre-2012 .atc raw."""

from pathlib import Path

import pytest

from receivers.rinex.ashtech_converter import AshtechConverter
from receivers.rinex.converter_base import ConversionError


def _conv():
    c = AshtechConverter.__new__(AshtechConverter)  # skip heavy __init__
    import logging

    c.station_id = "RHOF"
    c.logger = logging.getLogger("test.ashtech")
    return c


class TestFlagDispatch:
    def test_u_file(self, tmp_path):
        f = tmp_path / "RHOF201004020000a.atc"
        f.write_bytes(b"\x00\x00\x00\x30BHDRVersion: UZ-12" + b"\x00" * 60)
        assert _conv()._ashtech_flag(f) == "u"

    def test_r_file(self, tmp_path):
        f = tmp_path / "SKRO201006020000a.atc"
        f.write_bytes(b"Z-12\x00dump" + b"\x00" * 60)
        assert _conv()._ashtech_flag(f) == "r"

    def test_sbf_content_refused(self, tmp_path):
        f = tmp_path / "KOSK201301010000a.atc"
        f.write_bytes(b"$@Sic" + b"\x00" * 60)
        with pytest.raises(ConversionError, match="refusing to guess"):
            _conv()._ashtech_flag(f)


class TestDeclaration:
    def test_formats_and_tools(self):
        assert AshtechConverter.accepted_raw_formats == frozenset(
            {"ashtech_u", "ashtech_r"}
        )
        c = _conv()
        assert c._get_required_tools() == ["teqc"]
        assert ".atc" in c.supported_extensions

    def test_r3_request_clamped_to_native_r2(self):
        """teqc cannot make REAL RINEX 3; an R3 request must clamp to 2.11
        (R2->R3 via gfzrnx is ambiguous reformatting - bgo policy)."""
        from receivers.rinex.ashtech_converter import AshtechConverter
        from receivers.rinex.converter_base import NamingConvention, RinexVersion

        c = AshtechConverter("RHOF", rinex_version=RinexVersion.RINEX_3)
        assert c.rinex_version == RinexVersion.RINEX_2
        assert c.naming_convention == NamingConvention.SHORT
        assert c._get_required_tools() == ["teqc"]
