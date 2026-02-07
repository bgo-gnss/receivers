"""Scheduled task implementations.

This module contains concrete implementations of ScheduledTask for different operations:
- DownloadTask: Downloads data from receivers
- StatusTask: Live receiver status checks (real-time monitoring, includes ping)
- HealthTask: Background health extraction from downloaded files
- RINEXTask: Converts raw files to RINEX format
- SyncTask: Syncs files to permanent storage via rsync

Note: Ping checks are integrated into StatusTask - no separate PingTask needed.
The connection checker performs ping first and skips remaining checks if unreachable.
"""

from .download_task import DownloadTask
from .health_task import HealthTask
from .rinex_task import RINEXTask
from .status_task import StatusTask
from .sync_task import SyncTask

__all__ = [
    'DownloadTask',
    'HealthTask',
    'RINEXTask',
    'StatusTask',
    'SyncTask',
]
