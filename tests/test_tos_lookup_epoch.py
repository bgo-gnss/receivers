"""TOS is asked WHEN the data was recorded, not what the filename implies.

TOS device sessions are bounded by real timestamps. A RINEX 2 daily filename
carries no time at all, so every "which hardware was here?" lookup used to
resolve to midnight — and a station whose receiver was swapped mid-day got the
header of the hardware it no longer had.

The measured case, RFEL 2026-09-08: TOS closes the NETRS period at
``2026 251 15 00 00`` and opens the PolaRX5 there; the day's data runs
19:26→23:59:45. Same code, same station, one day later (2026-09-10) produced
``SEPT POLARX5 4103742`` correctly, while 09-08 produced
``TRIMBLE NETRS 4921172756``. TOS was right; the lookup key was wrong.

What these tests pin, in order of how badly each would hurt:

* **The refined epoch is what reaches the corrector** — the whole fix. The
  filename-derived date must keep driving naming and canonicalisation, so the
  two values are deliberately NOT merged.
* **The refinement never moves the DAY.** Following a header onto a different
  date would silently re-era a misfiled file — the exact condition
  ``_verify_conversion_identity`` exists to catch and delete.
* **That gate still fires.** Widening the header reader from date to datetime
  must not turn its comparison into a tautology.
* **Every unreadable header falls back to the claim**, so a metadata
  refinement can never be the reason a conversion fails.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from receivers.rinex.converter_base import RawToRinexConverter, RawValidationError
from receivers.rinex.obs_epoch import (
    read_obs_header_identity,
    resolve_tos_lookup_epoch,
)

# TOS closes RFEL's NETRS period here and opens the PolaRX5.
SWAP = datetime(2026, 9, 8, 15, 0, 0)
CLAIMED = datetime(2026, 9, 8)  # what RFEL2510.26D's name resolves to


def _header(first_obs_line: str = "", extra: str = "") -> str:
    return (
        "     2.11           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
        "RFEL                                                        MARKER NAME         \n"
        "  2523573.9163  -863213.4429  5725718.0000                  APPROX POSITION XYZ \n"
        f"{first_obs_line}"
        f"{extra}"
        "                                                            END OF HEADER       \n"
    )


def _obs(y, m, d, hh=None, mm=None, ss=None) -> str:
    body = f"{y:6d}{m:6d}{d:6d}"
    if hh is not None:
        body += f"{hh:6d}{mm:6d}{ss:13.7f}     GPS"
    return f"{body:<60}TIME OF FIRST OBS   \n"


def _write(tmp_path, line, name="RFEL2510.26o") -> Path:
    p = tmp_path / name
    p.write_text(_header(line), encoding="latin-1")
    return p


class TestTheRfelCase:
    """The changeover day that produced a NETRS header for PolaRX5 data."""

    def test_the_lookup_lands_after_the_swap(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 8, 19, 26, 45.0))
        assert resolve_tos_lookup_epoch(f, CLAIMED) > SWAP

    def test_the_filename_midnight_would_have_landed_before_it(self):
        """Without the fix the key is midnight, which is in the OLD era.

        Pinning this keeps the test honest: if the boundary or the claimed
        value ever stops straddling the swap, the test above proves nothing.
        """
        assert CLAIMED < SWAP

    def test_the_full_epoch_is_carried_not_just_the_hour(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 8, 19, 26, 45.0))
        assert resolve_tos_lookup_epoch(f, CLAIMED) == datetime(2026, 9, 8, 19, 26, 45)


class TestTheDayIsNeverMoved:
    """A different date is misfiled raw — the identity gate's business, not a
    metadata lookup's. Following the header there would hide the defect."""

    def test_a_header_on_another_day_is_ignored(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 10, 19, 26, 45.0))
        assert resolve_tos_lookup_epoch(f, CLAIMED) == CLAIMED

    def test_even_one_day_earlier_is_ignored(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 7, 23, 59, 45.0))
        assert resolve_tos_lookup_epoch(f, CLAIMED) == CLAIMED


class TestFallsBackToTheClaim:
    def test_no_time_of_first_obs_record(self, tmp_path):
        assert resolve_tos_lookup_epoch(_write(tmp_path, ""), CLAIMED) == CLAIMED

    def test_unparseable_epoch(self, tmp_path):
        bad = f"{'  abcd    xx    yy':<60}TIME OF FIRST OBS   \n"
        assert resolve_tos_lookup_epoch(_write(tmp_path, bad), CLAIMED) == CLAIMED

    def test_a_date_only_record_yields_midnight_unchanged(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 8))
        assert resolve_tos_lookup_epoch(f, CLAIMED) == CLAIMED

    def test_a_missing_file_does_not_raise(self, tmp_path):
        assert resolve_tos_lookup_epoch(tmp_path / "nope.26o", CLAIMED) == CLAIMED

    def test_no_claim_stays_none(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 8, 19, 26, 45.0))
        assert resolve_tos_lookup_epoch(f, None) is None


class TestHeaderParsing:
    def test_leap_second_seconds_do_not_raise(self, tmp_path):
        """``59.9999999`` is a legal header value.

        Handing it to ``datetime(second=...)`` raises "second must be in
        0..59"; carrying it as a timedelta rolls cleanly into the next minute
        instead. Either answer is fine for a TOS lookup — raising is not.
        """
        f = _write(tmp_path, _obs(2026, 9, 8, 19, 26, 59.9999999))
        assert resolve_tos_lookup_epoch(f, CLAIMED) == datetime(2026, 9, 8, 19, 27)

    def test_the_other_identity_fields_still_parse(self, tmp_path):
        f = _write(tmp_path, _obs(2026, 9, 8, 19, 26, 45.0))
        first_obs, xyz, marker = read_obs_header_identity(f)
        assert first_obs == datetime(2026, 9, 8, 19, 26, 45)
        assert xyz is not None and abs(xyz[0] - 2523573.9163) < 1e-3
        assert marker == "RFEL"

    def test_the_body_is_not_read(self, tmp_path):
        """A record past END OF HEADER must never be mistaken for the header's."""
        p = tmp_path / "RFEL2510.26o"
        p.write_text(
            _header(_obs(2026, 9, 8, 19, 26, 45.0)) + _obs(2026, 1, 1, 0, 0, 0.0),
            encoding="latin-1",
        )
        assert resolve_tos_lookup_epoch(p, CLAIMED) == datetime(2026, 9, 8, 19, 26, 45)


class _Stub(RawToRinexConverter):
    """Concrete RawToRinexConverter with the external tooling removed."""

    @property
    def supported_extensions(self):
        return [".sbf"]

    @property
    def converter_name(self):
        return "stub"

    def _run_conversion(self, raw_path, output_path, observation_date):
        return self._produced

    def _get_required_tools(self):
        return []


class _StubConfig:
    """Only the one lookup the identity gate makes."""

    @staticmethod
    def get_rinex_config():
        return {}


@pytest.fixture
def converter(tmp_path):
    obj = object.__new__(_Stub)
    obj.station_id = "RFEL"
    obj.logger = logging.getLogger("test.converter")
    obj.apply_header_corrections = True
    obj.corrections_seen = []
    obj.named_with = []

    produced = tmp_path / "RFEL251a.26o"
    produced.write_text(_header(_obs(2026, 9, 8, 19, 26, 45.0)), encoding="latin-1")
    obj._produced = produced

    obj._raw_validation_enabled = lambda: False
    obj.config = _StubConfig()

    def _corrections(rinex_file, observation_date):
        obj.corrections_seen.append(observation_date)
        return 1

    def _named(rinex_file, observation_date):
        obj.named_with.append(observation_date)
        return rinex_file

    obj._apply_header_corrections = _corrections
    obj._canonicalize_rinex = lambda f, d: f
    obj._rename_to_convention = _named
    obj._apply_compression = lambda f: f
    obj._apply_naming_gated_records = lambda *a, **k: None
    return obj


class TestTheConverterUsesIt:
    """The integration guard: the two dates must stay separate all the way
    through. Merging them would break naming; not refining leaves the bug."""

    def _convert(self, converter, tmp_path):
        raw = tmp_path / "RFEL202609080000a.sbf"
        raw.write_bytes(b"\x24\x40stub")
        return converter.convert_file(raw, output_dir=tmp_path)

    def test_tos_is_asked_with_the_epoch_after_the_swap(self, converter, tmp_path):
        self._convert(converter, tmp_path)
        assert converter.corrections_seen == [datetime(2026, 9, 8, 19, 26, 45)]
        assert converter.corrections_seen[0] > SWAP

    def test_naming_still_uses_the_filename_date(self, converter, tmp_path):
        """Naming must NOT follow the epoch — an hour in the name would
        rewrite a daily file to an hourly one."""
        self._convert(converter, tmp_path)
        assert converter.named_with == [CLAIMED]


class TestTheIdentityGateStillFires:
    """Widening the reader to a datetime must not make its comparison always
    true. A misfiled raw still has to be refused and its output deleted."""

    def _gate(self, converter, tmp_path, first_obs, claimed=CLAIMED):
        converter._raw_validation_enabled = lambda: True
        converter._expected_station_xyz = lambda: None
        out = tmp_path / "out.26o"
        out.write_text(_header(first_obs), encoding="latin-1")
        converter._verify_conversion_identity(out, claimed)
        return out

    def test_a_wrong_day_is_refused(self, converter, tmp_path):
        with pytest.raises(RawValidationError) as e:
            self._gate(converter, tmp_path, _obs(2026, 9, 10, 19, 26, 45.0))
        assert e.value.category == "wrong-date"

    def test_the_wrong_output_is_deleted(self, converter, tmp_path):
        out = tmp_path / "out.26o"
        with pytest.raises(RawValidationError):
            out = self._gate(converter, tmp_path, _obs(2026, 9, 10, 19, 26, 45.0))
        assert not (tmp_path / "out.26o").exists()

    def test_the_message_reports_a_date_not_a_timestamp(self, converter, tmp_path):
        """Operators diff these lines; a sudden ``00:00:00`` is noise."""
        with pytest.raises(RawValidationError) as e:
            self._gate(converter, tmp_path, _obs(2026, 9, 10, 19, 26, 45.0))
        assert "starts 2026-09-10 but" in str(e.value)

    def test_a_same_day_afternoon_epoch_passes(self, converter, tmp_path):
        """The RFEL file itself: right day, late start. Must NOT be refused."""
        self._gate(converter, tmp_path, _obs(2026, 9, 8, 19, 26, 45.0))
