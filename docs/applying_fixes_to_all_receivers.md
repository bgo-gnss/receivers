# Applying Fixes to All Receivers - Implementation Guide

**Date**: 2025-11-11
**Status**: Ready for implementation
**Approach**: Direct integration (pragmatic approach)

## Summary

This guide shows how to add both fixes directly to each receiver without full refactoring to download managers.

### Current Status

| Receiver | Fix #1 (Tmp Archiving) | Fix #2 (Retry Reconnection) | Notes |
|----------|------------------------|-----------------------------| ------|
| PolaRX5 | ✅ Complete | ✅ Complete | Already implemented in earlier work |
| NetR9 | ❌ Needs implementation | ❌ Needs implementation | Has own validation loop |
| NetR5 | ❌ Needs implementation | ❌ Needs implementation | Inherits from NetR9 |
| NetRS | ❌ Needs implementation | ❌ Needs implementation | Has own validation loop |
| G10 | ❌ Needs implementation | ❌ Needs implementation | Has own validation loop |

## Fix #1: Tmp Directory Archiving

### What It Does

Replaces manual validation loops with `batch_validate_archives()` which:
1. Checks archive directory (compressed and uncompressed)
2. **Checks tmp directory for unarchived files**
3. Returns files that need archiving separately from missing files
4. Allows automatic archiving of tmp files when `archive=True`

### Implementation Pattern

**Before (Current Code):**
```python
# Manual validation loop - only checks archive
missing_files_dict = {}
files_found_in_archive = 0

for filename, remote_dir in files_dict.items():
    archive_path = archive_files_dict.get(filename)
    if archive_path:
        archive_path_obj = Path(archive_path)
        if archive_path_obj.exists():
            if self._validate_archived_file(archive_path_obj):
                files_found_in_archive += 1
                continue
        # Check .gz version
        archive_path_gz = archive_path + ".gz"
        if Path(archive_path_gz).exists():
            if self._validate_archived_file(Path(archive_path_gz)):
                files_found_in_archive += 1
                continue

    missing_files_dict[filename] = remote_dir
```

**After (With Fix #1):**
```python
# Use Phase 1 batch validation - checks archive AND tmp
missing_files_dict, found_count, validated_count, files_in_tmp_dict = \
    self.archive_validator.batch_validate_archives(
        files_dict,
        archive_files_dict,
        tmp_dir_path  # NEW: Pass tmp directory
    )

# Archive files from tmp if found and archive flag is set
files_archived_from_tmp = 0
if files_in_tmp_dict and archive:
    self.logger.info(f"Archiving {len(files_in_tmp_dict)} files from tmp directory...")

    from ..utils.file_archiver import FileArchiver, ArchiveMode

    with FileArchiver(mode=ArchiveMode.BULK, logger=self.logger) as archiver:
        for filename, tmp_path in files_in_tmp_dict.items():
            archive_dest = archive_files_dict.get(filename)
            if archive_dest:
                archiver.archive_file(
                    tmp_path,
                    Path(archive_dest),
                    compress=False,  # Files already compressed
                    remove_tmp=True
                )

    stats = archiver.get_statistics()
    files_archived_from_tmp = stats['successful']
    self.logger.info(f"Archived {stats['successful']}/{len(files_in_tmp_dict)} files from tmp to archive")
```

### Where to Apply

**NetR9** (`src/receivers/trimble/netr9.py`):
- **Location**: Lines ~297-350 (in `download_data()` method)
- **Replace**: The manual validation loop
- **With**: `batch_validate_archives()` call + tmp archiving logic

**NetRS** (`src/receivers/trimble/netrs.py`):
- **Location**: Similar to NetR9 (check around line ~250-300)
- **Replace**: The manual validation loop
- **With**: Same pattern as NetR9

**NetR5** (`src/receivers/trimble/netr5.py`):
- **Status**: Inherits from NetR9, so fixing NetR9 fixes NetR5 too! ✅
- **No separate changes needed**

**G10** (`src/receivers/leica/g10.py`):
- **Location**: Check in `download_data()` method
- **Replace**: The manual validation loop
- **With**: `batch_validate_archives()` call + tmp archiving logic

## Fix #2: Retry with Reconnection

### What It Does

When a download times out:
1. Detects the timeout error
2. Closes the dead connection
3. Establishes a new connection
4. Retries the download with fresh connection

Without this fix, all retry attempts reuse the same frozen connection and fail immediately.

### Implementation Pattern for HTTP (NetR9, NetRS)

**Before (If they have retries):**
```python
# Retries without reconnection
for attempt in range(max_retries):
    try:
        download_result = self.http_downloader.download_file(...)
        break
    except TimeoutError:
        if attempt < max_retries - 1:
            time.sleep(delay)
            # Problem: reuses same connection!
```

**After (With Fix #2):**
```python
# Timeout/connection error patterns that need reconnection
timeout_patterns = [
    "timed out", "timeout", "cannot read from timed out",
    "connection reset", "broken pipe", "connection refused"
]

for attempt in range(max_retries):
    try:
        download_result = self.http_downloader.download_file(...)
        break
    except Exception as e:
        error_msg = str(e).lower()

        if attempt < max_retries - 1:
            self.logger.warning(f"⚠️  Download attempt {attempt + 1} failed: {e}")

            # Check if we need to reconnect (timeout/connection errors)
            if any(pattern in error_msg for pattern in timeout_patterns):
                self.logger.info("🔄 Reconnecting HTTP client...")
                # For HTTP: reinitialize the client or session
                self.http_downloader = NetR9HTTPDownloader(self.station_id, self.station_info)
                self.logger.info("✅ HTTP client reconnected")

            delay = initial_delay * (attempt + 1)
            self.logger.info(f"🔄 Retrying in {delay:.1f}s...")
            time.sleep(delay)
```

### Implementation Pattern for FTP (G10)

**Before (If they have retries):**
```python
# Retries without reconnection
for attempt in range(max_retries):
    try:
        ftp.retrbinary(f"RETR {file}", callback)
        break
    except TimeoutError:
        if attempt < max_retries - 1:
            time.sleep(delay)
            # Problem: reuses same frozen FTP connection!
```

**After (With Fix #2):**
```python
# Timeout/connection error patterns
timeout_patterns = [
    "timed out", "timeout", "cannot read from timed out",
    "connection reset", "broken pipe"
]

for attempt in range(max_retries):
    try:
        ftp.retrbinary(f"RETR {file}", callback)
        break
    except Exception as e:
        error_msg = str(e).lower()

        if attempt < max_retries - 1:
            self.logger.warning(f"⚠️  Download attempt {attempt + 1} failed: {e}")

            # Check if we need to reconnect
            if any(pattern in error_msg for pattern in timeout_patterns):
                self.logger.info("🔄 Closing dead FTP connection and reconnecting...")
                try:
                    ftp.quit()
                except:
                    pass  # Ignore errors closing dead connection

                # Reconnect
                ftp = self._establish_ftp_connection()  # Use your connection method
                self.logger.info("✅ FTP reconnected successfully")

            delay = initial_delay * (attempt + 1)
            self.logger.info(f"🔄 Retrying in {delay:.1f}s...")
            time.sleep(delay)
```

### Where to Apply

**NetR9** (`src/receivers/trimble/netr9.py`):
- **Search for**: Existing retry logic or download loops
- **Check**: `NetR9HTTPDownloader` download methods
- **Add**: Reconnection logic in retry blocks

**NetRS** (`src/receivers/trimble/netrs.py`):
- **Similar to**: NetR9 pattern
- **Check**: `NetRSHTTPDownloader` download methods

**NetR5** (`src/receivers/trimble/netr5.py`):
- **Status**: Inherits from NetR9, automatically gets the fix! ✅

**G10** (`src/receivers/leica/g10.py`):
- **Search for**: FTP download retry logic
- **Check**: `LeicaFTPDownloader` download methods
- **Add**: FTP reconnection in retry blocks

## Testing Checklist

After applying fixes to each receiver:

### Fix #1 Testing (Tmp Archiving)

```bash
# 1. Download files WITHOUT --archive flag (leaves files in tmp)
receivers download -D 5 -se status_1hr STATION

# 2. Verify files are in tmp
ls /tmp/gps_receivers/download/STATION/

# 3. Download again WITH --archive flag
receivers download -D 5 -se status_1hr STATION --archive

# 4. Verify:
# - Log message: "Found X files in tmp directory that need archiving"
# - Log message: "Archived X/X files from tmp to archive"
# - Tmp directory is now empty
ls /tmp/gps_receivers/download/STATION/  # Should be empty or only new files

# 5. Check archive directory
ls /tmp/gpsdata/2025/nov/STATION/status_1hr/raw/
```

### Fix #2 Testing (Retry Reconnection)

This requires a timeout to occur naturally or simulation:

```bash
# Test during network issues or with slow connection
receivers download -D 10 -se 1Hz_1hr STATION --archive -v

# Watch logs for:
# - "⚠️  Download attempt X failed: timed out"
# - "🔄 Closing dead connection and reconnecting..."
# - "✅ Reconnected successfully"
# - "🔄 Retrying in X.Xs..."
# - Successful download after reconnection
```

## Implementation Order

Recommended order to minimize risk:

1. **NetR9** - Most modern, well-tested
2. **NetRS** - Similar to NetR9
3. **G10** - Different protocol (FTP), independent
4. **Test all** - Verify fixes work

NetR5 automatically gets fixes from NetR9 (inheritance).

## Verification

After applying all fixes, verify:

```bash
# Test each receiver type
receivers download -D 2 -se status_1hr ISFS --archive    # PolaRX5 - already has fixes
receivers download -D 2 -se status_1hr MANA --archive    # NetR9
receivers download -D 2 -se status_1hr BLEI --archive    # NetRS
receivers download -D 2 -se status_1hr SKFC --archive    # G10

# Check logs for:
# - "Found X files in tmp directory that need archiving" (Fix #1)
# - "Archived X/X files from tmp to archive" (Fix #1)
# - No errors about frozen connections (Fix #2 working)
```

## Reference: PolaRX5 Implementation

PolaRX5 already has both fixes implemented. Use it as reference:

**Fix #1 (Tmp Archiving):**
- **File**: `src/receivers/septentrio/polarx5.py`
- **Method**: `_validate_files_phase1()` (lines ~557-618)
- **Pattern**: See lines 577-605 for archiving from tmp

**Fix #2 (Retry Reconnection):**
- **File**: `src/receivers/septentrio/polarx5.py`
- **Method**: `_download_with_immediate_retry()` (lines ~1238-1344)
- **Pattern**: See lines 1316-1333 for reconnection logic

## Benefits After Implementation

✅ **Fix #1 Benefits:**
- Files left in tmp from crashes/errors automatically recovered
- No manual cleanup needed
- Prevents disk space issues

✅ **Fix #2 Benefits:**
- Timeouts recover properly instead of wasting all retries
- Better success rate on unreliable networks
- Mobile/remote stations more reliable

## Rollback Plan

If issues occur after applying fixes:

1. **Revert changes** using git
2. **Test original code** works
3. **Review logs** for specific error
4. **Apply fix more carefully** with additional logging

## Next Steps

1. Apply Fix #1 to NetR9 (most important, affects 2 receiver types via inheritance)
2. Test with MANA station
3. Apply Fix #1 to NetRS
4. Test with BLEI station
5. Apply Fix #1 to G10
6. Test with SKFC station
7. Add Fix #2 to all three (NetR9, NetRS, G10)
8. Comprehensive testing

---

**Total Estimated Time**: 2-3 hours for all receivers + testing
**Risk Level**: Low - Changes are isolated and well-tested in PolaRX5
**Rollback**: Easy - git revert if needed
