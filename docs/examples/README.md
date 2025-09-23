# Examples and Sample Data

This directory contains example scripts, sample data, and usage demonstrations for the receivers package.

## Files

- **jord.csv** - Sample GPS station data for testing and examples
- **extract_health_bin2asc.py** - Example script for SBF health data extraction

## Usage Examples

### Health Data Extraction

```bash
# Extract health data from SBF files
python docs/examples/extract_health_bin2asc.py --station ORFC

# Extract from specific session type
python docs/examples/extract_health_bin2asc.py --station ELDC --session status_1hr
```

### Sample Data

The `jord.csv` file contains sample station configuration data that can be used for:
- Testing receiver type detection
- Validating configuration parsing
- Development and debugging

## Integration

These examples demonstrate real-world usage patterns that are integrated into the main receivers package functionality.