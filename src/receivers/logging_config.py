"""
Unified logging configuration for the GPS receivers package.

Single entry point for all logging setup. Replaces the separate setup functions
in cli/main.py and scheduling/bulk_scheduler.py with one idempotent function.

All loggers use the ``receivers.*`` hierarchy:
    receivers.download.{station}   — download jobs
    receivers.health.{station}     — health extractors
    receivers.scheduler            — scheduler core
    receivers.scheduler.backfill   — backfill jobs
    receivers.scheduler.gaps       — gap detection
    receivers.scheduler.reconciler — archive reconciler
    receivers.pipeline.{station}   — pipeline tracking
    receivers.audit                — audit trail (separate file)

Per-component level overrides are read from the ``[logging]`` section
of ``database.cfg``::

    [logging]
    # receivers.health = DEBUG
    # receivers.scheduling = WARNING
"""

import configparser
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

from .base.production_logging import ProductionFormatter, JSONFormatter

# Sentinel to make setup_logging() idempotent
_configured = False

# Default log directory
_DEFAULT_LOG_DIR = Path.home() / ".cache" / "gps_receivers" / "logs"

# Third-party loggers to suppress at WARNING level
_NOISY_LOGGERS = ("urllib3", "ftplib", "gps_parser", "apscheduler")


def _load_level_overrides() -> dict[str, int]:
    """Read per-component log-level overrides from database.cfg [logging]."""
    config_dir = os.getenv("GPS_CONFIG_PATH")
    if config_dir:
        cfg_path = Path(config_dir) / "database.cfg"
    else:
        cfg_path = Path.home() / ".config" / "gpsconfig" / "database.cfg"

    if not cfg_path.exists():
        return {}

    parser = configparser.ConfigParser()
    parser.read(cfg_path)

    if not parser.has_section("logging"):
        return {}

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    overrides: dict[str, int] = {}
    for key, value in parser.items("logging"):
        level = level_map.get(value.strip().upper())
        if level is not None:
            overrides[key] = level

    return overrides


def setup_logging(
    level: int = logging.INFO,
    json_output: bool = False,
    log_dir: Optional[Path] = None,
    component: str = "receivers",
) -> logging.Logger:
    """Configure the ``receivers`` logger hierarchy.

    Sets up:
      - Console handler with :class:`ProductionFormatter` (or JSON if requested)
      - Rotating file handler (JSON, 20 MB, 3 backups) → ``{log_dir}/receivers.log``
      - Suppression of noisy third-party loggers
      - Per-component level overrides from ``database.cfg``

    Safe to call multiple times — subsequent calls are no-ops.

    Args:
        level: Base log level (default INFO).
        json_output: Use JSON format on the console (for monitoring pipelines).
        log_dir: Directory for log files. Defaults to ``~/.cache/gps_receivers/logs``.
        component: Sub-logger name to return (e.g. ``"scheduler"`` → ``receivers.scheduler``).

    Returns:
        A logger under the ``receivers`` hierarchy.
    """
    global _configured
    if not _configured:
        _configure(level, json_output, log_dir or _DEFAULT_LOG_DIR)
        _configured = True

    return logging.getLogger(f"receivers.{component}" if component != "receivers" else "receivers")


def _configure(level: int, json_output: bool, log_dir: Path) -> None:
    """Internal: wire up handlers on the ``receivers`` root logger."""
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("receivers")
    root.setLevel(logging.DEBUG)  # handlers decide what passes through
    root.propagate = False  # don't bubble up to the root logger

    # ── Console handler ──────────────────────────────────────────────
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    if json_output:
        console.setFormatter(JSONFormatter())
    else:
        console.setFormatter(ProductionFormatter())
    root.addHandler(console)

    # ── File handler (JSON, rotating) ────────────────────────────────
    log_file = log_dir / "receivers.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=20 * 1024 * 1024,  # 20 MB
        backupCount=3,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    root.addHandler(file_handler)

    # ── Suppress noisy third-party loggers ───────────────────────────
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # ── Per-component level overrides from database.cfg ──────────────
    overrides = _load_level_overrides()
    for logger_name, lvl in overrides.items():
        logging.getLogger(logger_name).setLevel(lvl)
