"""
Trimble native RINEX 3 converter using Docker + Wine.

This module provides native RINEX 3 conversion for Trimble T00/T02 files
using the official Trimble Convert to RINEX utility running in a Docker
container with Wine.

Requirements:
    - Docker installed and running
    - trm2rinex:cli-light Docker image built from:
      https://github.com/Matioupi/trm2rinex-docker

Advantages over teqc+gfzrnx workflow:
    - Native RINEX 3.x output (not reformatted)
    - Proper RINEX 3 observation codes
    - Official Trimble conversion

Disadvantages:
    - Requires Docker
    - ~3x slower than native Windows
    - Docker image must be built manually (IP restrictions)
"""

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .converter_base import (
    ConversionError,
    NamingConvention,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)


class TrimbleNativeConverter(RawToRinexConverter):
    """Native RINEX 3 converter for Trimble files using Docker.

    Uses the Trimble Convert to RINEX utility via Docker+Wine wrapper.

    Supports:
    - NetR9 .T02 files
    - NetRS .T00 files
    - Native RINEX 3.02, 3.03, 3.04, 3.05 output

    Example:
        >>> converter = TrimbleNativeConverter("MANA", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("MANA202601010000a.T02")
        >>> print(result.rinex_file)
        MANA0010.26o.gz
    """

    # Docker image name
    DOCKER_IMAGE = "trm2rinex:cli-light"

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: OutputFormat = OutputFormat.MODERN,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        docker_image: Optional[str] = None,
        loglevel: int = logging.INFO,
    ):
        """Initialize Trimble native converter.

        Args:
            station_id: Station identifier (e.g., 'MANA')
            rinex_version: Target RINEX version (3.02-3.05)
            output_format: Output format (modern or legacy)
            naming_convention: Filename convention (SHORT or LONG)
            apply_header_corrections: Whether to apply TOS metadata corrections
            docker_image: Override Docker image name (default: trm2rinex:cli-light)
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=apply_header_corrections,
            loglevel=loglevel,
        )
        self.docker_image = docker_image or self.DOCKER_IMAGE
        self._temp_dirs: List[Path] = []

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [".t02", ".T02", ".t00", ".T00", ".t02.gz", ".T02.gz", ".t00.gz", ".T00.gz"]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        return "trimble-docker"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        return ["docker"]

    @classmethod
    def is_available(cls) -> bool:
        """Check if Docker and the trm2rinex image are available.

        Returns:
            True if Docker is running and image exists
        """
        try:
            # Check Docker is running
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False

            # Check image exists
            result = subprocess.run(
                ["docker", "image", "inspect", cls.DOCKER_IMAGE],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run native Trimble to RINEX conversion via Docker.

        Args:
            raw_file: Path to T02/T00 file
            output_dir: Output directory for RINEX file
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file

        Raises:
            ConversionError: If conversion fails
        """
        import gzip

        try:
            # Create temp directory for Docker volume mount
            temp_dir = Path(tempfile.mkdtemp(prefix="trimble_native_"))
            self._temp_dirs.append(temp_dir)

            # Decompress if needed and copy to temp dir
            if raw_file.suffix.lower() == '.gz':
                working_file = temp_dir / raw_file.stem
                with gzip.open(raw_file, 'rb') as f_in:
                    with open(working_file, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
            else:
                working_file = temp_dir / raw_file.name
                shutil.copy(raw_file, working_file)

            # Create output subdirectory
            docker_out = temp_dir / "out"
            docker_out.mkdir()

            # Determine RINEX version string
            version_map = {
                RinexVersion.RINEX_2: "2.11",
                RinexVersion.RINEX_3: "3.04",
                RinexVersion.RINEX_4: "3.05",  # Trimble doesn't support RINEX 4 yet
            }
            rinex_ver = version_map.get(self.rinex_version, "3.04")

            # Build Docker command
            # docker run --rm -v "temp_dir:/data" trm2rinex:cli-light \
            #   data/file.T02 -p data/out -v 3.04 -d -co -s
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{temp_dir}:/data",
                self.docker_image,
                f"data/{working_file.name}",
                "-p", "data/out",
                "-v", rinex_ver,
                "-d",   # Include Doppler
                "-co",  # Include clock offsets
                "-s",   # Include SNR
            ]

            self.logger.info(f"Running Trimble native conversion for {raw_file.name}")
            self.logger.debug(f"Docker command: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            if result.returncode != 0:
                raise ConversionError(
                    f"Docker conversion failed: {result.stderr}",
                    raw_file,
                )

            # Find output file
            rinex_file = self._find_output_file(docker_out, observation_date)

            if not rinex_file:
                raise ConversionError(
                    "Trimble converter produced no output file",
                    raw_file,
                )

            # Move to final output directory
            final_file = output_dir / rinex_file.name
            shutil.move(rinex_file, final_file)

            return final_file

        except subprocess.TimeoutExpired:
            raise ConversionError(
                "Docker conversion timed out after 10 minutes",
                raw_file,
            )
        except Exception as e:
            if isinstance(e, ConversionError):
                raise
            raise ConversionError(str(e), raw_file)
        finally:
            self._cleanup_temp_dirs()

    def _find_output_file(
        self,
        output_dir: Path,
        observation_date: datetime,
    ) -> Optional[Path]:
        """Find the RINEX output file created by Trimble converter.

        Args:
            output_dir: Directory where converter wrote output
            observation_date: Date of observation

        Returns:
            Path to RINEX file if found
        """
        # Trimble converter creates files with various naming patterns
        patterns = [
            "*.??o",   # RINEX 2/3 obs
            "*.??O",
            "*.rnx",   # RINEX 3
            "*.RNX",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            # Filter to observation files only (not nav)
            obs_files = [
                f for f in matches
                if f.suffix.lower() in ('.o', '.rnx') or
                   (len(f.suffix) == 4 and f.suffix[3].lower() == 'o')
            ]
            if obs_files:
                return max(obs_files, key=lambda p: p.stat().st_mtime)

        return None

    def _cleanup_temp_dirs(self) -> None:
        """Clean up temporary directories."""
        for temp_dir in self._temp_dirs:
            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                    self.logger.debug(f"Cleaned up {temp_dir}")
            except Exception as e:
                self.logger.warning(f"Could not clean up {temp_dir}: {e}")
        self._temp_dirs.clear()
