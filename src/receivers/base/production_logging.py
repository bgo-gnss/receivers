#!/usr/bin/env python3
"""
Production logging configuration for GPS receiver management system.

Provides:
- Concise INFO level logging for automated systems
- Structured JSON logging for monitoring integration
- Separate audit trail for download statistics
- Error-focused output for critical issues
- Integration-ready format for Icinga monitoring
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional


class ProductionFormatter(logging.Formatter):
    """Concise formatter optimized for production automated systems."""
    
    def __init__(self):
        super().__init__()
        
    def format(self, record):
        # Production format: timestamp - level - station - message
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        
        # Extract station ID from logger name if available
        station_id = ""
        if 'receiver.' in record.name:
            station_id = f"[{record.name.split('.')[-1]}] "
        elif hasattr(record, 'station_id'):
            station_id = f"[{record.station_id}] "
            
        # Concise level indicators
        level_indicators = {
            'CRITICAL': '🔴',
            'ERROR': '❌',
            'WARNING': '⚠️ ',
            'INFO': '✅',
            'DEBUG': '🔍'
        }
        
        level_icon = level_indicators.get(record.levelname, record.levelname)
        
        return f"{timestamp} {level_icon} {station_id}{record.getMessage()}"


class JSONFormatter(logging.Formatter):
    """JSON formatter for monitoring system integration."""
    
    def format(self, record):
        log_entry = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'line': record.lineno
        }
        
        # Add station context if available
        if 'receiver.' in record.name:
            log_entry['station_id'] = record.name.split('.')[-1]
        elif hasattr(record, 'station_id'):
            log_entry['station_id'] = record.station_id
            
        # Add performance metrics if available
        if hasattr(record, 'duration'):
            log_entry['duration_seconds'] = record.duration
        if hasattr(record, 'bytes_downloaded'):
            log_entry['bytes_downloaded'] = record.bytes_downloaded
        if hasattr(record, 'files_count'):
            log_entry['files_count'] = record.files_count
            
        # Add error context if available
        if record.levelname in ['ERROR', 'CRITICAL'] and hasattr(record, 'error_type'):
            log_entry['error_type'] = record.error_type
            log_entry['error_category'] = getattr(record, 'error_category', 'unknown')
            
        return json.dumps(log_entry)


class AuditLogger:
    """Separate audit logger for download statistics and performance metrics."""
    
    def __init__(self, log_dir: Path = None):
        if log_dir is None:
            log_dir = Path.home() / '.cache' / 'gps_receivers' / 'logs'
        
        log_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = log_dir / 'download_audit.jsonl'
        
        # Set up audit logger
        self.logger = logging.getLogger('gps_receivers.audit')
        self.logger.setLevel(logging.INFO)
        
        # Remove existing handlers to avoid duplicates
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            
        # JSON file handler for audit trail
        file_handler = logging.handlers.RotatingFileHandler(
            self.audit_file,
            maxBytes=50*1024*1024,  # 50MB
            backupCount=5
        )
        file_handler.setFormatter(JSONFormatter())
        self.logger.addHandler(file_handler)
        
        # Prevent propagation to avoid duplicate logs
        self.logger.propagate = False
    
    def log_download_session(self, station_id: str, session_data: Dict[str, Any]):
        """Log complete download session statistics."""
        audit_entry = {
            'event_type': 'download_session',
            'station_id': station_id,
            'session': session_data.get('session', 'unknown'),
            'status': session_data.get('status', 'unknown'),
            'duration_seconds': session_data.get('duration', 0),
            'files_downloaded': session_data.get('files_downloaded', 0),
            'bytes_downloaded': session_data.get('bytes_downloaded', 0),
            'errors': session_data.get('errors', 0),
            'start_time': session_data.get('start_time'),
            'end_time': session_data.get('end_time')
        }
        
        # Add performance metrics if available
        if 'connection_time' in session_data:
            audit_entry['connection_time_seconds'] = session_data['connection_time']
        if 'download_speed' in session_data:
            audit_entry['download_speed_kbps'] = session_data['download_speed']
            
        self.logger.info('Download session completed', extra=audit_entry)
    
    def log_performance_metrics(self, station_id: str, metrics: Dict[str, Any]):
        """Log performance metrics for monitoring."""
        audit_entry = {
            'event_type': 'performance_metrics',
            'station_id': station_id,
            'metrics': metrics
        }
        
        self.logger.info('Performance metrics', extra=audit_entry)
    
    def log_failure_event(self, station_id: str, failure_data: Dict[str, Any]):
        """Log failure events for analysis."""
        audit_entry = {
            'event_type': 'failure',
            'station_id': station_id,
            'error_type': failure_data.get('error_type', 'unknown'),
            'error_category': failure_data.get('error_category', 'unknown'),
            'error_message': failure_data.get('error_message', ''),
            'severity': failure_data.get('severity', 'unknown'),
            'validation_triggered': failure_data.get('validation_triggered', False)
        }
        
        self.logger.error('Station failure', extra=audit_entry)


class ProductionLoggingConfig:
    """Production logging configuration manager."""
    
    def __init__(self, log_dir: Path = None, json_output: bool = False, verbose: bool = False):
        self.log_dir = log_dir or Path.home() / '.cache' / 'gps_receivers' / 'logs'
        self.json_output = json_output
        self.verbose = verbose
        self.audit_logger = AuditLogger(self.log_dir)
        
    def setup_production_logging(self) -> logging.Logger:
        """Set up production-optimized logging configuration."""
        
        # Create log directory
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        
        # Remove default handlers to avoid duplicates
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Console handler with production formatter
        console_handler = logging.StreamHandler(sys.stdout)
        if self.json_output:
            console_handler.setFormatter(JSONFormatter())
        else:
            console_handler.setFormatter(ProductionFormatter())
        
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)
        
        # File handler for persistent logging
        log_file = self.log_dir / 'receivers.log'
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=20*1024*1024,  # 20MB
            backupCount=3
        )
        file_handler.setFormatter(JSONFormatter())
        file_handler.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        root_logger.addHandler(file_handler)
        
        # Configure specific logger levels for production
        if not self.verbose:
            # Reduce verbosity of specific components
            logging.getLogger('urllib3').setLevel(logging.WARNING)
            logging.getLogger('ftplib').setLevel(logging.WARNING)
            logging.getLogger('gps_parser').setLevel(logging.WARNING)
        
        return root_logger
    
    def get_audit_logger(self) -> AuditLogger:
        """Get the audit logger instance."""
        return self.audit_logger
    
    def create_station_logger(self, station_id: str) -> logging.Logger:
        """Create a logger for a specific station."""
        logger = logging.getLogger(f'receiver.{station_id}')
        
        # Add station ID to all log records
        class StationContextFilter(logging.Filter):
            def filter(self, record):
                record.station_id = station_id
                return True
        
        logger.addFilter(StationContextFilter())
        return logger


def setup_production_logging(json_output: bool = False, verbose: bool = False, log_dir: Path = None) -> ProductionLoggingConfig:
    """
    Set up production logging configuration.
    
    Args:
        json_output: Use JSON format for console output (for monitoring systems)
        verbose: Enable verbose logging (includes DEBUG level)
        log_dir: Custom log directory (defaults to ~/.cache/gps_receivers/logs)
    
    Returns:
        ProductionLoggingConfig instance
    """
    config = ProductionLoggingConfig(log_dir=log_dir, json_output=json_output, verbose=verbose)
    config.setup_production_logging()
    return config


# Example usage and testing
if __name__ == "__main__":
    # Test production logging
    config = setup_production_logging(json_output=False, verbose=False)
    
    # Create station logger
    logger = config.create_station_logger('TEST')
    
    # Test different log levels
    logger.info("Connection test successful")
    logger.warning("Slow download speed detected")
    logger.error("Connection failed - retrying")
    logger.critical("Station unreachable after 3 attempts")
    
    # Test audit logging
    audit = config.get_audit_logger()
    audit.log_download_session('TEST', {
        'session': '15s_24hr',
        'status': 'completed',
        'duration': 45.2,
        'files_downloaded': 3,
        'bytes_downloaded': 15728640,
        'errors': 0,
        'connection_time': 1.2,
        'download_speed': 285.4
    })
    
    print("Production logging test completed")