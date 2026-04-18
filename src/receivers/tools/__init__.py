"""
RINEX conversion tools management.

This module provides automatic installation and configuration of external tools
required for RINEX conversion from various GPS receiver formats.

Supported Tools:
    - teqc: Legacy converter for Leica MDB and other formats (UNAVCO)
    - gfzrnx: RINEX format conversion and QC (GFZ Potsdam)
    - rnx2crx/crx2rnx: Hatanaka compression tools
    - mdb2rinex: Leica MDB to RINEX 3 (requires Leica myWorld account)
    - runpkr00: Trimble T00/T02 extraction (requires manual install)
    - sbf2rin: Septentrio SBF to RINEX (requires RxTools)

Usage:
    receivers tools list          # Show available tools
    receivers tools install teqc  # Install specific tool
    receivers tools install-all   # Install all auto-installable tools
    receivers tools check         # Verify tool installations
"""

from .tool_manager import InstallResult, ToolInfo, ToolManager

__all__ = ["ToolManager", "ToolInfo", "InstallResult"]
