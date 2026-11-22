"""Which stations are ours: the fleet, as stations.cfg declares it.

yang's POS directory also holds files for the global reference stations the
processing uses and for stations long out of service. A file for a station
outside the roster is only worth an operator's attention when the station is
one of ours, so the check tags the others ``not_in_fleet`` and leaves them out
of the "unexpected files" count (``--no-ignore`` counts them again).

The fleet is every ``[STATION]`` section of stations.cfg that is

* not ``station_status = inactive`` / ``discontinued``, and
* not ``is_reference_site = true`` (the global IGS stations; never use
  ``station_role = passive`` for this, which also covers our own passive
  stations).

Only the section names and those two keys are read. stations.cfg also holds
receiver passwords; nothing here prints or keeps any other value.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

INACTIVE = frozenset({"inactive", "discontinued"})


class FleetError(ValueError):
    """stations.cfg could not be read."""


def default_stations_cfg() -> Path:
    """``$GPS_CONFIG_PATH/stations.cfg``, else ``~/.config/gpsconfig/stations.cfg``."""
    base = os.environ.get("GPS_CONFIG_PATH")
    root = Path(base) if base else Path.home() / ".config" / "gpsconfig"
    return root / "stations.cfg"


def load_fleet(path: Path) -> frozenset[str]:
    """The active, non-reference stations in ``path``."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error) as exc:
        raise FleetError(f"{path}: {type(exc).__name__}") from exc
    fleet = set()
    for sid in parser.sections():
        sec = parser[sid]
        status = (sec.get("station_status") or "active").strip().lower()
        reference = (sec.get("is_reference_site") or "false").strip().lower()
        if status not in INACTIVE and reference != "true":
            fleet.add(sid)
    if not fleet:
        raise FleetError(f"{path}: no active stations")
    return frozenset(fleet)
