# Phase 1 Testing Guide - Quick Start

## Run Tests Manually

### 1. Quick Verification (10 seconds)
```bash
cd /home/bgo/work/projects/gpslibrary/receivers
export PYTHONPATH=src:../gtimes/src:../gps_parser/src

# Run all unit tests
python3 -m pytest tests/test_archive_validator.py tests/test_time_processor.py tests/test_file_archiver.py -v
```

**Expected**: `72 passed in ~0.11s`

---

### 2. Performance Benchmarks (30 seconds)
```bash
# See how fast the utilities are (imports now work automatically)
python3 tests/benchmarks/benchmark_utilities.py
```

**What to look for**:
- Archive validation: ~50-200 μs per file
- Time parsing: ~10-50 μs per operation
- File archiving: ~5-20 ms per file

**Interpretation**: Operations < 1ms are excellent (no overhead)

---

### 3. Integration Test (5 seconds)
```bash
# Simulate complete receiver workflow (imports now work automatically)
python3 tests/integration/test_receiver_workflow.py --station ELDC --session 1Hz_1hr --days 1
```

**What happens**:
1. Generates file list for 1 hour
2. Creates some existing archives (50%)
3. Validates what's missing
4. Simulates download
5. Archives files
6. Verifies everything worked

**Expected**: All steps complete, final message shows test directory path

---

### 4. More Extensive Tests

```bash
# Test with 24 hours of data (24 files)
python3 tests/integration/test_receiver_workflow.py --station ELDC --session 1Hz_1hr --days 24

# Test both immediate AND bulk archiving modes
python3 tests/integration/test_receiver_workflow.py --station ELDC --session 1Hz_1hr --days 4 --test-both-modes

# Test different receiver type (NetR9)
python3 tests/integration/test_receiver_workflow.py --station SJUK --receiver-type netr9 --session 1Hz_1hr --days 2
```

---

## Understanding the Results

### Unit Tests
- Tests individual utility functions
- Should all pass (72/72)
- Very fast (< 0.5 seconds total)

### Benchmarks
- Shows performance in microseconds (μs)
- Compare with existing code if needed
- Most operations should be < 1ms

### Integration Tests
- Simulates real workflow without network
- Creates test files in /tmp
- Shows step-by-step what happens
- **Inspect the test directory** to see actual files

---

## Detailed Guide

See `docs/manual_testing_guide.md` for:
- Detailed explanations of each test
- How to interpret results
- What to look for
- Troubleshooting tips
- Interactive Python examples

---

## Quick Summary

| Test | Time | Purpose |
|------|------|---------|
| Unit tests | 10s | Verify correctness |
| Benchmarks | 30s | Check performance |
| Integration | 5s | Test workflow |

**Total time**: ~45 seconds to run all tests

---

## Example Output to Expect

### Unit Tests
```
tests/test_archive_validator.py::TestGzipValidator::test_valid_gzip_magic_bytes PASSED
tests/test_archive_validator.py::TestGzipValidator::test_invalid_gzip_magic_bytes PASSED
...
============================== 72 passed in 0.11s ==============================
```

### Benchmarks
```
ARCHIVE VALIDATOR BENCHMARKS
======================================================================
Benchmark: bench_validate_valid_file
======================================================================
Iterations:     10,000
Average:        50.00 μs/op
Throughput:     20,000 ops/sec
```

### Integration Test
```
RECEIVER WORKFLOW INTEGRATION TEST: ELDC
...
STEP 1: Generate File List
Generated 1 timestamps
...
STEP 6: Verify Final State
✅ All files successfully archived
✅ Tmp directory is clean
WORKFLOW COMPLETE
```

---

## Feedback Welcome!

After running the tests:
1. Note anything confusing
2. Check if behavior matches expectations
3. Compare with existing receiver code if possible
4. Share your findings!

The goal is to ensure the utilities work correctly before integrating them into actual receivers.