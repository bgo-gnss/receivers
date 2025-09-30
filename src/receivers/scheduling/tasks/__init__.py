"""Scheduled task implementations.

This module contains concrete implementations of ScheduledTask for different operations:
- DownloadTask: Downloads data from receivers
- (Future) StatusTask: Checks receiver status
- (Future) HealthTask: Performs health monitoring
- (Future) ValidateTask: Validates configurations
"""

from .download_task import DownloadTask

__all__ = ['DownloadTask']
