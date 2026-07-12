"""General symbolic date vocabulary for ``--start`` / ``--end`` / ``--date`` flags.

A tiny registry of tokens resolvable through one entry point, :func:`resolve_date`,
alongside literal dates (``YYYY-MM-DD`` / ``YYYYMMDD``). Built-in tokens:

- ``today`` / ``yesterday`` — the injected "now" and the day before.
- ``full`` / ``last`` — the earliest / latest **archived** observation date for the
  station (truest "what we can actually push"). Needs an archive context.
- ``device_start`` / ``device_end`` — the bounds of a specific device's TOS session,
  selected by ``--device SN``. Needs a device context.

Any token OR literal may carry a ``±N`` day offset — ``full+7``, ``last-1``,
``today-30``, ``2026-01-01+14`` — the "can be added to" knob for trimming a range.

The resolver is **domain-agnostic**: the caller supplies a :class:`DateContext`
with the concrete providers (archive extent, device sessions). A token whose
provider or required argument is missing raises a clear :class:`DateVocabError` —
the vocabulary never silently guesses. New tokens register with :func:`register`
and are then usable by every CLI that resolves through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

# (earliest, latest) archived observation dates, or a device's (start, end).
BoundsFn = Callable[[], "tuple[Optional[date], Optional[date]]"]
DeviceBoundsFn = Callable[[str], "tuple[Optional[date], Optional[date]]"]


class DateVocabError(ValueError):
    """A date value could not be resolved (unknown token, missing provider/arg)."""


@dataclass
class DateContext:
    """Everything a token resolver may need. Providers are lazy — a token only
    invokes the provider it needs, so a caller that supplies none can still use
    literals + ``today``/``yesterday``.

    Attributes:
        role: ``"start"`` | ``"end"`` | ``"date"`` — advisory (available to
            resolvers; the built-ins don't branch on it).
        today: The injected current date (pass it in; never read the clock here,
            so tests are deterministic).
        station: 4-char id, for error messages and archive lookup.
        device_serial: Receiver serial for ``device_start`` / ``device_end``.
        archive_bounds: ``() -> (earliest, latest)`` archived dates; None if the
            caller has no archive context.
        device_bounds: ``serial -> (device_start, device_end|None)`` from TOS;
            ``device_end`` None means an open (still-current) session.
    """

    role: str
    today: date
    station: Optional[str] = None
    device_serial: Optional[str] = None
    archive_bounds: Optional[BoundsFn] = None
    device_bounds: Optional[DeviceBoundsFn] = None


Resolver = Callable[[DateContext], date]
_REGISTRY: dict[str, Resolver] = {}

_LITERAL_FORMATS = ("%Y-%m-%d", "%Y%m%d")
# base (token or literal) followed by a signed day offset, e.g. ``full+7``,
# ``today-30``, ``2026-01-01+14``. The base is non-greedy so the trailing signed
# integer is the offset, not part of the base.
_OFFSET_RE = re.compile(r"^(?P<base>.+?)\s*(?P<offset>[+-]\d+)$")


def register(token: str, resolver: Resolver, *, overwrite: bool = False) -> None:
    """Register a date token. Raises unless ``overwrite`` on a name clash."""
    key = token.strip().lower()
    if not key:
        raise ValueError("date token must be non-empty")
    if key in _REGISTRY and not overwrite:
        raise ValueError(f"date token {key!r} already registered")
    _REGISTRY[key] = resolver


def tokens() -> list[str]:
    """The registered token names, sorted (for help text / error messages)."""
    return sorted(_REGISTRY)


def _parse_literal(value: str) -> Optional[date]:
    for fmt in _LITERAL_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def resolve_date(value: str, ctx: DateContext) -> date:
    """Resolve a literal date or a registry token (with optional ``±N`` offset).

    Order: whole-string literal → literal+offset → token+offset. Raises
    :class:`DateVocabError` on anything unresolvable.
    """
    if value is None:
        raise DateVocabError("no date value")
    raw = value.strip()
    if not raw:
        raise DateVocabError("empty date value")

    # 1. A bare literal is absolute — no offset parsing (so a date's own hyphens
    #    are never mistaken for an offset).
    lit = _parse_literal(raw)
    if lit is not None:
        return lit

    # 2. Split a trailing signed offset; the base may be a literal or a token.
    base, offset = raw, 0
    m = _OFFSET_RE.match(raw)
    if m:
        base = m.group("base").strip()
        offset = int(m.group("offset"))

    lit = _parse_literal(base)
    if lit is not None:
        return lit + timedelta(days=offset)

    resolver = _REGISTRY.get(base.lower())
    if resolver is None:
        raise DateVocabError(
            f"unknown date {value!r} — use a YYYY-MM-DD date or one of: "
            f"{', '.join(tokens())} (any with an optional ±N day offset)"
        )
    return resolver(ctx) + timedelta(days=offset)


# --------------------------------------------------------------------- built-ins
def _require_archive(ctx: DateContext) -> tuple[date, date]:
    if ctx.archive_bounds is None:
        raise DateVocabError(
            "'full'/'last' need an archive context — pass --station "
            "(not available for an allowlist sweep)"
        )
    lo, hi = ctx.archive_bounds()
    if lo is None or hi is None:
        raise DateVocabError(
            f"no archived files found for {ctx.station or 'station'} — "
            "cannot resolve 'full'/'last'"
        )
    return lo, hi


def _require_device(ctx: DateContext) -> tuple[date, Optional[date]]:
    if not ctx.device_serial:
        raise DateVocabError("'device_start'/'device_end' need --device SN")
    if ctx.device_bounds is None:
        raise DateVocabError("device sessions unavailable in this context")
    lo, hi = ctx.device_bounds(ctx.device_serial)
    if lo is None:
        raise DateVocabError(
            f"no TOS device session for serial {ctx.device_serial!r} "
            f"at {ctx.station or 'station'}"
        )
    return lo, hi


def _device_end(ctx: DateContext) -> date:
    _, hi = _require_device(ctx)
    if hi is not None:
        return hi
    # Open (still-current) session → the last archived day, else yesterday.
    if ctx.archive_bounds is not None:
        _, ahi = ctx.archive_bounds()
        if ahi is not None:
            return ahi
    return ctx.today - timedelta(days=1)


register("today", lambda ctx: ctx.today)
register("yesterday", lambda ctx: ctx.today - timedelta(days=1))
register("full", lambda ctx: _require_archive(ctx)[0])
register("last", lambda ctx: _require_archive(ctx)[1])
register("device_start", lambda ctx: _require_device(ctx)[0])
register("device_end", _device_end)
