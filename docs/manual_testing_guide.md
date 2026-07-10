# Manual Testing Guide for Phase 1 Utilities

This guide walks you through manually testing the Phase 1 utilities to understand their behavior and verify correctness.

## Prerequisites

```bash
# Ensure you're in the receivers directory
cd /home/bgo/work/projects/gpslibrary/receivers

# Set PYTHONPATH
export PYTHONPATH=src:../gtimes/src:../gps_parser/src
```

## Test 1: Unit Tests (Quick Verification)

### Purpose
Verify that all utilities pass their unit tests.

### Run
```bash
# Run all Phase 1 tests
python3 -m pytest tests/test_archive_validator.py tests/test_time_processor.py tests/test_file_archiver.py -v

# Expected output: 72 passed in ~0.11s
```

### What to Look For
- ✅ All 72 tests should pass
- ⏱️  Total time should be < 0.5 seconds
- No warnings or errors

### Understanding the Results
Each test validates a specific behavior:
- **ArchiveValidator** (21 tests): File validation, compression checking, archive discovery
- **TimeParameterProcessor** (31 tests): Datetime parsing, timestamp normalization
- **FileArchiver** (20 tests): Immediate vs bulk archiving, compression, error handling

---

## Test 2: Performance Benchmarks

### Purpose
Understand performance characteristics and ensure no regression.

### Run
```bash
python3 tests/benchmarks/benchmark_utilities.py
```

### What to Expect
```
🔧🔧🔧🔧🔧🔧🔧...
PHASE 1 UTILITIES PERFORMANCE BENCHMARKS
...

ARCHIVE VALIDATOR BENCHMARKS
======================================================================
Benchmark: bench_validate_valid_file
======================================================================
Iterations:     10,000
Total Time:     0.5000 seconds
Average:        50.00 μs/op
Median:         48.00 μs/op
...
Throughput:     20,000 ops/sec
```

### What to Look For

#### ArchiveValidator Performance
- **File validation**: ~50-200 μs per file
  - *Interpretation*: Negligible overhead, validates 5,000-20,000 files/second
- **Archive discovery**: ~100-500 μs (checks 3 locations)
  - *Interpretation*: Fast enough for real-time use
- **Batch validation** (100 files): ~10-50 ms total
  - *Interpretation*: Can validate thousands of files per second

#### TimeParameterProcessor Performance
- **Datetime parsing**: ~10-50 μs per operation
  - *Interpretation*: Extremely fast, parsing is not a bottleneck
- **Timestamp normalization**: ~5-20 μs per timestamp
  - *Interpretation*: Can normalize millions per second
- **Full processing**: ~50-100 μs per call
  - *Interpretation*: No measurable overhead

#### FileArchiver Performance
- **Single file archiving**: ~5-20 ms per file
  - *Interpretation*: I/O and compression dominate, utility overhead negligible
  - *Note*: Most time is gzip compression (~90%)
- **10 files immediate**: ~50-200 ms total
  - *Interpretation*: Scales linearly
- **10 files bulk**: ~50-200 ms total
  - *Interpretation*: Similar to immediate (bulk just defers execution)

### Performance Guidelines
- ✅ **< 1ms**: Excellent - no overhead
- ✅ **1-10ms**: Good - acceptable for I/O operations
- ⚠️ **> 10ms per operation**: Review if needed

### Comparison with Existing Code
To compare with existing implementations:
1. Time the old `_validate_archived_file()` method in a receiver
2. Should be nearly identical (±10%)
3. Any difference is likely system noise

---

## Test 3: Integration Test (Workflow Simulation)

### Purpose
Simulate complete receiver workflow without actual network operations.

### Run Basic Test
```bash
# Test with 1 hour of data for ELDC station
python3 tests/integration/test_receiver_workflow.py \
    --station ELDC \
    --session 1Hz_1hr \
    --days 1
```

### What Happens
1. **Generate file list**: Creates 1 hour of filenames (1 file)
2. **Create existing archives**: Simulates 50% already archived
3. **Validate archives**: Uses ArchiveValidator to find missing files
4. **Simulate download**: Creates dummy files in tmp
5. **Archive (immediate mode)**: Archives files one-by-one
6. **Verify**: Checks all files are properly archived

### Expected Output
```
🔧🔧🔧...
RECEIVER WORKFLOW INTEGRATION TEST: ELDC
...

======================================================================
STEP 1: Generate File List
======================================================================
Generated 1 timestamps
Generated filenames for 1 files
Example: ELDC202509291500b.sbf -> /DSK1/SSN/1Hz_1hr/2025/39

======================================================================
STEP 2: Create Existing Archives (Simulation)
======================================================================
Creating 0 existing archive files (50%)
...

======================================================================
STEP 3: Validate Archives
======================================================================
Validated: 1 files
Found in archive: 0 files
Missing (need download): 1 files
Example missing: ELDC202509291500b.sbf

======================================================================
STEP 4: Simulate Download
======================================================================
Simulating download of 1 files to /tmp/.../tmp/ELDC
Simulated download of 1 files

======================================================================
STEP 5a: Archive Files (IMMEDIATE MODE)
======================================================================
Archiving 1 files immediately
...
Archived: 1/1 files
Average compression: 95.0%

======================================================================
STEP 6: Verify Final State
======================================================================
Archive verification:
  Present: 1/1 files
  Missing: 0/1 files
✅ All files successfully archived
✅ Tmp directory is clean

======================================================================
WORKFLOW COMPLETE
======================================================================
Test directory: /tmp/receiver_test_ELDC_...
Inspect the files manually to verify correctness
```

### Test with More Data
```bash
# Test with 24 hours (24 files for hourly session)
python3 tests/integration/test_receiver_workflow.py \
    --station ELDC \
    --session 1Hz_1hr \
    --days 24

# Test daily session (3 days = 3 files)
python3 tests/integration/test_receiver_workflow.py \
    --station ELDC \
    --session 15s_24hr \
    --days 3

# Test both immediate AND bulk modes
python3 tests/integration/test_receiver_workflow.py \
    --station ELDC \
    --session 1Hz_1hr \
    --days 4 \
    --test-both-modes
```

### Test Different Receiver Types
```bash
# Test NetR9 receiver
python3 tests/integration/test_receiver_workflow.py \
    --station SJUK \
    --receiver-type netr9 \
    --session 1Hz_1hr \
    --days 2

# Test NetRS receiver
python3 tests/integration/test_receiver_workflow.py \
    --station BLEI \
    --receiver-type netrs \
    --session 15s_24hr \
    --days 1
```

### What to Look For
1. **File counts match expectations**:
   - Hourly session: 1 file per hour
   - Daily session: 1 file per day

2. **Archive validation works**:
   - Finds existing files correctly
   - Identifies missing files

3. **Archiving modes behave correctly**:
   - Immediate: Archives one-by-one
   - Bulk: Queues then flushes

4. **Final state is clean**:
   - All files in archive
   - Tmp directory empty
   - Compression applied

### Manual Inspection
```bash
# After running test, inspect the test directory
TEST_DIR=$(ls -td /tmp/receiver_test_ELDC_* | head -1)

# Check archive structure
tree $TEST_DIR/archive/

# Check file sizes
ls -lh $TEST_DIR/archive/ELDC/1Hz_1hr/raw/

# Verify gzip compression
file $TEST_DIR/archive/ELDC/1Hz_1hr/raw/*.gz

# Check tmp is empty
ls $TEST_DIR/tmp/ELDC/
```

---

## Test 4: Real Receiver Data (Advanced)

### Purpose
Test with actual receiver configuration and time periods (no download).

### Prerequisites
- Station configured in stations.cfg
- receivers.cfg properly set up

### Run with Real Configuration
```bash
# This tests the utilities with real configuration but simulated data
python3 tests/integration/test_receiver_workflow.py \
    --station SKFC \
    --session 1Hz_1hr \
    --days 1
```

### Compare with Existing Receiver
To verify utilities produce identical results to existing code:

1. **Capture existing behavior**:
```bash
# Dry-run with existing receiver (if available)
receivers download SKFC -D 1 --session 1Hz_1hr --test-connection -v > existing_output.log
```

2. **Run integration test**:
```bash
python3 tests/integration/test_receiver_workflow.py --station SKFC --session 1Hz_1hr --days 1 > utility_output.log
```

3. **Compare**:
   - File counts should match
   - File names should match
   - Archive structure should match
   - Validation results should match

---

## Test 5: Interactive Python Testing

### Purpose
Explore utilities interactively to understand behavior.

### Start Python REPL
```bash
python3
```

### Test ArchiveValidator
```python
from pathlib import Path
from receivers.utils.archive_validator import ArchiveValidator

# Create validator
validator = ArchiveValidator()

# Check existing file
result = validator.validate_archived_file(Path("/some/file.sbf.gz"))
print(f"Valid: {result}")

# Get detailed report
report = validator.validate_with_detailed_report(Path("/some/file.sbf.gz"))
print(report)

# Find archive across locations
found, path, location = validator.find_existing_archive(
    "ELDC202509240000a.sbf",
    "/archive/path/ELDC202509240000a.sbf",
    Path("/tmp/downloads")
)
print(f"Found: {found}, Location: {location}")
```

### Test TimeParameterProcessor
```python
from datetime import datetime
from receivers.utils.time_processor import TimeParameterProcessor

# Create processor
processor = TimeParameterProcessor()

# Parse various formats
dt1 = processor.parse_datetime_flexible("2025-09-24")
dt2 = processor.parse_datetime_flexible("2025-09-24T15:30:00")
dt3 = processor.parse_datetime_flexible("20250924-1530")

print(f"Parsed: {dt1}, {dt2}, {dt3}")

# Normalize timestamps
dt = datetime(2025, 9, 24, 15, 30, 45)
normalized_hourly = processor.normalize_timestamp(dt, "1hr")
normalized_daily = processor.normalize_timestamp(dt, "24hr")

print(f"Hourly: {normalized_hourly}")  # 2025-09-24 15:00:00
print(f"Daily: {normalized_daily}")    # 2025-09-24 00:00:00

# Process time parameters
start, end = processor.process_time_parameters(
    "2025-09-24",
    "2025-09-25",
    "15s_24hr"
)
print(f"Range: {start} to {end}")
```

### Test FileArchiver
```python
from pathlib import Path
from receivers.utils.file_archiver import FileArchiver, ArchiveMode

# Create test file
tmp_file = Path("/tmp/test_file.sbf")
tmp_file.write_bytes(b'X' * 2048)

archive_path = Path("/tmp/test_file.sbf.gz")

# Test immediate mode
with FileArchiver(mode=ArchiveMode.IMMEDIATE) as archiver:
    success = archiver.archive_file(tmp_file, archive_path, compress=True, remove_tmp=False)
    print(f"Archived: {success}")
    print(f"Stats: {archiver.get_statistics()}")

# Test bulk mode
tmp_file.write_bytes(b'Y' * 2048)  # Recreate file

with FileArchiver(mode=ArchiveMode.BULK) as archiver:
    archiver.archive_file(tmp_file, archive_path, compress=True, remove_tmp=False)
    print(f"Pending: {archiver.get_pending_count()}")
    # Auto-flushes on exit

print(f"Final stats: {archiver.get_statistics()}")
```

---

## Interpreting Results

### Success Criteria
- ✅ All unit tests pass (72/72)
- ✅ Performance benchmarks show reasonable times
- ✅ Integration tests complete without errors
- ✅ File counts match expectations
- ✅ Archive validation works correctly
- ✅ Both archiving modes work

### Red Flags
- ❌ Any test failures
- ❌ Performance > 10ms per operation for non-I/O
- ❌ File counts don't match
- ❌ Missing or corrupted archives
- ❌ Tmp files not cleaned up

### Common Issues
1. **Import errors**: Check PYTHONPATH
2. **File not found**: Ensure working directory is correct
3. **Permission errors**: Check directory permissions
4. **Slow performance**: May be first run (system caching)

---

## Next Steps

After manual testing:

1. **Compare with existing code**: Run same operations with old implementation
2. **Test with real data**: Use actual receiver downloads (carefully!)
3. **Review logs**: Check for warnings or unexpected behavior
4. **Provide feedback**: Document any issues or suggestions

---

**Questions or Issues?**
- Check the implementation guide: `docs/phase1_implementation.md`
- Review usage guide: `docs/utilities_usage_guide.md`
- Inspect test code for detailed examples