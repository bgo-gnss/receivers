#!/usr/bin/env python3
"""Build receiver_standalone_tools.zip: only what a user needs, nothing else.

The result is reproducible: the same files always give the same bytes, so a
stale zip shows up as a difference.

    python3 build_zip.py [OUTPUT.zip]
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP = "receiver_standalone_tools"
FILES = {
    "README.md": 0o644,
    "prx5.py": 0o755,
    "prx5.sh": 0o755,
    "stations.cfg.example": 0o644,
    "PolaRx5_TEST_config.txt": 0o644,
}
LICENSE = HERE.parent.parent / "LICENSE"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def build(output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        sources = {name: (HERE / name, mode) for name, mode in FILES.items()}
        sources["LICENSE"] = (LICENSE, 0o644)
        for name, (source, mode) in sorted(sources.items()):
            info = zipfile.ZipInfo(f"{TOP}/{name}", FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so that the mode below is honoured
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, source.read_bytes())


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / f"{TOP}.zip"
    build(target)
    print(target)
