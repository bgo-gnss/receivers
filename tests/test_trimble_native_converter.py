"""Unit tests for TrimbleNativeConverter epoch line normalization.

Tests the _normalize_epoch_lines() method which fixes clock offset column
alignment in Trimble native RINEX output so rnx2crx (Hatanaka) succeeds.
"""

import logging
import pytest

from receivers.rinex.trimble_native_converter import TrimbleNativeConverter


@pytest.fixture
def converter():
    """Create a TrimbleNativeConverter without needing Docker."""
    conv = TrimbleNativeConverter.__new__(TrimbleNativeConverter)
    conv.logger = logging.getLogger("test.trimble_native")
    return conv


# --- Epoch lines as produced by trm2rinex Docker ---
# Misaligned: 13 spaces + 14-char number (63 chars total)
MISALIGNED_EPOCH = (
    "> 2026 03 13 00 40  0.0000000  0 34"
    "            -0.000000002000"
)
MISALIGNED_POSITIVE = (
    "> 2026 03 13 01 00 30.0000000  0 34"
    "             0.000000002000"
)

# Correct: 6 spaces + F15.12 (56 chars total)
CORRECT_NEGATIVE = (
    "> 2026 03 13 00 40  0.0000000  0 34"
    "      -0.000000002000"
)
CORRECT_POSITIVE = (
    "> 2026 03 13 01 00 30.0000000  0 34"
    "       0.000000002000"
)

# Epoch line without clock offset (zero offset omitted by Trimble)
NO_OFFSET_EPOCH = "> 2026 03 13 00 00  0.0000000  0 33      "

# Observation data line (should never be touched)
OBS_LINE = "G01  23456789.012 7  23456789.012 7  23456789.012 7"


@pytest.mark.unit
class TestNormalizeEpochLines:
    """Test _normalize_epoch_lines() fixes Trimble clock offset alignment."""

    def test_fixes_negative_clock_offset(self, converter, tmp_path):
        rinex = tmp_path / "TEST.26o"
        rinex.write_text(MISALIGNED_EPOCH + "\n" + OBS_LINE + "\n")

        converter._normalize_epoch_lines(rinex)

        lines = rinex.read_text().split("\n")
        assert lines[0] == CORRECT_NEGATIVE

    def test_fixes_positive_clock_offset(self, converter, tmp_path):
        rinex = tmp_path / "TEST.26o"
        rinex.write_text(MISALIGNED_POSITIVE + "\n" + OBS_LINE + "\n")

        converter._normalize_epoch_lines(rinex)

        lines = rinex.read_text().split("\n")
        assert lines[0] == CORRECT_POSITIVE

    def test_leaves_no_offset_epochs_untouched(self, converter, tmp_path):
        rinex = tmp_path / "TEST.26o"
        content = NO_OFFSET_EPOCH + "\n" + OBS_LINE + "\n"
        rinex.write_text(content)

        converter._normalize_epoch_lines(rinex)

        assert rinex.read_text() == content

    def test_leaves_observation_lines_untouched(self, converter, tmp_path):
        rinex = tmp_path / "TEST.26o"
        content = NO_OFFSET_EPOCH + "\n" + OBS_LINE + "\n"
        rinex.write_text(content)

        converter._normalize_epoch_lines(rinex)

        lines = rinex.read_text().split("\n")
        assert lines[1] == OBS_LINE

    def test_fixes_mixed_file(self, converter, tmp_path):
        """File with both offset and no-offset epoch lines."""
        rinex = tmp_path / "TEST.26o"
        content = "\n".join([
            NO_OFFSET_EPOCH,
            OBS_LINE,
            MISALIGNED_EPOCH,
            OBS_LINE,
            MISALIGNED_POSITIVE,
            OBS_LINE,
            NO_OFFSET_EPOCH,
            OBS_LINE,
            "",
        ])
        rinex.write_text(content)

        converter._normalize_epoch_lines(rinex)

        lines = rinex.read_text().split("\n")
        assert lines[0] == NO_OFFSET_EPOCH  # unchanged
        assert lines[2] == CORRECT_NEGATIVE  # fixed
        assert lines[4] == CORRECT_POSITIVE  # fixed
        assert lines[6] == NO_OFFSET_EPOCH  # unchanged

    def test_already_correct_file_unchanged_content(self, converter, tmp_path):
        """Already-correct file should produce identical content."""
        rinex = tmp_path / "TEST.26o"
        content = CORRECT_NEGATIVE + "\n" + OBS_LINE + "\n"
        rinex.write_text(content)

        converter._normalize_epoch_lines(rinex)

        # Content should be identical (even if file was rewritten)
        assert rinex.read_text() == content

    def test_clock_offset_field_width(self, converter, tmp_path):
        """Verify the fixed clock offset is exactly 21 chars (6X + F15.12)."""
        rinex = tmp_path / "TEST.26o"
        rinex.write_text(MISALIGNED_EPOCH + "\n")

        converter._normalize_epoch_lines(rinex)

        line = rinex.read_text().strip()
        # Prefix is 35 chars, clock offset field should be 21 chars
        prefix_len = 35
        offset_field = line[prefix_len:]
        assert len(offset_field) == 21, f"Expected 21 chars, got {len(offset_field)}: [{offset_field}]"

    def test_handles_missing_file_gracefully(self, converter, tmp_path):
        """Should log warning, not raise, on missing file."""
        rinex = tmp_path / "NONEXISTENT.26o"
        # Should not raise
        converter._normalize_epoch_lines(rinex)

    def test_preserves_header(self, converter, tmp_path):
        """RINEX header lines should be completely untouched."""
        header = (
            "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
            "trm2rinex           Trimble             20260313            PGM / RUN BY / DATE\n"
            "                                                            END OF HEADER\n"
        )
        rinex = tmp_path / "TEST.26o"
        rinex.write_text(header + MISALIGNED_EPOCH + "\n" + OBS_LINE + "\n")

        converter._normalize_epoch_lines(rinex)

        content = rinex.read_text()
        assert content.startswith(header)
