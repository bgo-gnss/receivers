"""Generate per-station BNC (BKG Ntrip Client) configuration files.

Replaces hand-maintained ``rtcm2rinex-<SID>.bnc`` files with configs rendered from
a :class:`~receivers.streaming.config.StreamConfig`. The output is a faithful subset
of the keys BNC needs to convert an RTCM3 mountpoint into hourly RINEX; BNC falls
back to internal defaults for any key not emitted here.

The rendered config embeds caster credentials in ``casterUrlList`` and ``mountPoints``
(BNC's required form), so written files are created with ``0600`` permissions and
credentials are never logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Tuple

from .config import StreamConfig

logger = logging.getLogger(__name__)


def _credentialed_authority(cfg: StreamConfig) -> str:
    """Return ``user:pass@host:port`` (or ``host:port`` when no creds)."""
    if cfg.caster_user:
        secret = cfg.caster_password or ""
        return f"{cfg.caster_user}:{secret}@{cfg.caster_netloc}"
    return cfg.caster_netloc


def build_bnc_config(cfg: StreamConfig) -> str:
    """Render a BNC ``.bnc`` config file body for ``cfg``.

    Produces hourly RINEX capture of the station's RTCM3 mountpoint into
    ``cfg.rnx_path``. RINEX version follows ``cfg.rnx_version`` (2 → ``rnxV3=0``).
    """
    authority = _credentialed_authority(cfg)
    lat = f"{cfg.latitude:.2f}" if cfg.latitude is not None else "0.00"
    lon = f"{cfg.longitude:.2f}" if cfg.longitude is not None else "0.00"
    rnx_v3 = 1 if cfg.rnx_version >= 3 else 0
    log_file = str(Path(cfg.rnx_path) / "RinexObs.log")

    # mountPoints format: //auth/MOUNT FORMAT COUNTRY LAT LON nmea ntrip-version
    mount_line = (
        f"//{authority}/{cfg.mountpoint} RTCM_3 {cfg.country} {lat} {lon} no 1"
    )

    general: List[Tuple[str, str]] = [
        ("adviseFail", "15"),
        ("adviseReco", "5"),
        ("autoStart", "2"),
        ("casterUrlList", f"http://{authority}"),
        ("ignoreSslErrors", "2"),
        ("logFile", log_file),
        ("mountPoints", mount_line),
        ("onTheFlyInterval", "1 day"),
        ("rnxAppend", "2"),
        ("rnxIntr", cfg.rnx_interval),
        ("rnxPath", cfg.rnx_path),
        ("rnxSampl", str(cfg.rnx_sampling)),
        ("rnxSkel", "SKL"),
        ("rnxV2Priority", "CWPX_?"),
        ("rnxV3", str(rnx_v3)),
    ]
    general.extend(sorted(cfg.extra.items()))

    lines = ["[General]"]
    lines.extend(f"{k}={v}" for k, v in general)
    lines.append("[PPP]")  # BNC expects the section header; defaults are fine.
    lines.append("")  # trailing newline
    return "\n".join(lines)


def write_bnc_config(cfg: StreamConfig, path: os.PathLike | str) -> Path:
    """Write the BNC config for ``cfg`` to ``path`` with 0600 perms (contains creds).

    Creates parent directories and the configured ``rnx_path`` output directory.
    Returns the written path.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if cfg.rnx_path:
        Path(cfg.rnx_path).mkdir(parents=True, exist_ok=True)
    body = build_bnc_config(cfg)
    out.write_text(body)
    out.chmod(0o600)
    logger.info(
        "Wrote BNC config for %s -> %s (mountpoint=%s, rnx=%s)",
        cfg.station_id,
        out,
        cfg.mountpoint,
        cfg.rnx_path,
    )
    return out


def bnc_config_filename(station_id: str) -> str:
    """Canonical BNC config filename for a station (legacy convention)."""
    return f"rtcm2rinex-{station_id}.bnc"
