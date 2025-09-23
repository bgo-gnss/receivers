# Receivers Package Diagrams

This directory contains architectural diagrams and design documentation for the receivers package.

## Architecture Diagrams

### DOWNLOAD_FLOW_DIAGRAM.md
Complete flow diagram showing the receivers download subcommand architecture, from CLI input to actual file download. Highlights integration between receivers, gps_parser, and gtimes packages.

**View with:**
```bash
# Multi-page PDF viewer (recommended)
mermaid-multi DOWNLOAD_FLOW_DIAGRAM.md

# Quick inline preview in Neovim
nvim DOWNLOAD_FLOW_DIAGRAM.md
# (position cursor on closing ``` to see diagram)
```

## Generated Files

When you run `mermaid-multi`, the following files are generated:
- `diagrams-1.pdf` - Individual diagram PDF
- `diagrams_combined.pdf` - Multi-page PDF (if multiple diagrams)
- `diagram_hq-1.png` - High-resolution PNG (if using `mermaid-hq`)

These generated files are in `.gitignore` to keep the repository clean.

## Tools Used

- **snacks.nvim** - Inline Mermaid rendering in Neovim
- **mermaid-cli (mmdc)** - Command-line diagram generation
- **zathura** - CLI PDF viewer for studying diagrams
- **pdfunite** - Combining multiple diagrams into single PDF

## Adding New Diagrams

1. Create `.md` file with mermaid code blocks
2. Use `mermaid-multi filename.md` to generate and view
3. Add description to this README