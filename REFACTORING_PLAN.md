# Configuration Refactoring Plan

## Problem Statement

Currently, configuration management code is duplicated across receiver implementations:
- **PolaRX5**: Has complex `_get_data_prepath()`, `_get_system_path()`, `_get_session_map()` methods
- **NetR9/NetRS**: Use hardcoded paths and missing configuration utilities
- **Leica**: Also missing these utilities

## Solution: Shared ConfigManager

Created `receivers/base/config_manager.py` to centralize configuration access.

## Refactoring Changes Needed

### 1. PolaRX5 Refactoring (Major)

**Before** (current):
```python
class PolaRX5(BaseReceiver):
    def __init__(self, station_id, station_config):
        super().__init__(station_id, station_config)
        self.data_prepath = self._get_data_prepath()
        self.session_map = self._get_session_map()
    
    def _get_data_prepath(self):
        """65 lines of gps_parser integration + fallbacks"""
        # Complex implementation...
    
    def _get_system_path(self, path_name):
        """45 lines of system path resolution"""
        # Complex implementation...
    
    def _get_session_map(self):
        """40 lines of session configuration"""
        # Complex implementation...
```

**After** (refactored):
```python
class PolaRX5(BaseReceiver):
    def __init__(self, station_id, station_config):
        super().__init__(station_id, station_config)  # ConfigManager initialized here
        # data_prepath already available via self.data_prepath
        self.session_map = self.config_manager.get_session_map()
    
    # Remove _get_data_prepath(), _get_system_path(), _get_session_map()
    # Use self.config_manager.get_system_path("sbf2rin_path") etc.
```

### 2. NetR9/NetRS Enhancement (Minor)

**Before** (current):
```python
class NetR9(BaseReceiver):
    def __init__(self, station_id, station_info):
        super().__init__(station_id, station_info)
        # Hardcoded fallback
        self.data_prepath = station_info.get("data_prepath", "/mnt_data/rawgpsdata/%Y/%b/")
```

**After** (enhanced):
```python
class NetR9(BaseReceiver):
    def __init__(self, station_id, station_info):
        super().__init__(station_id, station_info)  # Gets proper data_prepath
        # self.data_prepath now uses proper gps_parser configuration
        
        # Can also access:
        timeout_category = self.config_manager.get_timeout_config(station_id, ip)
        ftp_mode = self.config_manager.get_ftp_mode(station_id, ip)
```

### 3. Leica Enhancement (Minor)

**Before** (current):
```python
class Leica(BaseReceiver):
    def __init__(self, station_id, station_config):
        super().__init__(station_id, station_config)
        # No configuration utilities available
```

**After** (enhanced):
```python
class Leica(BaseReceiver):
    def __init__(self, station_id, station_config):
        super().__init__(station_id, station_config)  # Gets ConfigManager
        # Can now use:
        # - self.data_prepath (proper configuration)
        # - self.config_manager.get_system_path("leica_tool_path")
        # - self.config_manager.get_timeout_config(station_id, ip)
```

## Benefits of Refactoring

### 1. Code Reduction
- **Remove ~150 lines** of duplicated configuration code from PolaRX5
- **Standardize configuration** across all receiver types
- **Single source of truth** for configuration logic

### 2. Enhanced Functionality
- **NetR9/NetRS**: Gain proper gps_parser integration (vs hardcoded paths)
- **Leica**: Gain configuration utilities it currently lacks
- **All receivers**: Consistent timeout and FTP mode handling

### 3. Maintainability  
- **Centralized configuration logic** - fix once, benefits all receivers
- **Consistent fallback strategies** across receiver types
- **Easier testing** - mock ConfigManager instead of multiple methods

### 4. Future Extensibility
- **Easy to add new configuration sources** (environment, database, etc.)
- **Simple to add new configuration types** (add method to ConfigManager)
- **Consistent interface** for all receiver implementations

## Implementation Steps

### Phase 1: Create Infrastructure ✅
- [x] Create `config_manager.py` with ConfigManager class
- [x] Update BaseReceiver to use ConfigManager
- [x] Demonstrate NetR9 usage

### Phase 2: Refactor PolaRX5 ✅ **COMPLETED**
- [x] Remove `_get_data_prepath()`, `_get_system_path()`, `_get_session_map()` methods
- [x] Update PolaRX5 to use `self.config_manager.get_session_map()`
- [x] Update tool path calls to use `self.config_manager.get_system_path()`
- [x] Test PolaRX5 functionality is preserved

### Phase 3: Enhance NetR9/NetRS (Optional)
- [ ] Remove hardcoded data_prepath fallback (now uses proper config)
- [ ] Add timeout category integration via ConfigManager
- [ ] Add FTP mode configuration via ConfigManager

### Phase 4: Enhance Leica (Optional) 
- [ ] Add proper configuration utilities for future Leica protocol work
- [ ] Use proper data_prepath instead of default

## Code Impact

### Files Modified ✅
- `receivers/base/config_manager.py` - Shared configuration utilities ✅ **CREATED**
- `receivers/base/receiver.py` - Added ConfigManager integration ✅ **UPDATED** 
- `receivers/septentrio/polarx5.py` - Major refactoring ✅ **COMPLETED** (removed 102 lines)
- `receivers/trimble/netr9.py` - Minor cleanup ✅ **UPDATED** (1 line)  
- `receivers/trimble/netrs.py` - Minor cleanup (1-2 lines) - Optional
- `receivers/leica/leica_gnss.py` - Minor enhancement (optional)

### Results Achieved ✅
- **102 lines removed** from PolaRX5 duplicated configuration code
- **All receivers** now use shared ConfigManager for consistent configuration
- **Zero functionality lost** - all tests pass, health checks work
- **Backward compatible** - all existing APIs preserved

## Risk Assessment

### Low Risk
- BaseReceiver integration is non-breaking (adds functionality)
- ConfigManager has proper fallbacks for all scenarios
- Existing hardcoded paths preserved as fallbacks

### Testing Strategy
- Test each receiver type after refactoring
- Ensure existing functionality preserved
- Validate proper gps_parser integration

## Recommendation

**Proceed with Phase 2** (PolaRX5 refactoring) as it provides the biggest benefit:
- Removes most duplicated code (~150 lines)
- Makes PolaRX5 consistent with other receivers
- Easiest to test and validate

The refactoring is **backward compatible** and provides **immediate benefits** without breaking existing functionality.