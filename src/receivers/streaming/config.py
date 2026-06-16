"""Stream-capture configuration: acquisition mode + per-station stream parameters.

A station selects its acquisition mode in ``stations.cfg``::

    acquisition_mode = stream      # default: download

Stream stations pull their RTCM3 data from an NTRIP caster. Caster credentials are
*never* stored in the repo or in stations.cfg — they live in the deployed
``receivers.cfg`` ``[streaming]`` section and are injected at config-build time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: Default NTRIP caster (IMO). Overridable per-station or via [streaming] config.
DEFAULT_CASTER_HOST = "ntrcaster.vedur.is"
DEFAULT_CASTER_PORT = 2101


class AcquisitionMode:
    """How a station's data is acquired."""

    DOWNLOAD = "download"
    """Pull logged files from the receiver over FTP/HTTP (the fleet default)."""

    STREAM = "stream"
    """Capture the real-time RTCM3 stream from an NTRIP caster via BNC."""

    ALL = (DOWNLOAD, STREAM)


def _lookup(station_config: Dict[str, Any], key: str) -> Optional[str]:
    """Find ``key`` across the adapted-config sections (receiver/station/top-level)."""
    for section in (
        station_config.get("receiver"),
        station_config.get("station"),
        station_config,
    ):
        if isinstance(section, dict):
            val = section.get(key)
            if val not in (None, ""):
                return str(val)
    return None


def get_acquisition_mode(station_config: Dict[str, Any]) -> str:
    """Return the station's acquisition mode, defaulting to ``download``.

    Unknown values fall back to ``download`` (fail safe — never silently stream).
    """
    raw = (_lookup(station_config, "acquisition_mode") or "").strip().lower()
    return raw if raw in AcquisitionMode.ALL else AcquisitionMode.DOWNLOAD


@dataclass
class StreamConfig:
    """Per-station parameters for BNC RTCM3→RINEX stream capture.

    Mirrors the meaningful keys of a legacy ``rtcm2rinex-<SID>.bnc`` file. Credentials
    (``caster_user``/``caster_password``) are supplied separately from deployed config,
    not from stations.cfg.
    """

    station_id: str
    mountpoint: str
    caster_host: str = DEFAULT_CASTER_HOST
    caster_port: int = DEFAULT_CASTER_PORT
    caster_user: Optional[str] = None
    caster_password: Optional[str] = None
    rnx_path: str = ""
    rnx_interval: str = "1 hour"
    rnx_sampling: int = 1
    # RINEX 3 by default: matches the authoritative SBF/sbf2rin product (3.04) so
    # the stream interim and the daily SBF supersede agree on version, and RINEX 3
    # represents modern multi-GNSS (incl. GLONASS slots >24) properly.
    rnx_version: int = 3
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country: str = "ISL"  # 3-char ISO for valid RINEX 3 long filenames
    extra: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_station_config(
        cls,
        station_id: str,
        station_config: Dict[str, Any],
        *,
        rnx_path: str,
        caster_user: Optional[str] = None,
        caster_password: Optional[str] = None,
        mountpoint_suffix: str = "0",
    ) -> StreamConfig:
        """Build a StreamConfig from a station's adapted config dict.

        ``mountpoint`` defaults to ``<SID><mountpoint_suffix>`` (the IMO caster
        convention, e.g. ``GONH0``), or a per-station ``stream_mountpoint`` override;
        caster host/port and lat/lon are taken from the station config when present.
        """
        mountpoint = (
            _lookup(station_config, "stream_mountpoint")
            or f"{station_id}{mountpoint_suffix}"
        )
        host = _lookup(station_config, "caster_host") or DEFAULT_CASTER_HOST
        port_s = _lookup(station_config, "caster_port")
        lat = _lookup(station_config, "latitude")
        lon = _lookup(station_config, "longitude")
        return cls(
            station_id=station_id,
            mountpoint=mountpoint,
            caster_host=host,
            caster_port=int(port_s) if port_s else DEFAULT_CASTER_PORT,
            caster_user=caster_user,
            caster_password=caster_password,
            rnx_path=rnx_path,
            latitude=float(lat) if lat else None,
            longitude=float(lon) if lon else None,
        )

    @property
    def caster_netloc(self) -> str:
        """``host:port`` for the caster."""
        return f"{self.caster_host}:{self.caster_port}"
