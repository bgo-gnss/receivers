"""Sync/dissemination config — declarative targets for the archive feed.

Loaded from ``sync.yaml`` (in ``GPS_CONFIG_PATH`` or ``~/.config/gpsconfig/``),
mirroring how ``scheduler.yaml`` is loaded. One ``targets:`` list; ``rawdata`` is
the first target (``tier: archive``). EPOS and other dissemination targets
(``tier: dissemination``) get added later — same engine, different rules — and
are inert until #34 lands. See design 1781867391.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - yaml is a hard dep in practice
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

logger = logging.getLogger(__name__)

DEFAULT_OVERLAP_MINUTES = 5


@dataclass(frozen=True)
class SyncTarget:
    """One declarative sync destination.

    For the Monday MVP only the ``tier: archive`` target (rawdata) is active.
    """

    name: str
    """Stable identifier; also the ``archive_catalog.storage_location`` value."""

    active: bool
    """Master on/off switch. Inactive targets are skipped entirely."""

    tier: str
    """'archive' (authoritative, audited, no conversion) or 'dissemination'."""

    host: str
    user: str
    dest: str
    """Destination base path on the remote (e.g. '~/gpsdata')."""

    source_root: str
    """Local collection root to push from (e.g. '/mnt/data/gpsdata')."""

    sessions: tuple[str, ...]
    """Session-dir names to include (e.g. ('15s_24hr', '1Hz_1hr', 'status_1hr'))."""

    file_categories: tuple[str, ...]
    """Tiers to push, in order — e.g. ('raw', 'rinex'). Each tier carries its own
    archive immutability rule (raw never overwrites; rinex updates-if-newer) —
    see ``engine.IMMUTABILITY``."""

    exclude_stations: frozenset[str]
    """Stations NOT pushed here — aliases (DYNA/HRNC/HAUR) for the archive."""

    cutover: datetime
    """The watermark floor: files older than this never enter the delta."""

    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES
    """Re-scan window below the watermark guarding the mtime-boundary race."""

    @property
    def remote(self) -> str:
        """``user@host:dest`` rsync destination spec."""
        return f"{self.user}@{self.host}:{self.dest}"


def _default_config_path() -> Path:
    gps_config_dir = os.getenv("GPS_CONFIG_PATH")
    base = (
        Path(gps_config_dir)
        if gps_config_dir
        else Path.home() / ".config" / "gpsconfig"
    )
    return base / "sync.yaml"


def _parse_cutover(value: object, target_name: str) -> datetime:
    """Parse a target's ``cutover`` into a NAIVE-local datetime.

    Accepts an ISO 8601 string or a value PyYAML already parsed to a datetime.
    The watermark lives in the file-mtime domain (naive local time on the
    collection host), so an aware value is converted to local and its tzinfo
    dropped — keeping it comparable with ``find -newermt`` / file mtimes.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.strip())  # py3.11+ handles trailing 'Z'
    else:
        raise ValueError(
            f"target {target_name!r}: 'cutover' must be an ISO timestamp, got {value!r}"
        )
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _build_target(raw: dict, default_overlap: int) -> SyncTarget:
    name = raw["name"]
    return SyncTarget(
        name=name,
        active=bool(raw.get("active", False)),
        tier=raw.get("tier", "archive"),
        host=raw["host"],
        user=raw["user"],
        dest=raw["dest"],
        source_root=raw["source_root"],
        sessions=tuple(raw.get("sessions", ())),
        # Accept the list `file_categories`, or the legacy singular `file_category`.
        file_categories=tuple(
            raw.get("file_categories") or [raw.get("file_category", "raw")]
        ),
        exclude_stations=frozenset(raw.get("exclude_stations", ())),
        cutover=_parse_cutover(raw["cutover"], name),
        overlap_minutes=int(raw.get("overlap_minutes", default_overlap)),
    )


def load_sync_config(config_path: Path | None = None) -> list[SyncTarget]:
    """Load all sync targets from ``sync.yaml``.

    Returns an empty list when the file is absent (sync simply does nothing).
    Raises ``RuntimeError`` if PyYAML is unavailable and a config exists.
    """
    path = config_path or _default_config_path()
    if not path.is_file():
        logger.info("No sync config at %s — archive sync disabled", path)
        return []
    if not HAS_YAML:
        raise RuntimeError("PyYAML is required to read sync.yaml")
    assert yaml is not None
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    default_overlap = int(doc.get("overlap_minutes", DEFAULT_OVERLAP_MINUTES))
    return [_build_target(t, default_overlap) for t in doc.get("targets", [])]
