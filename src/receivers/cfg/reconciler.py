"""Three-way comparison of stations.cfg, live receiver, and TOS.

Produces a list of :class:`FieldDiff` records — one per declared field —
that capture the cfg value, the value reported by the live receiver (if
queried), and the value present in TOS (if queried), along with a
:class:`Verdict` describing how the sources agree or disagree.

This is the data layer; the CLI in :mod:`receivers.cli.cfg` owns the
human interaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

from .field_manifest import FIELDS, FieldSpec, fields_by_key

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    OK = "ok"  # cfg matches all queried sources
    MISSING = "missing"  # cfg empty, ≥1 source has a value
    CONFLICT = "conflict"  # cfg present, disagrees with at least one source
    SOURCES_DISAGREE = "sources_disagree"  # receiver and TOS disagree
    NO_DATA = "no_data"  # no source could supply a value
    NOT_QUERYABLE = "not_queryable"  # field can't be derived from requested sources


class SourceUnavailable(Exception):
    """Raised when a requested source could not be queried."""


@dataclass
class FieldDiff:
    spec: FieldSpec
    cfg_value: Optional[str]
    receiver_value: Optional[str]  # None: not queried OR field not receiver-derivable
    tos_value: Optional[str]  # None: not queried OR field absent in TOS
    sources_queried: FrozenSet[str] = field(default_factory=frozenset)
    verdict: Verdict = Verdict.OK
    suggestion: Optional[str] = None
    suggestion_source: Optional[str] = None  # "receiver", "tos", "agree"
    note: Optional[str] = None

    @property
    def cfg_key(self) -> str:
        return self.spec.cfg_key

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def needs_attention(self) -> bool:
        return self.verdict not in (Verdict.OK, Verdict.NO_DATA, Verdict.NOT_QUERYABLE)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "field": self.cfg_key,
            "label": self.label,
            "cfg": self.cfg_value,
            "receiver": self.receiver_value,
            "tos": self.tos_value,
            "sources_queried": sorted(self.sources_queried),
            "verdict": self.verdict.value,
            "suggestion": self.suggestion,
            "suggestion_source": self.suggestion_source,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _read_cfg_value(
    station_config: Dict[str, Any], spec: FieldSpec
) -> Optional[str]:
    """Look up a field in a station config dict.

    Station configs come in two shapes in this codebase:

    * Flat: ``{"receiver_type": "PolaRX5", "router_ip": "1.2.3.4", ...}``
    * Nested: ``{"receiver": {"type": "..."}, "router": {"ip": "..."}}``

    We look for the flat key first (which matches what's actually written
    in stations.cfg), then fall back to common nested locations.
    """
    val = station_config.get(spec.cfg_key)
    if val is None:
        # nested fallbacks
        if spec.cfg_key.startswith("receiver_"):
            sub = spec.cfg_key[len("receiver_"):]
            val = (station_config.get("receiver") or {}).get(sub)
        elif spec.cfg_key.startswith("antenna_"):
            sub = spec.cfg_key[len("antenna_"):]
            val = (station_config.get("antenna") or {}).get(sub)
        elif spec.cfg_key.startswith("router_"):
            sub = spec.cfg_key[len("router_"):]
            val = (station_config.get("router") or {}).get(sub)
    if val is None or val == "":
        return None
    return str(val)


def _suggest_value(
    spec: FieldSpec,
    receiver_value: Optional[str],
    tos_value: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Choose a candidate value for auto-fill (used when cfg is missing).

    Priority follows the operational workflow: TOS is authoritative for most
    fields, but for receiver-only fields (firmware/serial/type as reported
    by the device) the receiver itself is canonical reality. If both sources
    agree, return the agreed value tagged ``"agree"``.
    """
    if receiver_value is not None and tos_value is not None:
        if spec.values_equal(receiver_value, tos_value):
            return tos_value, "agree"
        return None, None  # disagreement; caller decides
    if tos_value is not None:
        return tos_value, "tos"
    if receiver_value is not None:
        return receiver_value, "receiver"
    return None, None


def _compute_verdict(
    spec: FieldSpec,
    cfg: Optional[str],
    rx: Optional[str],
    tos: Optional[str],
    sources: FrozenSet[str],
) -> Verdict:
    has_rx = rx is not None and "receiver" in sources
    has_tos = tos is not None and "tos" in sources

    # Determine if the requested sources can in principle supply this field.
    can_query_rx = "receiver" in sources and spec.receiver_extract is not None
    can_query_tos = "tos" in sources and spec.tos_extract is not None
    queryable = can_query_rx or can_query_tos

    if not queryable:
        return Verdict.NOT_QUERYABLE

    if not has_rx and not has_tos:
        # The field is queryable but no source returned a value — caller
        # needs to know the result is "no data", not a real conflict.
        return Verdict.NO_DATA

    if cfg is None:
        return Verdict.MISSING

    cfg_matches_rx = (not has_rx) or spec.values_equal(cfg, rx)
    cfg_matches_tos = (not has_tos) or spec.values_equal(cfg, tos)

    if cfg_matches_rx and cfg_matches_tos:
        # cfg agrees with everything queried; sources may still disagree
        # internally, which the caller may want to know about.
        if has_rx and has_tos and not spec.values_equal(rx, tos):
            return Verdict.SOURCES_DISAGREE
        return Verdict.OK

    return Verdict.CONFLICT


def compare_station(
    station_id: str,
    station_config: Dict[str, Any],
    receiver_identity: Optional[Dict[str, Any]],
    tos_data: Optional[Dict[str, Any]],
    fields: Optional[Iterable[str]] = None,
    queried_sources: Optional[Iterable[str]] = None,
) -> List[FieldDiff]:
    """Build :class:`FieldDiff` records for one station.

    Args:
        station_id: 4-letter marker (used only in log messages).
        station_config: Dict from gps_parser for the station.
        receiver_identity: ``health["receiver_identity"]`` dict, or ``None``
            if the receiver was not queried (or could not be reached).
        tos_data: TOS station record, or ``None`` if TOS was not queried
            (or the station is not in TOS).
        fields: Optional whitelist of cfg keys to include.
        queried_sources: Iterable subset of ``{"cfg","receiver","tos"}``
            indicating which sources the caller *attempted* to query. This
            distinguishes "not asked" from "asked but no data": a field
            reported as ``MISSING`` only makes sense for sources we asked.
            Defaults are inferred from the non-None arguments above.

    Returns:
        One :class:`FieldDiff` per field in the manifest (filtered to
        ``fields`` if provided).
    """
    if queried_sources is None:
        sources = {"cfg"}
        if receiver_identity is not None:
            sources.add("receiver")
        if tos_data is not None:
            sources.add("tos")
    else:
        sources = set(queried_sources)
    sources_frozen = frozenset(sources)

    by_key = fields_by_key()
    if fields is not None:
        wanted = [by_key[k] for k in fields if k in by_key]
    else:
        wanted = list(FIELDS)

    diffs: List[FieldDiff] = []
    for spec in wanted:
        cfg_val = spec.normalize(_read_cfg_value(station_config, spec))

        rx_val: Optional[str] = None
        if "receiver" in sources and spec.receiver_extract is not None and receiver_identity is not None:
            try:
                rx_val = spec.normalize(spec.receiver_extract(receiver_identity))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] receiver extract for %s failed: %s",
                             station_id, spec.cfg_key, exc)

        tos_val: Optional[str] = None
        if "tos" in sources and spec.tos_extract is not None and tos_data is not None:
            try:
                tos_val = spec.normalize(spec.tos_extract(tos_data))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] tos extract for %s failed: %s",
                             station_id, spec.cfg_key, exc)

        verdict = _compute_verdict(spec, cfg_val, rx_val, tos_val, sources_frozen)

        suggestion: Optional[str] = None
        suggestion_source: Optional[str] = None
        if verdict in (Verdict.MISSING,):
            suggestion, suggestion_source = _suggest_value(spec, rx_val, tos_val)

        note: Optional[str] = None
        if (
            verdict == Verdict.SOURCES_DISAGREE
            and rx_val is not None
            and tos_val is not None
        ):
            note = f"receiver={rx_val!r} but TOS={tos_val!r}"

        diffs.append(
            FieldDiff(
                spec=spec,
                cfg_value=cfg_val,
                receiver_value=rx_val,
                tos_value=tos_val,
                sources_queried=sources_frozen,
                verdict=verdict,
                suggestion=suggestion,
                suggestion_source=suggestion_source,
                note=note,
            )
        )
    return diffs


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_diff(
    station_id: str,
    diff: FieldDiff,
    new_value: str,
    cfg_path: Optional[Path] = None,
) -> bool:
    """Write ``new_value`` for ``diff.cfg_key`` to stations.cfg.

    Returns True if the file changed, False if the value already matched.
    Raises :class:`FileNotFoundError` if the cfg path can't be located.
    """
    from ..config.receivers_config import _update_cfg_field

    if cfg_path is None:
        try:
            import gps_parser as _gps  # type: ignore
        except ImportError as exc:
            raise SourceUnavailable(
                "gps_parser not importable — cannot locate stations.cfg"
            ) from exc
        cfg_path = Path(_gps.ConfigParser().get_stations_config_path())

    return _update_cfg_field(cfg_path, station_id, diff.cfg_key, new_value)
