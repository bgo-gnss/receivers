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
from .downsample import DownsampleResult, RinexDownsampler
from .gap import (
    GapFiller,
    GapFillResult,
    GapPolicy,
    find_missing_hours,
    make_archive_slot_checker,
)
from .ingest import BncRinexFile, IngestResult, StreamIngestor, parse_bnc_rinex_name
from .pipeline import StationCycleResult, StreamPipeline
from .skeleton import (
    SkeletonMetadata,
    build_skeleton,
    fill_skeleton,
    geodetic_to_ecef,
    metadata_from_tos,
    refresh_skeleton,
    upgrade_skeleton,
)
from .supervisor import StreamSupervisor, SuperviseResult

__all__ = [
    "AcquisitionMode",
    "StreamConfig",
    "get_acquisition_mode",
    "DEFAULT_CASTER_HOST",
    "DEFAULT_CASTER_PORT",
    "StreamSupervisor",
    "SuperviseResult",
    "RinexDownsampler",
    "DownsampleResult",
    "StreamIngestor",
    "IngestResult",
    "BncRinexFile",
    "parse_bnc_rinex_name",
    "GapFiller",
    "GapFillResult",
    "GapPolicy",
    "find_missing_hours",
    "make_archive_slot_checker",
    "StreamPipeline",
    "StationCycleResult",
    "SkeletonMetadata",
    "fill_skeleton",
    "refresh_skeleton",
    "upgrade_skeleton",
    "metadata_from_tos",
    "build_skeleton",
    "geodetic_to_ecef",
]
