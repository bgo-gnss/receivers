"""`--fix-headers` and the EPOS path must ask TOS the same question the converter does.

The archive converter was fixed to key its TOS session lookup on the file's own
first-observation epoch rather than the filename's midnight (see
``receivers.rinex.obs_epoch``). These two paths were left on midnight, which is
worse than the original bug was: the converter now produces a CORRECT header for
a changeover day and ``--fix-headers`` would overwrite it with the pre-change
receiver. Whichever ran last would win.

The measured case is RFEL 2026-09-08 — TOS closes the NETRS period at
``2026 251 15 00 00`` and opens the PolaRX5 there, while the day's data starts
19:26.

What these tests pin:

* **The lookup epoch reaches TOS**, taken from the header ``--fix-headers``
  has ALREADY read. These files are ``.Z``/``.gz`` in the archive, so a fix that
  re-opened them to re-read one field would decompress every file twice.
* **The backup-deletion gate uses the same epoch.** It decides whether a
  pre-fix backup may be deleted; asking a different session than the fix used
  would make the two disagree on every changeover day and keep backups forever.
* **The day is never moved**, and every unreadable header falls back to the
  filename claim — the same rule as the converter, shared, not re-implemented.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from receivers.rinex.obs_epoch import parse_first_obs_value, refine_tos_epoch

SWAP = datetime(2026, 9, 8, 15, 0, 0)
CLAIMED = datetime(2026, 9, 8)  # what RFEL2510.26D.Z's name resolves to
HDR_VALUE = "  2026     9     8    19    26   45.0000000     GPS"


class TestTheHeaderValueParser:
    """`--fix-headers` holds the header VALUE, not a path — parse from that."""

    def test_a_full_epoch_parses(self):
        assert parse_first_obs_value(HDR_VALUE) == datetime(2026, 9, 8, 19, 26, 45)

    def test_the_parsed_epoch_is_after_the_swap(self):
        assert parse_first_obs_value(HDR_VALUE) > SWAP

    def test_a_missing_label_is_none_not_a_crash(self):
        """`rinex_info.get(...)` returns None when the label is absent."""
        assert parse_first_obs_value(None) is None

    def test_an_empty_value_is_none(self):
        assert parse_first_obs_value("") is None

    def test_a_date_only_value_parses_to_midnight(self):
        assert parse_first_obs_value("  2026 9 8") == datetime(2026, 9, 8)

    def test_junk_is_none(self):
        assert parse_first_obs_value("not a date at all") is None


class TestTheSharedRule:
    """One copy of the rule, used by the converter and by fix-headers."""

    def test_a_same_day_epoch_refines(self):
        got = refine_tos_epoch(CLAIMED, parse_first_obs_value(HDR_VALUE))
        assert got == datetime(2026, 9, 8, 19, 26, 45) and got > SWAP

    def test_a_different_day_is_ignored(self):
        other = datetime(2026, 9, 10, 19, 26, 45)
        assert refine_tos_epoch(CLAIMED, other) == CLAIMED

    def test_an_unreadable_epoch_falls_back_to_the_claim(self):
        assert refine_tos_epoch(CLAIMED, None) == CLAIMED

    def test_no_claim_stays_none(self):
        assert refine_tos_epoch(None, parse_first_obs_value(HDR_VALUE)) is None


class _Cache:
    """TOS session cache double: records the epoch it was asked with."""

    def __init__(self):
        self.asked = []

    def get_session(self, station, when):
        self.asked.append(when)
        # Mimic TOS: the session covering `when`, boundary at the swap.
        return {"era": "polarx5" if when and when >= SWAP else "netrs"}


def _header(first_obs=HDR_VALUE):
    return {
        "TIME OF FIRST OBS": first_obs,
        "MARKER NAME": "RFEL",
        "REC # / TYPE / VERS": "4103742 SEPT POLARX5 5.7.0",
    }


class TestFixHeadersAsksWithTheEpoch:
    def _run(self, monkeypatch, header, tmp_path, claimed=CLAIMED):
        import receivers.rinex.header_fix as hf

        cache = _Cache()
        monkeypatch.setattr(hf, "_read_header_info", lambda *a, **k: header)
        f = tmp_path / "RFEL2510.26D.Z"
        f.write_bytes(b"not really compressed - the reader is stubbed")
        hf.fix_headers_in_file(
            f, "RFEL", observation_date=claimed, dry_run=True, tos_cache=cache
        )
        return cache

    def test_tos_is_asked_with_the_post_swap_epoch(self, monkeypatch, tmp_path):
        cache = self._run(monkeypatch, _header(), tmp_path)
        assert cache.asked, "TOS was never consulted"
        assert cache.asked[0] == datetime(2026, 9, 8, 19, 26, 45)
        assert cache.asked[0] > SWAP, "would have selected the NETRS era"

    def test_a_header_without_the_label_falls_back_to_the_claim(
        self, monkeypatch, tmp_path
    ):
        cache = self._run(monkeypatch, _header(first_obs=None), tmp_path)
        assert cache.asked and cache.asked[0] == CLAIMED

    def test_a_foreign_day_in_the_header_does_not_move_the_lookup(
        self, monkeypatch, tmp_path
    ):
        cache = self._run(
            monkeypatch, _header("  2026 9 10 19 26 45.0000000  GPS"), tmp_path
        )
        assert cache.asked and cache.asked[0] == CLAIMED


class TestBackupGateUsesTheSameEpoch:
    """If this gate asks a different session than the fix used, a corrected file
    never verifies and its backup is kept forever."""

    def test_the_gate_asks_with_the_refined_epoch(self, monkeypatch, tmp_path):
        import receivers.rinex.header_fix as hf

        cache = _Cache()
        monkeypatch.setattr(hf, "_read_header_info", lambda *a, **k: _header())
        monkeypatch.setattr(
            "tostools.rinex.validator.compare_rinex_to_tos",
            lambda *a, **k: {"discrepancies": {}},
        )
        f = tmp_path / "RFEL2510.26D.Z"
        f.write_bytes(b"stub")
        hf.archive_header_matches_tos(f, "RFEL", CLAIMED, tos_cache=cache)
        assert cache.asked and cache.asked[0] == datetime(2026, 9, 8, 19, 26, 45)

    def test_both_paths_agree_on_the_era(self, monkeypatch, tmp_path):
        """The property that actually matters: fix and verify pick one session."""
        import receivers.rinex.header_fix as hf

        monkeypatch.setattr(hf, "_read_header_info", lambda *a, **k: _header())
        monkeypatch.setattr(
            "tostools.rinex.validator.compare_rinex_to_tos",
            lambda *a, **k: {"discrepancies": {}},
        )
        f = tmp_path / "RFEL2510.26D.Z"
        f.write_bytes(b"stub")

        fix_cache, gate_cache = _Cache(), _Cache()
        hf.fix_headers_in_file(
            f, "RFEL", observation_date=CLAIMED, dry_run=True, tos_cache=fix_cache
        )
        hf.archive_header_matches_tos(f, "RFEL", CLAIMED, tos_cache=gate_cache)
        assert fix_cache.asked[0] == gate_cache.asked[0]


class TestEposCacheVersionWasBumped:
    """A code-level header change that does not bump this silently re-serves a
    stale cached header on --force. v4's own comment says so."""

    def test_version_is_at_least_5(self):
        from receivers.dissemination.convert import HEADER_SCHEMA_VERSION

        assert HEADER_SCHEMA_VERSION >= 5

    def test_the_bump_is_documented(self):
        import inspect

        import receivers.dissemination.convert as c

        src = inspect.getsource(c)
        assert "#   v5:" in src, "every bump documents what changed"


class TestEposPathAsksWithTheEpoch:
    """The version bump proves the cache will re-head; this proves the header it
    re-heads to is actually right."""

    def _call(self, tmp_path, first_obs_line, claimed=CLAIMED):
        import receivers.dissemination.convert as conv

        f = tmp_path / "RFEL2510.26o"
        f.write_text(
            "     3.04           OBSERVATION DATA    M                   "
            "RINEX VERSION / TYPE\n"
            + (f"{first_obs_line:<60}TIME OF FIRST OBS   \n" if first_obs_line else "")
            + "                                                            "
            "END OF HEADER       \n",
            encoding="latin-1",
        )
        seen = {}

        def _fake(**kw):
            seen["observation_date"] = kw["observation_date"]
            return kw["rinex_file"]

        import tostools.rinex as tr

        real = tr.correct_rinex_from_tos
        tr.correct_rinex_from_tos = _fake
        try:
            conv.set_header_from_tos(f, "RFEL", claimed)
        finally:
            tr.correct_rinex_from_tos = real
        return seen.get("observation_date")

    def test_the_post_swap_epoch_reaches_the_corrector(self, tmp_path):
        got = self._call(tmp_path, "  2026     9     8    19    26   45.0000000     GPS")
        assert got == datetime(2026, 9, 8, 19, 26, 45)
        assert got > SWAP, "would have published the NETRS header"

    def test_a_headerless_file_falls_back_to_the_claim(self, tmp_path):
        assert self._call(tmp_path, None) == CLAIMED

    def test_a_foreign_day_does_not_move_the_lookup(self, tmp_path):
        got = self._call(tmp_path, "  2026     9    10    19    26   45.0000000     GPS")
        assert got == CLAIMED
