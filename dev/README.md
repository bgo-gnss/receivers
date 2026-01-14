# Development & Exploration Scripts

This directory contains exploration scripts, prototypes, and development work
that hasn't yet been integrated into the main `receivers` package.

## Structure

```
dev/
├── health/              # Health extraction development
│   ├── polarx5/         # PolaRX5-specific exploration
│   ├── netr9/           # NetR9-specific exploration
│   └── netrs/           # NetRS-specific exploration
├── receivers/           # New receiver type development
└── notebooks/           # Jupyter notebooks for analysis
```

## Workflow

### 1. Exploration Phase
Create scripts in the appropriate `dev/` subdirectory to:
- Parse new message types or data formats
- Test extraction approaches
- Understand receiver-specific behavior

Example:
```bash
# Explore PolaRX5 health extraction
python dev/health/polarx5/extract_health_bin2asc.py
```

### 2. Validation Phase
Once an approach works:
- Add tests in `tests/`
- Document findings in the script or a separate `.md` file
- Note any receiver-specific quirks

### 3. Integration Phase
When ready to integrate:
1. Move logic to appropriate module in `src/receivers/`
2. Add proper error handling and logging
3. Update CLI if needed (`src/receivers/cli/`)
4. Add/update tests
5. Remove or archive the dev script

## Current Development

### Health Extraction
- `health/polarx5/extract_health_bin2asc.py` - Initial PolaRX5 health extraction using bin2asc
  - Status: Integrated into `src/receivers/health/`
  - Kept for reference and future development

### Adding New Receiver Types
When adding support for a new receiver type:
1. Create `dev/receivers/<type>/` directory
2. Explore connection, download, and health extraction
3. Document protocol quirks and message formats
4. Integrate into `src/receivers/<manufacturer>/`

## Running Dev Scripts

```bash
# From receivers/ directory
cd /path/to/receivers

# Set up Python path
export PYTHONPATH=../gtimes/src:../gps_parser/src:src

# Run exploration script
python dev/health/polarx5/my_exploration.py
```

## Notes
- Dev scripts may have rough edges - that's OK
- Document findings even if incomplete
- Consider adding Jupyter notebooks for complex analysis
