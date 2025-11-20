# TODO: Contribute Time Range Fix to gtimes Package

## Overview
The `receivers` package currently implements correct "previous complete period" logic for time-series data processing that should be contributed back to the `gtimes` package as general-purpose utilities.

## Background

### The Bug in gtimes
Located in `gtimes/src/gtimes/timefunc.py:492-505`, the `datepathlist()` function has a documented bug:
- **Issue**: Hardcodes `delta=timedelta(days=1)` regardless of `lfrequency` parameter
- **Impact**: Fails for hourly data (generates daily intervals instead of hourly)
- **Missing logic**: No "previous complete period" handling (at 22:41, should end at 21:00, not 22:00)

### Our Working Implementation
Located in `receivers/src/receivers/utils/time_utils.py`, we have:
1. `calculate_download_time_range()` - Correct "previous complete period" logic
2. `generate_download_datetimes()` - Generates datetime lists with proper frequency handling
3. `get_session_frequency()` - Session type to frequency string mapping

## Contribution Steps

### Phase 1: Documentation and Issue Creation
- [ ] Read `gtimes/.github/CONTRIBUTING.md` to understand contribution process
- [ ] Review `gtimes/.github/PULL_REQUEST_TEMPLATE.md` for PR requirements
- [ ] Create GitHub issue in gtimes repo describing:
  - The bug at lines 492-505 in `timefunc.py`
  - Impact on hourly time-series data processing
  - Missing "previous complete period" logic
  - Reference to working implementation in receivers package
  - Use cases: GPS data processing, time-series downloads, automated scheduling

### Phase 2: Design the API
Design how these utilities should be exposed in gtimes:

**Option A: Extend datepathlist()** (minimal change)
```python
def datepathlist(stringformat, lfrequency, starttime=None, endtime=None,
                 datelist=[], closed="left",
                 complete_periods_only=True):  # NEW parameter
    """
    Args:
        complete_periods_only: If True, adjust endtime to last complete period.
            For hourly data at 22:41, ends at 21:00 (not 22:00).
            For daily data, ends at 00:00 today (not tomorrow).
    """
```

**Option B: New utility functions** (cleaner, more explicit)
```python
def calculate_time_range(frequency: str, lookback_periods: int,
                        reference_time: datetime = None,
                        complete_periods_only: bool = True) -> Tuple[datetime, datetime]:
    """Calculate time range for time-series data processing.

    Args:
        frequency: Time frequency ('1H', '1D', '8H', etc.)
        lookback_periods: Number of periods to look back
        reference_time: Reference time (default: now)
        complete_periods_only: If True, end at last complete period

    Returns:
        (start_time, end_time) tuple
    """

def generate_datetime_list(frequency: str, lookback_periods: int = None,
                          start_time: datetime = None, end_time: datetime = None,
                          reverse_chronological: bool = False) -> List[datetime]:
    """Generate list of datetimes for time-series processing.

    Args:
        frequency: Time frequency ('1H', '1D', etc.)
        lookback_periods: Number of periods (if start_time not provided)
        start_time: Explicit start time
        end_time: Explicit end time
        reverse_chronological: If True, return newest-first order

    Returns:
        List of datetime objects
    """
```

**Option C: Hybrid approach** (recommended)
- Fix the bug in existing `datepathlist()` to respect `lfrequency`
- Add new utility functions for common use cases
- Keep backward compatibility with existing API

### Phase 3: Implementation
- [ ] Create feature branch in gtimes: `feature/fix-hourly-frequency-bug`
- [ ] Fix the bug in `datepathlist()`:
  - Parse `lfrequency` string to extract number and unit
  - Use appropriate `timedelta` based on frequency (hours/days/etc.)
  - Add `complete_periods_only` parameter with default `True`
- [ ] Add new utility functions (if Option B or C chosen):
  - `calculate_time_range()`
  - `generate_datetime_list()`
  - Helper to parse frequency strings: `parse_frequency("1H") -> (1, "hours")`
- [ ] Update existing tests in `gtimes/tests/`
- [ ] Add comprehensive new tests:
  - Hourly frequency handling
  - Daily frequency handling
  - 8-hour frequency handling
  - Complete periods logic
  - Edge cases (midnight, year boundaries, leap years)

### Phase 4: Testing
- [ ] Run full gtimes test suite: `pytest gtimes/tests/ -v`
- [ ] Test against receivers package use cases:
  - Hourly downloads (`1Hz_1hr`, `status_1hr`)
  - Daily downloads (`15s_24hr`)
  - Time range calculations match current behavior
- [ ] Test backward compatibility:
  - Existing `datepathlist()` calls still work
  - No breaking changes to API

### Phase 5: Documentation
- [ ] Update function docstrings with new parameters
- [ ] Add usage examples in docstrings
- [ ] Update gtimes README if needed
- [ ] Add entry to CHANGELOG
- [ ] Document the bug fix and new features

### Phase 6: Pull Request
- [ ] Create PR following template in `.github/PULL_REQUEST_TEMPLATE.md`
- [ ] Reference the GitHub issue created in Phase 1
- [ ] Include:
  - Clear description of bug and fix
  - Examples showing before/after behavior
  - Test results showing all tests pass
  - Documentation of new features
  - Migration guide if any API changes

### Phase 7: Integration Back to receivers
Once gtimes PR is merged and released:
- [ ] Update receivers to use gtimes utilities instead of local implementation
- [ ] Keep `time_utils.py` as compatibility layer initially
- [ ] Add gtimes minimum version requirement to receivers `pyproject.toml`
- [ ] Update receivers documentation to reference gtimes
- [ ] Deprecate local implementations with warnings pointing to gtimes
- [ ] Eventually remove local implementation after transition period

## Reference Materials

### Current Implementation
- **receivers**: `src/receivers/utils/time_utils.py` (lines 28-154)
  - `calculate_download_time_range()`
  - `generate_download_datetimes()`
  - `get_session_frequency()`

### Bug Location
- **gtimes**: `src/gtimes/timefunc.py:492-505`
  - Bug comments at lines 492-505
  - Affected function: `datepathlist()`

### Documentation
- **gtimes**: `.github/CONTRIBUTING.md`
- **gtimes**: `.github/PULL_REQUEST_TEMPLATE.md`
- **receivers**: `docs/phase2_complete.md` - Background on time processing

## Benefits of Contribution

1. **For gtimes users**:
   - Fix long-standing bug affecting hourly time-series data
   - Add general-purpose time range utilities
   - Better support for automated data processing workflows

2. **For receivers package**:
   - Remove duplicate code
   - Maintain consistency with gtimes API
   - Leverage gtimes testing and maintenance
   - Single source of truth for time calculations

3. **For GPS community**:
   - Consistent time handling across packages
   - Better documentation and examples
   - Reduced maintenance burden

## Timeline Estimate
- Phase 1 (Documentation): 2 hours
- Phase 2 (Design): 2-3 hours (requires discussion with gtimes maintainers)
- Phase 3 (Implementation): 4-6 hours
- Phase 4 (Testing): 3-4 hours
- Phase 5 (Documentation): 2-3 hours
- Phase 6 (PR and review): Variable (depends on maintainer feedback)
- Phase 7 (Integration): 2-3 hours

**Total estimated effort**: 15-21 hours of active work + review time

## Notes
- This is a **breaking fix** - existing code relying on the buggy behavior may need updates
- Consider semantic versioning: This should be a MINOR version bump (new features, bug fix)
- Add deprecation warnings for any changed behavior to ease transition
- The receivers package will maintain local implementation until gtimes integration is complete

---

**Created**: 2025-11-13
**Updated**: 2025-11-13
**Status**: ✅ Phase 3 Complete (Implementation and Testing) - Ready for PR
**Priority**: Medium (working local implementation exists, but contribution benefits the community)
**Dependencies**: None (gtimes is actively maintained)

## Progress Update (2025-11-13)

### ✅ Completed
- **Phase 1**: Documentation and issue creation (skipped - went straight to implementation)
- **Phase 2**: API design - chose Option C (hybrid approach)
  - Fixed bug in existing `datepathlist()` to respect `lfrequency`
  - Added `_parse_frequency_to_timedelta()` helper function
  - Maintained backward compatibility
- **Phase 3**: Implementation
  - Created feature branch: `fix/datepathlist-hourly-frequency-bug`
  - Fixed hardcoded `delta=timedelta(days=1)` bug
  - Added frequency parsing for H, D, W, M, S units (case-insensitive)
  - Supports formats: "1H", "H", "3H", "1D", "D", etc.
- **Phase 4**: Testing
  - Added 13 comprehensive tests (all passing)
  - Verified existing tests still pass (7 datepathlist tests)
  - Type hints validated with mypy
  - Committed to feature branch with detailed commit message

### Branch Information
- **Branch**: `fix/datepathlist-hourly-frequency-bug`
- **Commit**: `a357f28` - "fix: respect lfrequency parameter in datepathlist for hourly data"
- **Location**: `/home/bgo/work/projects/gps/gpslibrary_new/gtimes`

### Next Steps
- **Phase 5**: Documentation updates (if needed for PR)
- **Phase 6**: Create pull request to gtimes main branch
- **Phase 7**: After merge, integrate back to receivers package
