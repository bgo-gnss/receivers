"""Raw-content validation gates in the RINEX conversion path.

The three checks from the .atc findings (vault todo #56), wired into
BaseRinexConverter.convert_file: magic-byte format gate, decoded-date vs
claimed-date, and the post-conversion identity gate (first-obs date +
raw-derived APPROX POSITION vs the station's surveyed coordinates).
"""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.rinex.converter_base import (
    ConversionError,
    RawToRinexConverter,
)


def _write_rinex(path: Path, *, first_obs=(2010, 4, 2), xyz=None, marker="RHOF"):
    xyz = xyz or (2456174.12, -701824.79, 5824755.54)  # RHOF-ish
    y, mo, d = first_obs
    tofo = f"{y:6d}{mo:6d}{d:6d}{0:6d}{0:6d}{0.0:13.7f}     GPS"
    lines = [
        "     2.11           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE",
        f"{marker:<60}MARKER NAME",
        f"{xyz[0]:14.4f}{xyz[1]:14.4f}{xyz[2]:14.4f}{'':18}APPROX POSITION XYZ",
        f"{tofo:<60}TIME OF FIRST OBS",
        f"{'':60}END OF HEADER",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return path


class _FakeConverter(RawToRinexConverter):
    """Minimal concrete converter: 'conversion' just returns a prepared file."""

    accepted_raw_formats = frozenset({"sbf"})

    def __init__(self, station="RHOF", output: Path = None):  # type: ignore[assignment]
        # Skip heavy base __init__; set only what the gates use.
        import logging

        self.station_id = station
        self.logger = logging.getLogger("test.fakeconv")
        self._output = output
        self._rinex_cfg = {}

        outer = self

        class _Cfg:
            def get_rinex_config(self):
                return outer._rinex_cfg

        self.config = _Cfg()

    @property
    def supported_extensions(self):
        return [".sbf"]

    @property
    def converter_name(self):
        return "fake2rin"

    def _get_required_tools(self):
        return []

    def _run_conversion(self, raw_file, output_dir, observation_date):
        return self._output


RHOF_XYZ = (2456174.12, -701824.79, 5824755.54)


class TestFormatGate:
    def test_wrong_positive_format_refused(self, tmp_path):
        raw = tmp_path / "KOSK201301010000a.sbf"
        raw.write_bytes(b"\x00\x00\x00\x30BHDRVersion" + b"\x00" * 200)  # ashtech_u
        c = _FakeConverter()
        with pytest.raises(ConversionError, match="ashtech_u"):
            c._validate_raw_content(raw, datetime(2013, 1, 1))

    def test_matching_format_passes(self, tmp_path):
        raw = tmp_path / "KOSK201301010000a.sbf"
        raw.write_bytes(b"$@Sic" + b"\x00" * 200)
        c = _FakeConverter()
        with patch("receivers.archive.raw_format.decoded_span", return_value=None):
            c._validate_raw_content(raw, datetime(2013, 1, 1))  # no raise

    def test_unknown_format_passes(self, tmp_path):
        raw = tmp_path / "X.sbf"
        raw.write_bytes(b"\x00" * 64)
        _FakeConverter()._validate_raw_content(raw, datetime(2013, 1, 1))

    def test_gate_disabled_by_config(self, tmp_path):
        raw = tmp_path / "K.sbf"
        raw.write_bytes(b"\x00\x00\x00\x30BHDR" + b"\x00" * 60)
        c = _FakeConverter()
        c._rinex_cfg = {"raw_validation": "false"}
        c._validate_raw_content(raw, datetime(2013, 1, 1))  # no raise


class TestDecodedDateGate:
    """Date validation lives in the POST-conversion identity gate (free there;
    a pre-decode teqc pass would double I/O on the hot reconciler path).
    Here: the pre-gate only checks format and never calls decoded_span."""

    def test_pre_gate_does_not_decode_dates(self, tmp_path):
        raw = tmp_path / "RHOF201004020000a.sbf"
        raw.write_bytes(b"$@Sic" + b"\x00" * 200)
        c = _FakeConverter()
        with patch(
            "receivers.archive.raw_format.decoded_span",
            side_effect=AssertionError("must not be called on the hot path"),
        ):
            c._validate_raw_content(raw, datetime(2000, 1, 1))  # no raise


class TestIdentityGate:
    def _conv(self, out):
        c = _FakeConverter(output=out)
        return c

    def test_wrong_first_obs_date_deletes_output(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o")
        c = self._conv(out)
        with patch.object(c, "_expected_station_xyz", return_value=RHOF_XYZ):
            with pytest.raises(ConversionError, match="misfiled") as ei:
                c._verify_conversion_identity(out, datetime(2000, 4, 1))
        assert not out.exists()
        assert ei.value.category == "wrong-date"
        assert "archive-sort" in ei.value.suggestion

    def test_wrong_station_position_deletes_output(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o")
        c = self._conv(out)
        reyk = (2587384.0, -1043033.0, 5716564.0)  # ~hundreds of km away
        with patch.object(c, "_expected_station_xyz", return_value=reyk):
            with pytest.raises(ConversionError, match="NOT this station"):
                c._verify_conversion_identity(out, datetime(2010, 4, 2))
        assert not out.exists()

    def test_matching_identity_passes(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o")
        c = self._conv(out)
        near = (RHOF_XYZ[0] + 5, RHOF_XYZ[1] - 3, RHOF_XYZ[2] + 4)  # ~7 m off
        with patch.object(c, "_expected_station_xyz", return_value=near):
            c._verify_conversion_identity(out, datetime(2010, 4, 2))
        assert out.exists()

    def test_zero_position_skips_position_check(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o", xyz=(0.0, 0.0, 0.0))
        c = self._conv(out)
        with patch.object(c, "_expected_station_xyz", return_value=RHOF_XYZ):
            c._verify_conversion_identity(out, datetime(2010, 4, 2))
        assert out.exists()

    def test_no_expected_coords_fails_open(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o")
        c = self._conv(out)
        with patch.object(c, "_expected_station_xyz", return_value=None):
            c._verify_conversion_identity(out, datetime(2010, 4, 2))
        assert out.exists()

    def test_header_reader(self, tmp_path):
        out = _write_rinex(tmp_path / "RHOF0920.10o", marker="RHOF RAUFARHOFN")
        first, xyz, marker = RawToRinexConverter._read_identity_header(out)
        assert first == date(2010, 4, 2)
        assert xyz is not None and abs(xyz[0] - RHOF_XYZ[0]) < 0.01
        assert marker == "RHOF RAUFARHOFN"


class TestSubclassGates:
    def test_declared_formats(self):
        from receivers.rinex.sbf_converter import SBFConverter
        from receivers.rinex.trimble_converter import TrimbleConverter
        from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

        assert SBFConverter.accepted_raw_formats == frozenset({"sbf"})
        assert TrimbleConverter.accepted_raw_formats == frozenset({"trimble"})
        assert TrimbleNativeConverter.accepted_raw_formats == frozenset({"trimble"})


class TestValidationEpilog:
    def test_epilog_groups_and_suggests(self):
        from receivers.rinex.converter_base import (
            ConversionResult,
            validation_epilog,
        )

        results = [
            ConversionResult(raw_file=Path("A.sbf"), success=True),
            ConversionResult(
                raw_file=Path("RHOF200004010000a.atc"),
                message="converted output starts 2010-04-02 ...",
                validation_category="wrong-date",
                validation_suggestion="relocate with 'receivers archive-sort ...'",
            ),
            ConversionResult(
                raw_file=Path("KOSK201301010000a.atc"),
                message="raw content is 'sbf' ...",
                validation_category="wrong-format",
                validation_suggestion="decode with sbf2rin ...",
            ),
        ]
        text = validation_epilog(results)
        assert text is not None
        assert "refused 2 file(s)" in text
        assert "[wrong-format] 1" in text and "[wrong-date] 1" in text
        assert "archive-sort" in text and "sbf2rin" in text

    def test_epilog_none_when_clean(self):
        from receivers.rinex.converter_base import (
            ConversionResult,
            validation_epilog,
        )

        assert (
            validation_epilog([ConversionResult(raw_file=Path("A"), success=True)])
            is None
        )

    def test_convert_batch_logs_epilog(self, tmp_path, caplog):
        import logging as _logging

        raw = tmp_path / "KOSK201301010000a.sbf"
        raw.write_bytes(b"\x00\x00\x00\x30BHDR" + b"\x00" * 200)  # ashtech in .sbf
        c = _FakeConverter()
        with caplog.at_level(_logging.WARNING):
            batch = c.convert_batch([raw])
        assert batch.failed == 1
        assert batch.results[0].validation_category == "wrong-format"
        assert any("refused 1 file(s)" in r.message for r in caplog.records)
        # mid-run line is the COMPACT one
        assert any(
            "raw-validation refused" in r.message and "\n" not in r.message
            for r in caplog.records
        )
