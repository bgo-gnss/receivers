#!/usr/bin/env python3
"""Integration tests for receiver workflow with Phase 1 utilities.

This test simulates the complete download workflow for a receiver,
allowing manual inspection of behavior and output comparison.

Run manually to understand the workflow:
    python tests/integration/test_receiver_workflow.py --station ELDC --session 1Hz_1hr --days 1

This will:
1. Generate file lists for the time period
2. Validate existing archives
3. Simulate download process
4. Archive files (using both immediate and bulk modes)
5. Generate detailed report showing what would happen

No actual FTP/HTTP downloads are performed - this tests the file
management, validation, and archiving logic in isolation.
"""

import sys
from pathlib import Path

# Ensure imports work when running script directly
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent.parent
sys.path.insert(0, str(project_root / "src"))

import argparse
import logging
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List

from receivers.utils.archive_validator import ArchiveLocation, ArchiveValidator
from receivers.utils.file_archiver import ArchiveMode, FileArchiver
from receivers.utils.file_validator import FileValidator
from receivers.utils.time_processor import TimeParameterProcessor

# For generating realistic file lists
try:
    import gtimes.timefunc as gt

    HAS_GTIMES = True
except ImportError:
    HAS_GTIMES = False
    print("⚠️  gtimes not available - using simplified datetime generation")


class ReceiverWorkflowSimulator:
    """Simulates receiver download workflow for integration testing.

    This allows manual testing of the complete workflow without
    actual network operations.
    """

    def __init__(
        self,
        station_id: str,
        session: str,
        start: datetime,
        end: datetime,
        receiver_type: str = "polarx5",
    ):
        self.station_id = station_id
        self.session = session
        self.start = start
        self.end = end
        self.receiver_type = receiver_type

        # Initialize utilities
        self.archive_validator = ArchiveValidator()
        self.time_processor = TimeParameterProcessor()
        self.file_validator = FileValidator()

        # Setup directories
        self.temp_dir = Path(tempfile.mkdtemp(prefix=f"receiver_test_{station_id}_"))
        self.tmp_dir = self.temp_dir / "tmp" / station_id
        self.archive_dir = self.temp_dir / "archive" / station_id / session / "raw"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        # Setup logging
        self.logger = self._setup_logger()

        self.logger.info(f"Initialized workflow simulator for {station_id}")
        self.logger.info(f"Session: {session}, Period: {start} to {end}")
        self.logger.info(f"Working directory: {self.temp_dir}")

    def _setup_logger(self) -> logging.Logger:
        """Setup logger for workflow."""
        logger = logging.getLogger(f"workflow.{self.station_id}")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%H:%M:%S",
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    def step1_generate_file_list(self) -> Dict[str, str]:
        """Step 1: Generate list of files to download.

        Returns:
            Dict mapping filename -> remote_path
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 1: Generate File List")
        self.logger.info("=" * 70)

        # Generate datetime list
        if HAS_GTIMES:
            # Use gtimes for accurate datetime generation
            freq = "1H" if "1hr" in self.session else "1D"
            dt_list = gt.datepathlist(
                "#datelist", freq, self.start, self.end, closed="both"
            )
        else:
            # Simplified datetime generation
            dt_list = []
            current = self.start
            increment = (
                timedelta(hours=1) if "1hr" in self.session else timedelta(days=1)
            )
            while current <= self.end:
                dt_list.append(current)
                current += increment

        self.logger.info(f"Generated {len(dt_list)} timestamps")

        # Normalize timestamps based on file frequency
        ffrequency = self.session.split("_")[1] if "_" in self.session else "24hr"
        normalized_list = self.time_processor.normalize_timestamps(dt_list, ffrequency)

        # Generate filenames based on receiver type
        files_dict = {}
        for dt in normalized_list:
            if self.receiver_type == "polarx5":
                # PolaRX5: STATION%Y%m%d%H%Mb.sbf
                session_letter = "b" if "1hr" in self.session else "a"
                filename = (
                    f"{self.station_id}{dt.strftime('%Y%m%d%H%M')}{session_letter}.sbf"
                )
                remote_path = (
                    f"/DSK1/SSN/{self.session}/{dt.strftime('%Y')}/{dt.strftime('%V')}"
                )
            elif self.receiver_type == "netr9":
                # NetR9: STATION%Y%m%d%H%Mb.T02
                session_letter = "b" if "1hr" in self.session else "a"
                filename = (
                    f"{self.station_id}{dt.strftime('%Y%m%d%H%M')}{session_letter}.T02"
                )
                remote_path = f"/Internal/{dt.strftime('%Y%m')}/{self.session}"
            else:
                # Generic
                filename = f"{self.station_id}{dt.strftime('%Y%m%d%H%M')}.dat"
                remote_path = f"/data/{self.session}"

            files_dict[filename] = remote_path

        self.logger.info(f"Generated filenames for {len(files_dict)} files")
        self.logger.info(
            f"Example: {list(files_dict.keys())[0]} -> {list(files_dict.values())[0]}"
        )

        return files_dict

    def step2_create_existing_archives(
        self, files_dict: Dict[str, str], percentage: int = 50
    ):
        """Step 2: Create some existing archive files to simulate partial archive.

        Args:
            files_dict: Dictionary of files that should exist
            percentage: Percentage of files to create (simulate existing data)
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 2: Create Existing Archives (Simulation)")
        self.logger.info("=" * 70)

        import random

        random.seed(42)  # Reproducible results

        # Select files to create
        files_to_create = list(files_dict.keys())[
            : int(len(files_dict) * percentage / 100)
        ]

        self.logger.info(
            f"Creating {len(files_to_create)} existing archive files ({percentage}%)"
        )

        for filename in files_to_create:
            # Create in archive directory with compression
            archive_path = self.archive_dir / f"{filename}.gz"
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            # Create dummy compressed file
            import gzip

            with gzip.open(archive_path, "wb") as f:
                f.write(b"X" * 2048)  # Dummy data

        self.logger.info(
            f"Created {len(files_to_create)} archive files in {self.archive_dir}"
        )

    def step3_validate_archives(self, files_dict: Dict[str, str]) -> Dict[str, str]:
        """Step 3: Validate existing archives and identify missing files.

        Args:
            files_dict: Complete list of files needed

        Returns:
            Dict of missing files that need to be downloaded
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 3: Validate Archives")
        self.logger.info("=" * 70)

        # Generate archive paths
        archive_files_dict = {}
        for filename in files_dict.keys():
            archive_path = self.archive_dir / f"{filename}.gz"
            archive_files_dict[filename] = str(archive_path)

        # Use ArchiveValidator to check existing files
        missing_files, found_count, validated_count = (
            self.archive_validator.batch_validate_archives(
                files_dict, archive_files_dict, self.tmp_dir
            )
        )

        self.logger.info(f"Validated: {validated_count} files")
        self.logger.info(f"Found in archive: {found_count} files")
        self.logger.info(f"Missing (need download): {len(missing_files)} files")

        # Show examples
        if found_count > 0:
            example_found = list(set(files_dict.keys()) - set(missing_files.keys()))[0]
            self.logger.info(f"Example found: {example_found}")

        if missing_files:
            example_missing = list(missing_files.keys())[0]
            self.logger.info(f"Example missing: {example_missing}")

        return missing_files

    def step4_simulate_download(self, missing_files: Dict[str, str]) -> List[Path]:
        """Step 4: Simulate downloading missing files.

        Args:
            missing_files: Files that need to be downloaded

        Returns:
            List of downloaded file paths
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 4: Simulate Download")
        self.logger.info("=" * 70)

        if not missing_files:
            self.logger.info("No files to download - archive is up to date")
            return []

        self.logger.info(
            f"Simulating download of {len(missing_files)} files to {self.tmp_dir}"
        )

        downloaded_files = []
        for filename in missing_files.keys():
            # Create dummy downloaded file
            tmp_file = self.tmp_dir / filename
            tmp_file.parent.mkdir(parents=True, exist_ok=True)

            # Create dummy uncompressed data
            with open(tmp_file, "wb") as f:
                f.write(b"Y" * 2048)  # Different from archive data

            downloaded_files.append(tmp_file)

        self.logger.info(f"Simulated download of {len(downloaded_files)} files")
        if downloaded_files:
            self.logger.info(f"Example: {downloaded_files[0].name}")

        return downloaded_files

    def step5a_archive_immediate_mode(self, downloaded_files: List[Path]):
        """Step 5a: Archive files using immediate mode (PolaRX5 style).

        Args:
            downloaded_files: List of files to archive
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 5a: Archive Files (IMMEDIATE MODE)")
        self.logger.info("=" * 70)

        if not downloaded_files:
            self.logger.info("No files to archive")
            return

        self.logger.info(f"Archiving {len(downloaded_files)} files immediately")

        with FileArchiver(logger=self.logger, mode=ArchiveMode.IMMEDIATE) as archiver:
            for tmp_file in downloaded_files:
                archive_path = self.archive_dir / f"{tmp_file.name}.gz"
                success = archiver.archive_file(
                    tmp_file, archive_path, compress=True, remove_tmp=True
                )

                if not success:
                    self.logger.error(f"Failed to archive: {tmp_file.name}")

            # Get statistics
            stats = archiver.get_statistics()
            self.logger.info(
                f"Archived: {stats['successful']}/{stats['total_files']} files"
            )
            self.logger.info(
                f"Average compression: {stats['average_compression_ratio']:.1f}%"
            )

    def step5b_archive_bulk_mode(self, downloaded_files: List[Path]):
        """Step 5b: Archive files using bulk mode (NetR9/NetRS style).

        Args:
            downloaded_files: List of files to archive
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 5b: Archive Files (BULK MODE)")
        self.logger.info("=" * 70)

        if not downloaded_files:
            self.logger.info("No files to archive")
            return

        # First, recreate files (they were removed in step5a)
        for tmp_file in downloaded_files:
            with open(tmp_file, "wb") as f:
                f.write(b"Z" * 2048)

        self.logger.info(f"Queuing {len(downloaded_files)} files for bulk archiving")

        with FileArchiver(logger=self.logger, mode=ArchiveMode.BULK) as archiver:
            for tmp_file in downloaded_files:
                archive_path = self.archive_dir / f"{tmp_file.name}.gz"
                archiver.archive_file(
                    tmp_file, archive_path, compress=True, remove_tmp=True
                )

            pending = archiver.get_pending_count()
            self.logger.info(f"Pending archives: {pending}")

            # Auto-flushes on context exit

        # Get statistics
        stats = archiver.get_statistics()
        self.logger.info(
            f"Archived: {stats['successful']}/{stats['total_files']} files"
        )
        self.logger.info(
            f"Average compression: {stats['average_compression_ratio']:.1f}%"
        )

    def step6_verify_final_state(self, files_dict: Dict[str, str]):
        """Step 6: Verify final archive state.

        Args:
            files_dict: Complete list of files that should exist
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("STEP 6: Verify Final State")
        self.logger.info("=" * 70)

        # Check all files exist in archive
        missing_count = 0
        present_count = 0

        for filename in files_dict.keys():
            archive_path = self.archive_dir / f"{filename}.gz"
            if archive_path.exists():
                present_count += 1
            else:
                missing_count += 1

        self.logger.info("Archive verification:")
        self.logger.info(f"  Present: {present_count}/{len(files_dict)} files")
        self.logger.info(f"  Missing: {missing_count}/{len(files_dict)} files")

        if missing_count == 0:
            self.logger.info("✅ All files successfully archived")
        else:
            self.logger.warning(f"⚠️  {missing_count} files still missing")

        # Check tmp directory is clean
        tmp_files = list(self.tmp_dir.glob("*"))
        if tmp_files:
            self.logger.warning(f"⚠️  {len(tmp_files)} files remaining in tmp directory")
        else:
            self.logger.info("✅ Tmp directory is clean")

    def run_complete_workflow(self, test_both_modes: bool = True):
        """Run complete receiver workflow simulation.

        Args:
            test_both_modes: If True, test both immediate and bulk archiving modes
        """
        self.logger.info("\n" + "🔧" * 35)
        self.logger.info(f"RECEIVER WORKFLOW INTEGRATION TEST: {self.station_id}")
        self.logger.info("🔧" * 35)

        # Step 1: Generate file list
        files_dict = self.step1_generate_file_list()

        # Step 2: Create some existing archives (50%)
        self.step2_create_existing_archives(files_dict, percentage=50)

        # Step 3: Validate archives
        missing_files = self.step3_validate_archives(files_dict)

        # Step 4: Simulate download
        downloaded_files = self.step4_simulate_download(missing_files)

        if test_both_modes:
            # Step 5a: Test immediate mode
            self.step5a_archive_immediate_mode(downloaded_files)

            # Step 5b: Test bulk mode
            self.step5b_archive_bulk_mode(downloaded_files)
        else:
            # Just test immediate mode
            self.step5a_archive_immediate_mode(downloaded_files)

        # Step 6: Verify final state
        self.step6_verify_final_state(files_dict)

        self.logger.info("\n" + "=" * 70)
        self.logger.info("WORKFLOW COMPLETE")
        self.logger.info("=" * 70)
        self.logger.info(f"Test directory: {self.temp_dir}")
        self.logger.info("Inspect the files manually to verify correctness")


def main():
    """Main entry point for manual testing."""
    parser = argparse.ArgumentParser(
        description="Integration test for receiver workflow with Phase 1 utilities"
    )
    parser.add_argument("--station", default="ELDC", help="Station ID (default: ELDC)")
    parser.add_argument(
        "--session",
        default="1Hz_1hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Session type (default: 1Hz_1hr)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days/hours back to simulate (default: 1)",
    )
    parser.add_argument(
        "--receiver-type",
        default="polarx5",
        choices=["polarx5", "netr9", "netrs", "leica"],
        help="Receiver type (default: polarx5)",
    )
    parser.add_argument(
        "--test-both-modes",
        action="store_true",
        help="Test both immediate and bulk archiving modes",
    )

    args = parser.parse_args()

    # Calculate time range
    end = datetime.now()
    if "1hr" in args.session:
        # Hourly: go back N hours
        start = end - timedelta(hours=args.days)
    else:
        # Daily: go back N days
        start = end - timedelta(days=args.days)

    # Run workflow
    simulator = ReceiverWorkflowSimulator(
        station_id=args.station,
        session=args.session,
        start=start,
        end=end,
        receiver_type=args.receiver_type,
    )

    simulator.run_complete_workflow(test_both_modes=args.test_both_modes)


if __name__ == "__main__":
    main()
