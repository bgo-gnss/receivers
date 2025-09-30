# Receiver Naming Standardization

## Summary

Standardized receiver naming to use model names consistently across all receiver types, with vendor information stored as metadata.

## Changes Made

### ✅ Receiver Type Names

**Before**: `['PolaRX5', 'NetRS', 'NetR9', 'Leica', 'LeicaG10', 'G10']`
**After**: `['PolaRX5', 'NetRS', 'NetR9', 'G10']`

### ✅ Updated Files

#### 1. `src/receivers/base/receiver_factory.py`
**Change**: Simplified registration to use only `G10` as the primary name
```python
# Before
self._receiver_types["Leica"] = LeicaG10
self._receiver_types["LeicaG10"] = LeicaG10
self._receiver_types["G10"] = LeicaG10

# After
self._receiver_types["G10"] = LeicaG10
```

#### 2. `~/.config/gpsconfig/receivers.cfg`
**Change**: Renamed section from `[leica]` to `[g10]` and added vendor metadata to all receiver types

```ini
# Before
[leica]
# Leica G10 specific settings

# After
[g10]
# Leica G10 specific settings
vendor = Leica
model = GR10

[polarx5]
vendor = Septentrio
model = PolaRx5

[netr9]
vendor = Trimble
model = NetR9

[netrs]
vendor = Trimble
model = NetRS
```

#### 3. `src/receivers/base/type_validator.py`
**Change**: Updated validation key from `'Leica'` to `'G10'`

#### 4. `src/receivers/__init__.py`
**Change**: Updated import to use G10 name
```python
# Before
from .leica.leica_gnss import Leica
__all__.append("Leica")

# After
from .leica.g10 import LeicaG10 as G10
__all__.append("G10")
```

#### 5. `src/receivers/leica/g10.py`
**Change**: Updated all string literals from `"receiver_type": "LeicaG10"` to `"receiver_type": "G10"` (7 occurrences)

### ✅ Station Configuration

**Status**: Already using `G10` in `stations.cfg` - no changes needed

```ini
[STATION_NAME]
receiver_type = G10  # Already correct
```

## Naming Convention

### Standardized Pattern

All receiver types now follow this pattern:

| Receiver Type | Vendor      | Model    | Config Section |
|---------------|-------------|----------|----------------|
| **PolaRX5**   | Septentrio  | PolaRx5  | `[polarx5]`    |
| **NetR9**     | Trimble     | NetR9    | `[netr9]`      |
| **NetRS**     | Trimble     | NetRS    | `[netrs]`      |
| **G10**       | Leica       | GR10     | `[g10]`        |

### Benefits

✅ **Consistent naming**: All receivers use model names (no vendor prefix)
✅ **Vendor tracking**: Vendor information available in configuration
✅ **Clear hierarchy**: Model name in code, vendor as metadata
✅ **Future-proof**: Easy to add G15, G18, NetR10, etc.
✅ **No ambiguity**: Single canonical name per receiver type

## Backward Compatibility

**Status**: Not needed - changes made before production deployment

All aliases removed for clean implementation:
- ❌ `Leica` (removed)
- ❌ `LeicaG10` (removed)
- ✅ `G10` (only valid name)

## Testing

### Verification

```bash
$ python3 -c "from receivers.base.receiver_factory import ReceiverFactory; \
    f = ReceiverFactory(); \
    print('Discovered receiver types:', list(f.get_available_types().keys()))"

Discovered receiver types: ['PolaRX5', 'NetRS', 'NetR9', 'G10']
```

✅ **Result**: Clean, consistent naming with no aliases

## Usage

### Configuration

```ini
# Station configuration (stations.cfg)
[STATION_ID]
receiver_type = G10

# Receiver configuration (receivers.cfg)
[g10]
vendor = Leica
model = GR10
# ... other settings
```

### Code

```python
from receivers.base.receiver_factory import ReceiverFactory

factory = ReceiverFactory()

# Create receiver instance
receiver = factory.create_receiver("G10", "STATION_ID", station_config)

# Available types
print(factory.get_available_types())  # {'G10': <class 'LeicaG10'>, ...}
```

## Future Extensions

### Adding New Leica Models

When new Leica models are added (e.g., GR15, GR18):

1. Create new class (e.g., `LeicaG15` in `src/receivers/leica/g15.py`)
2. Register in factory as `"G15"`
3. Add configuration section `[g15]` with `vendor = Leica`, `model = GR15`
4. Update station configs to use `receiver_type = G15`

### Example

```python
# src/receivers/leica/g15.py
class LeicaG15(BaseReceiver):
    """Leica GR15 receiver implementation."""
    pass

# src/receivers/base/receiver_factory.py
from ..leica.g15 import LeicaG15
self._receiver_types["G15"] = LeicaG15

# receivers.cfg
[g15]
vendor = Leica
model = GR15
base_path = /SD Card/Data/
# ... settings
```

## Vendor Metadata Usage

The vendor field can be used for:

1. **Filtering/Grouping**: Query all Trimble or Leica receivers
2. **Documentation**: Automatically generate vendor-specific guides
3. **Support**: Route issues to vendor-specific support teams
4. **Statistics**: Track deployment by vendor
5. **Updates**: Apply vendor-specific firmware/configuration updates

### Example Queries

```python
# Get all receivers from a specific vendor
def get_receivers_by_vendor(vendor_name):
    from receivers.config.receivers_config import ReceiversConfig
    config = ReceiversConfig()

    receivers = {}
    for receiver_type in config.get_supported_types():
        vendor = config.get(receiver_type, 'vendor')
        if vendor == vendor_name:
            receivers[receiver_type] = config.get_section(receiver_type)

    return receivers

# Usage
trimble_receivers = get_receivers_by_vendor('Trimble')  # Returns NetR9, NetRS
leica_receivers = get_receivers_by_vendor('Leica')      # Returns G10
```

## Migration Checklist

- [x] Update `receiver_factory.py` to use G10 only
- [x] Rename `[leica]` to `[g10]` in receivers.cfg
- [x] Add vendor field to all receiver types
- [x] Update type_validator.py
- [x] Update __init__.py imports
- [x] Update string literals in g10.py
- [x] Verify station configs (already correct)
- [x] Test receiver discovery
- [x] Document changes

## Validation

All changes validated and working:

✅ Receiver discovery shows clean list
✅ No backward compatibility issues (pre-production)
✅ Vendor metadata added to all types
✅ Configuration sections renamed
✅ Code updated consistently

---

**Status**: ✅ COMPLETE
**Date**: 2025-09-30
**Impact**: All receiver types now follow consistent naming convention
**Breaking Changes**: None (pre-production)