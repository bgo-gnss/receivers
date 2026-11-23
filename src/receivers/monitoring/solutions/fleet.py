"""Which stations are local, and whose they are, as stations.cfg declares it.

yang's POS directory also holds files for the global reference stations the
processing uses and for stations long out of service. A file for a station
outside the roster is only worth an operator's attention when the station is
local, so the check tags the others ``not_local`` and leaves them out of the
"unexpected files" count (``--no-ignore`` counts them again). The board's
"Stations: local / all" option and its Agency filter read the same facts,
which every check run records in ``solution_station`` (migration 078).

A station is **local** when stations.cfg places it in Iceland and it is not
``station_status = inactive`` / ``discontinued``. Local is a place, not an
owner: HOFN and REYK (IGS) and BJAC (not run by IMO) are local. "In Iceland"
is decided from the section's ``latitude``/``longitude`` (every section has
them; ``is_in_iceland`` is set on only a few), falling back to
``is_in_iceland`` when the coordinates are missing.

The **agency** is ``station_owner``, the value the other GPS boards' Agency
filter shows; the **operator** is ``station_operator``, who runs the station
(IMO runs stations owned by others), for the board's Operator filter. Either
is None when stations.cfg has none.

Only the section names and the keys above are read. stations.cfg also holds
receiver passwords; nothing here prints or keeps any other value.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

INACTIVE = frozenset({"inactive", "discontinued"})

#: Iceland with its offshore islands (Grímsey, Kolbeinsey, Vestmannaeyjar)
ICELAND_LAT = (62.5, 67.5)
ICELAND_LON = (-25.5, -12.5)


class FleetError(ValueError):
    """stations.cfg could not be read."""


@dataclass(frozen=True)
class StationInfo:
    local: bool
    agency: str | None
    operator: str | None = None


def default_stations_cfg() -> Path:
    """``$GPS_CONFIG_PATH/stations.cfg``, else ``~/.config/gpsconfig/stations.cfg``."""
    base = os.environ.get("GPS_CONFIG_PATH")
    root = Path(base) if base else Path.home() / ".config" / "gpsconfig"
    return root / "stations.cfg"


def _in_iceland(sec: configparser.SectionProxy) -> bool:
    try:
        lat = float(sec.get("latitude", ""))
        lon = float(sec.get("longitude", ""))
    except ValueError:
        return (sec.get("is_in_iceland") or "").strip().lower() == "true"
    if lon > 180:
        lon -= 360
    return (
        ICELAND_LAT[0] < lat < ICELAND_LAT[1] and ICELAND_LON[0] < lon < ICELAND_LON[1]
    )


def load_stations(path: Path) -> dict[str, StationInfo]:
    """Every station in ``path``: whether it is local, and its agency."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error) as exc:
        raise FleetError(f"{path}: {type(exc).__name__}") from exc
    info = {}
    for sid in parser.sections():
        sec = parser[sid]
        status = (sec.get("station_status") or "active").strip().lower()
        owner = (sec.get("station_owner") or "").strip() or None
        operator = (sec.get("station_operator") or "").strip() or None
        local = status not in INACTIVE and _in_iceland(sec)
        info[sid] = StationInfo(local, owner, operator)
    if not any(i.local for i in info.values()):
        raise FleetError(f"{path}: no active stations in Iceland")
    return info


def local_stations(info: dict[str, StationInfo]) -> frozenset[str]:
    return frozenset(sid for sid, i in info.items() if i.local)


def load_fleet(path: Path) -> frozenset[str]:
    """The local stations in ``path``."""
    return local_stations(load_stations(path))
