"""Binary raw-file format identification + decoded-date validation.

Born from the 2026-07-06 ``.atc`` findings: the archive's ``.atc`` extension
covers THREE different raw formats (Ashtech µZ-12 "U-file", Ashtech Z-XII3
"R-file", and Septentrio PolaRx2 SBF mislabeled ``.atc``), and one batch
(RHOF ``2000/``+``2001/``) is filed a decade away from the data it contains.
Two lessons encoded here:

* **Classify by magic bytes, never by extension** — the wrong decoder either
  segfaults (``teqc -ash r`` on a U-file) or silently emits 0 bytes
  (``-ash u`` on anything else). Extension is only a last-resort hint for
  formats without a printable magic (Trimble .T02/.T00).
* **The filename's date is a claim, not a fact** — the receiver's embedded
  GPS week is authoritative. ``decoded_span()`` reads the true first/last
  epoch via ``teqc +meta`` so callers can check it against the filename
  (the misfiled RHOF batches pass every coordinate/identity check — only the
  decoded-date-vs-filename comparison catches them).
"""

from __future__ import annotations

import gzip
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.archive.raw_format")

# Format identifiers (plain strings so they serialize/log cleanly).
SBF = "sbf"
ASHTECH_U = "ashtech_u"
ASHTECH_R = "ashtech_r"
TRIMBLE = "trimble"
UNKNOWN = "unknown"

# teqc decoder flags per format — the dispatch table the magic check feeds.
# Trimble .T02 is NOT directly teqc-readable (needs runpkr00 first) → no entry.
_TEQC_FLAGS = {
    SBF: ["-sep", "sbf"],
    ASHTECH_U: ["-ash", "u"],
    ASHTECH_R: ["-ash", "r"],
}

# English month abbreviations, index 1-12 — the archive's directory names.
# Explicit (not strftime %b) so a non-English locale can't corrupt paths.
MONTH_DIRS = (
    "",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def read_head(path: Path, n: int = 64) -> bytes:
    """First ``n`` bytes of ``path``, transparently decompressing gzip."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(2)
        fh.seek(0)
        if head == b"\x1f\x8b":
            with gzip.open(fh) as gz:
                return gz.read(n)
        return fh.read(n)


def classify_raw(path: Optional[Path] = None, head: Optional[bytes] = None) -> str:
    """Identify a raw file's format from its content (magic bytes).

    Extension participates only as a fallback hint for Trimble containers,
    which carry no printable magic. Everything else is content-only.
    """
    if head is None:
        if path is None:
            raise ValueError("classify_raw needs a path or a head")
        try:
            head = read_head(Path(path))
        except OSError as exc:
            logger.warning("cannot read %s: %s", path, exc)
            return UNKNOWN
    if head[:2] == b"$@":
        return SBF
    if head[4:8] == b"BHDR":
        return ASHTECH_U
    if head[:4] == b"Z-12":
        return ASHTECH_R
    if path is not None:
        suffixes = "".join(Path(path).suffixes).lower()
        if ".t02" in suffixes or ".t00" in suffixes:
            return TRIMBLE
    return UNKNOWN


def decoded_span(
    path: Path, fmt: str, *, timeout: int = 120
) -> Optional[tuple[datetime, datetime]]:
    """True (first, last) epoch of a raw file, from ``teqc +meta``.

    Returns None when the format has no teqc decoder (Trimble — needs
    runpkr00 first) or the decode fails. The dates come from the receiver's
    embedded GPS week — trust them over the filename, always.
    """
    flags = _TEQC_FLAGS.get(fmt)
    if flags is None:
        return None
    from ..dissemination.convert import resolve_tool  # avoid import cycle

    try:
        teqc = resolve_tool("teqc")
    except Exception as exc:  # noqa: BLE001 - tool resolution is environment
        logger.warning("teqc not available: %s", exc)
        return None
    proc = subprocess.run(
        [teqc, *flags, "+meta", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    start = end = None
    for line in proc.stdout.splitlines():
        if line.startswith("start date & time:"):
            start = _parse_meta_dt(line)
        elif line.startswith("final date & time:"):
            end = _parse_meta_dt(line)
    if start is None or end is None:
        logger.warning(
            "teqc +meta gave no epoch span for %s (fmt=%s, rc=%s)",
            path,
            fmt,
            proc.returncode,
        )
        return None
    return start, end


def _parse_meta_dt(line: str) -> Optional[datetime]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", line)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, s)


# Archive raw filename: STATION + YYYYMMDDHHMM + session letter + extension,
# e.g. RHOF201004020000a.atc / HUSM202606270000a.sbf.gz
_RAW_NAME_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})"
    r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})(?P<h>\d{2})(?P<mi>\d{2})"
    r"(?P<letter>[a-z])\.(?P<ext>[A-Za-z0-9.]+)$"
)


@dataclass(frozen=True)
class ParsedRawName:
    station: str
    claimed: datetime  # the date+time the FILENAME claims
    session_letter: str
    ext: str


def parse_raw_name(name: str) -> Optional[ParsedRawName]:
    m = _RAW_NAME_RE.match(name)
    if not m:
        return None
    try:
        claimed = datetime(
            int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"])
        )
    except ValueError:
        return None
    return ParsedRawName(m["sta"], claimed, m["letter"], m["ext"])


def build_raw_name(parsed: ParsedRawName, true_start: datetime) -> str:
    """The corrected filename: same station/letter/ext, the DECODED date."""
    return (
        f"{parsed.station}{true_start:%Y%m%d%H%M}{parsed.session_letter}.{parsed.ext}"
    )
