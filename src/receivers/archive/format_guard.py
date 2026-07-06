"""Guard against wrong-format bytes entering the archive.

``.Z`` means Unix compress LZW (magic ``1f 9d``) — the IGS/GAMIT convention
and the format of the entire legacy IMO archive. From the rek_new cutover
(DOY 172, 2026) to 2026-07-06 the converter wrote gzip bytes (``1f 8b``)
under ``.Z`` names — ~98k files, remediated by
``deployment/scripts/fix_gzip_z_to_lzw.sh``. The converter now fails loudly
instead of writing gzip-as-.Z, and this module is the second layer: a
chokepoint check at every path that ships files toward the archive
(archive-sync delta, re-rinex push), so no future producer can silently
recontaminate it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

LZW_MAGIC = b"\x1f\x9d"
GZIP_MAGIC = b"\x1f\x8b"

# Cap per-run log spam: a systemic regression produces thousands of bad files;
# the count tells the story, the first few tell the shape.
_LOG_SAMPLE = 5


def bad_z_format(path: Union[str, Path]) -> Optional[str]:
    """Reason string when ``path`` is a ``.Z`` file whose bytes are NOT LZW
    compress output; ``None`` when the file is fine — or is not a ``.Z`` file
    at all (other extensions are out of scope here).
    """
    p = Path(path)
    if p.suffix != ".Z":
        return None
    try:
        with open(p, "rb") as fh:
            magic = fh.read(2)
    except OSError as e:
        return f"unreadable ({e})"
    if magic == LZW_MAGIC:
        return None
    if magic == GZIP_MAGIC:
        return "gzip bytes under a .Z name (must be LZW compress)"
    return f"magic {magic.hex() or 'empty'} under a .Z name (must be LZW compress)"


def split_bad_z(
    paths: Iterable[str],
    logger: logging.Logger,
    *,
    root: str = "",
) -> Tuple[list, list]:
    """Split ``paths`` into (ok, bad) by :func:`bad_z_format`.

    ``paths`` may be absolute or relative; ``root`` is prepended for the
    on-disk check when given (paths themselves are returned untouched).
    Logs each bad file (first ``_LOG_SAMPLE``) plus a total, at ERROR — a
    bad-format file headed for the archive is a producer bug, never routine.
    """
    ok: list = []
    bad: list = []
    for rel in paths:
        full = str(Path(root) / rel) if root else rel
        reason = bad_z_format(full)
        if reason is None:
            ok.append(rel)
        else:
            bad.append(rel)
            if len(bad) <= _LOG_SAMPLE:
                logger.error("format guard: refusing %s — %s", rel, reason)
    if len(bad) > _LOG_SAMPLE:
        logger.error(
            "format guard: %d more .Z file(s) refused (same class)",
            len(bad) - _LOG_SAMPLE,
        )
    return ok, bad
