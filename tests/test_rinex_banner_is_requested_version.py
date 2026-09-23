"""The run banner must not be readable as the version that will be written.

`receivers rinex` prints its version banner BEFORE `_create_rinex_converter`
applies the per-station receiver pin, so a NetRS run prints "3" and then writes
RINEX 2. Both lines are in the same log, one after the other:

    RINEX version: 3, Naming: short
    HAUD: NetRS pinned to RINEX 2 (pass --version 3 to override)

That ambiguity cost three sessions. The banner was read as proof that
`station onboard`'s re-rinex stage forces `--version 3` — it does not; it
passes no `--version` at all and the pin fires correctly. The real source of
the 35,854 RINEX 3 NetRS files was the scheduler's backfill job
(`resolve_trimble_rinex_version`, fixed separately).

So this test pins the wording, not the value: the banner must say the version
is what was REQUESTED and that a receiver pin can override it. A future edit
that shortens it back to "RINEX version: N" re-opens the same misreading.
"""

from __future__ import annotations

import re
from pathlib import Path

BANNER_SITE = Path("src/receivers/cli/main.py")


def _banner_block() -> str:
    """The lines around the banner print, as source text."""
    src = BANNER_SITE.read_text()
    m = re.search(r"RINEX version requested:.{0,400}", src, re.S)
    assert m, (
        "the version banner no longer says 'RINEX version requested:' — if it "
        "was reworded, keep the distinction between REQUESTED and EFFECTIVE"
    )
    return m.group(0)


class TestTheBannerDistinguishesRequestedFromEffective:
    def test_it_says_requested(self):
        assert "RINEX version requested:" in _banner_block()

    def test_it_warns_that_a_receiver_pin_can_override(self):
        block = _banner_block()
        assert "override" in block
        assert "pinned to RINEX" in block, (
            "the banner should name the log line the operator must look for, "
            "so the two are read together"
        )

    def test_the_bare_wording_is_gone(self):
        """`RINEX version: N` is what was misread; it must not come back."""
        src = BANNER_SITE.read_text()
        assert not re.search(r'f"RINEX version: \{rinex_version\.value\}', src), (
            "the ambiguous banner wording is back"
        )


class TestThePinStillLogsItself:
    def test_the_pin_message_the_banner_points_at_exists(self):
        """The banner references 'pinned to RINEX' — that string must be real."""
        src = BANNER_SITE.read_text()
        assert "NetRS pinned to RINEX" in src
