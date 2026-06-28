"""Dissemination target config — the ``tier: dissemination`` rows of sync.yaml.

Reuses the same ``sync.yaml`` file and parsing helpers as
:mod:`receivers.archive.config`, but a dissemination target carries the extra
metadata the convert+rename stage needs (country code, RINEX version/naming,
the convert cache dir) that an archive (as-is rsync) target does not.

T1 keeps this minimal — enough to drive the tracer bullet for one (station,
date). Later tickets add the TOS include-filter and per-target format spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..archive.config import _default_config_path, _parse_cutover

try:
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - yaml is a hard dep in practice
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

logger = logging.getLogger(__name__)

DEFAULT_COUNTRY_CODE = "ISL"
DEFAULT_CACHE_DIR = "~/.cache/gps_receivers/epos_convert"


@dataclass(frozen=True)
class DisseminationTarget:
    """One ``tier: dissemination`` destination (e.g. the EPOS files server).

    Mirrors the shape of :class:`receivers.archive.config.SyncTarget` for the
    transport fields, and adds the convert/rename metadata.
    """

    name: str
    active: bool
    """Master on/off switch (the ``active: true/false`` gate)."""

    host: str
    user: str
    dest: str
    """Destination base path on the remote (bare local path when ``host`` empty)."""

    source_root: str
    """Local archive root to read RINEX/raw from (e.g. '/mnt/data/gpsdata')."""

    sessions: tuple[str, ...]
    exclude_stations: frozenset[str]

    country_code: str = DEFAULT_COUNTRY_CODE
    """3-char IGS country code for the long name (ISL for Iceland)."""

    rinex_version: int = 3
    naming: str = "long"
    convert_cache_dir: str = DEFAULT_CACHE_DIR
    """Where converted outputs are cached, keyed on
    ``hash(source content_sha256 + TOS-metadata fingerprint)``."""

    cutover: Optional[datetime] = None
    """Optional watermark floor (unused by the T1 single-file path)."""

    tier: str = "dissemination"

    @property
    def remote(self) -> str:
        """rsync destination: ``user@host:dest`` (or bare ``dest`` when local)."""
        if not self.host:
            return self.dest
        return f"{self.user}@{self.host}:{self.dest}"

    @property
    def cache_path(self) -> Path:
        return Path(self.convert_cache_dir).expanduser()


def _build_target(raw: dict) -> DisseminationTarget:
    name = raw["name"]
    fmt = raw.get("format", {}) or {}
    cutover = None
    if raw.get("cutover") is not None:
        cutover = _parse_cutover(raw["cutover"], name)
    return DisseminationTarget(
        name=name,
        active=bool(raw.get("active", False)),
        host=raw.get("host", ""),
        user=raw.get("user", ""),
        dest=raw["dest"],
        source_root=raw["source_root"],
        sessions=tuple(raw.get("sessions", ())),
        exclude_stations=frozenset(raw.get("exclude_stations", ())),
        country_code=fmt.get("country_code", DEFAULT_COUNTRY_CODE),
        rinex_version=int(fmt.get("rinex_version", 3)),
        naming=fmt.get("naming", "long"),
        convert_cache_dir=raw.get("convert_cache_dir", DEFAULT_CACHE_DIR),
        cutover=cutover,
    )


def load_dissemination_config(
    config_path: Path | None = None,
) -> list[DisseminationTarget]:
    """Load all ``tier: dissemination`` targets from ``sync.yaml``.

    Returns an empty list when the file is absent. Archive targets (the default
    ``tier: archive``) are ignored here — they belong to
    :func:`receivers.archive.config.load_sync_config`.
    """
    path = config_path or _default_config_path()
    if not path.is_file():
        logger.info("No sync config at %s — dissemination disabled", path)
        return []
    if not HAS_YAML:
        raise RuntimeError("PyYAML is required to read sync.yaml")
    assert yaml is not None
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    return [
        _build_target(t)
        for t in doc.get("targets", [])
        if t.get("tier") == "dissemination"
    ]
