"""Operational scope: does IMO run this station?

The operational-status model (see ``docs/architecture/operational-status-model.md``)
defines one canonical question — *who runs this station?* — with three answers:

===========  ==================  ==============================================
value        Icelandic           meaning
===========  ==================  ==============================================
``operated`` Innri stöð          IMO runs it: direct download + health monitoring
``external`` Ytri stöð           a third party runs it; we pull their files,
                                 no health polling
``reference`` Viðmiðunarstöð     data-source-only reference station (GLOBK ties),
                                 not operated, no monitoring
===========  ==================  ==============================================

The signal lives in TOS (station attribute ``operational_status``). This module
is the ONE place that reads it and decides, so the fleet does not accumulate a
dozen subtly-different spellings of the same test — which is exactly what
happened with the field it replaces.

**What it replaces, and what it deliberately does NOT (yet).** The fleet decides
"don't poll this station" from four hand-kept cfg fields that all gesture at the
same thing:

===========================  ==========  ==================================
cfg field                    fleet size  folded in here?
===========================  ==========  ==================================
``health_check = passive``   4 stations  **yes** — this is the swap that landed
``download_method``          4 stations  no — documented dead, zero consumers
``station_role = passive``   147         **no — see the warning below**
``is_reference_site = true`` 139         **no — same reason**
===========================  ==========  ==================================

⚠ **``station_role = passive`` is a DIFFERENT axis and must not be folded in
blindly.** Those 147 stations are not IMO field sites at all — they are the IGS
reference sites used for GLOBK network ties (ALGO, ALIC, AUCK, …). They are
correctly ``reference`` in this model, but they are already excluded upstream
(``get_all_station_configs`` skips passive roles), so every caller of THIS
module has already filtered them. Widening this helper to also test
``station_role`` would therefore be a no-op at some call sites and a behaviour
change at others, and it needs its own verification pass — not a drive-by. The
architect's scope for this swap was specifically the ``health_check = passive``
checks.

**Why the swap is behaviour-preserving** (checked before landing): the 4 stations
with ``health_check = passive`` are KRAC/MYVA/THRC/TORK, and TOS marks exactly
those 4 ``external`` — so both the canonical signal and the legacy proxy select
the identical set, and the fallback below never fires on the current fleet.

**The fallback direction is the load-bearing detail.** ``operational_status`` is
absent for a station TOS has nothing to say about, and absent must mean UNKNOWN —
never "not operated":

* reading UNKNOWN as *not operated* would silently drop that station from
  polling. Silent deactivation is the unrecoverable failure class this whole
  body of work exists to avoid: a station that stops being polled produces no
  error, just an absence, and the absence is indistinguishable from a station
  that was intentionally retired.
* reading UNKNOWN as *operated* keeps today's behaviour (the legacy
  ``health_check`` proxy still decides), and the worst case is that a station
  which should be excluded keeps being polled — which is noisy, visible, and
  self-announcing.

So unknown resolves toward polling.

**Coverage** (verified live 2026-10-11): 215 ``operated`` / 4 ``external`` /
0 missing across the 219-station GPS fleet, and the 4 external are exactly the
cfg ``health_check = passive`` set (KRAC, MYVA, THRC, TORK). So in practice the
fallback is a safety net rather than the common path — but it is what makes the
migration safe to land without a flag day.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: The canonical vocabulary (TOS attribute 189). ``reference`` is included with
#: ``external`` in the "not operated by us" sense: we consume its data but do
#: not run or monitor the station.
OPERATIONAL_STATUS_VALUES = ("operated", "external", "reference")


def operational_status(cfg: Dict[str, Any]) -> Optional[str]:
    """Normalised ``operational_status`` from a station config, or ``None``.

    ``None`` means TOS said nothing (or cfg carries no such key) — NOT
    "operated" and NOT "not operated". Callers should almost always use
    :func:`is_operated` / :func:`is_third_party_operated` instead, which
    resolve that ambiguity deliberately.
    """
    val = cfg.get("operational_status")
    if val is None:
        return None
    val = str(val).strip().lower()
    return val or None


def is_operated(cfg: Dict[str, Any]) -> bool:
    """True when IMO runs this station (``operational_status == 'operated'``).

    Prefers the canonical TOS signal; falls back to the legacy
    ``health_check = passive`` proxy when the attribute is absent, resolving
    unknown toward *operated* (see the module docstring for why).

    Call sites that gate INCLUSION read best with this; call sites that gate
    EXCLUSION read best with :func:`is_third_party_operated`.
    """
    status = operational_status(cfg)
    if status is not None:
        return status == "operated"
    # Unknown operational_status: fall back to the proxy, treating unknown as
    # operated (i.e. keep polling unless the proxy positively says otherwise).
    return (cfg.get("health_check") or "").strip().lower() != "passive"


def is_third_party_operated(cfg: Dict[str, Any]) -> bool:
    """True when someone else runs this station (``external`` or ``reference``).

    The exact inverse of :func:`is_operated` — provided separately because
    ``if is_third_party_operated(cfg): skip`` states the intent more directly
    than ``if not is_operated(cfg): skip`` at exclusion sites.
    """
    return not is_operated(cfg)
