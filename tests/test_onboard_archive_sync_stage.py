"""The onboard walk must push raw the backfill left local-only.

The gap this closes, measured on rek-d01 2026-10-03: VFLS and VFLN had 11 and
10 `1Hz_1hr` raw files on the LOCAL tree that never reached the archive — a
contiguous 2026-10-01 00–10 UTC block — while the RINEX for those same hours
pushed fine. Mechanism, all three parts confirmed in the source:

* the backfill/catchup paths never call `_push_to_storage` (only
  `_download_station_data_job` does); `long_term_backfill.py` documents the
  push as "deliberately downstream", relying on the hourly `:45` sweep;
* that sweep is WATERMARK-driven — `floor = max(last_success - overlap_minutes,
  cutover)` and "files older than this never enter the delta"
  (`archive/config.py`), with `overlap_minutes: 5` in production;
* so a new station, whose first hours are filled in one catchup burst AFTER
  the watermark has advanced, strands exactly those files — permanently.

Scope is narrow but systematic: a sweep of 171 stations for 2026-10-01..02
found only STKA (2 files) besides the two new ones, so routine operation is
covered by the live download path. It is **onboarding** that reliably hits it.
"""

from __future__ import annotations

from receivers.cli.station_onboard import (
    STAGES,
    OnboardContext,
    _archive_sync_argv,
)


def _stage(key):
    return next(s for s in STAGES if s.key == key)


def test_the_stage_exists_and_is_mutating():
    s = _stage("archive-sync")
    assert s.mutating is True
    assert s.exec_argv is not None


def test_it_runs_before_re_rinex():
    """`re-rinex` reads raw with --from-archive, so stranded raw is skipped there.

    Ordering is the whole point: pushing after re-rinex would convert an
    incomplete archive and look successful.
    """
    keys = [s.key for s in STAGES]
    assert keys.index("archive-sync") < keys.index("re-rinex")


def test_it_does_not_restrict_to_the_onboard_session():
    """No --session: the stranded files were 1Hz_1hr, the default is 15s_24hr.

    Restricting to ctx.session would miss exactly the case this stage exists
    for — which is how the gap went unnoticed in the first place.
    """
    argv = _archive_sync_argv(OnboardContext(station="VFLS"))
    assert "--session" not in argv, (
        "archive-sync must sweep every session for the station; the onboard "
        "default session is not where the gap showed up"
    )
    assert argv[1:][:3] == ["archive-sync", "--station", "VFLS"]


def test_it_uses_selection_mode_which_skips_the_watermark():
    """`--station` is what makes recovery possible — the sweep cannot see these."""
    argv = _archive_sync_argv(OnboardContext(station="VFLN"))
    assert "--station" in argv and "VFLN" in argv


def test_dates_are_dashed_for_archive_sync_not_compact():
    """`archive-sync` takes YYYY-MM-DD; `rinex` takes YYYYMMDD. Easy to mix up."""
    argv = _archive_sync_argv(
        OnboardContext(station="VFLS", start="20260930", end="20261003")
    )
    assert "--start" in argv and "--end" in argv
    assert argv[argv.index("--start") + 1] == "2026-09-30"
    assert argv[argv.index("--end") + 1] == "2026-10-03"


def test_no_bounds_means_no_date_flags():
    """Without bounds it must fall through to the target's own window, not crash."""
    argv = _archive_sync_argv(OnboardContext(station="VFLS"))
    assert "--start" not in argv and "--end" not in argv


def test_the_preview_explains_the_watermark_blind_spot():
    """A future reader must learn WHY the :45 sweep does not cover this."""
    text = _stage("archive-sync").preview(OnboardContext(station="VFLS"))
    low = text.lower()
    assert "watermark" in low
    assert "overlap_minutes" in low
    assert "selection mode" in low
    assert "transferred=0" in low, "say that a no-op is the healthy outcome"
