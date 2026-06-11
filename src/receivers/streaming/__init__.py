"""Stream-capture acquisition mode (RTCM3 → BNC → RINEX).

For low-bandwidth stations the receiver's real-time RTCM3 stream is captured from
an NTRIP caster and converted to RINEX by BNC (BKG Ntrip Client), instead of
downloading logged files over FTP. This subpackage ports that pipeline (legacy
``rtcm2rinex.sh`` + ``conv1Hzrinto15s.sh`` on rek.vedur.is) into the receivers
package as a second acquisition mode selectable per-station in ``stations.cfg``.

Modules
-------
config       : acquisition-mode selection + :class:`StreamConfig` model
bnc_config   : generate per-station BNC ``.bnc`` config files
"""

from .config import (
    DEFAULT_CASTER_HOST,
    DEFAULT_CASTER_PORT,
    AcquisitionMode,
    StreamConfig,
    get_acquisition_mode,
)

__all__ = [
    "AcquisitionMode",
    "StreamConfig",
    "get_acquisition_mode",
    "DEFAULT_CASTER_HOST",
    "DEFAULT_CASTER_PORT",
]
