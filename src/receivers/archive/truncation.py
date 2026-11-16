"""Deterministic detection and repair decisions for truncated raw files.

A raw ``.gz`` whose stream ends early looks present to every cheap check the
pipeline makes (existence, size, the two gzip magic bytes), so it is never
fetched again and its RINEX conversion fails on every attempt. This module is
the pure core of ``receivers archive-truncation``:

* :func:`probe_gzip` reads a file once, streaming, and says whether its gzip
  stream is COMPLETE, TRUNCATED or CORRUPT, together with the hashes the rest of
  the tool keys on.
* :func:`sbf_complete_prefix` walks Septentrio SBF blocks from offset 0 and
  returns the end of the last CRC-valid block. On real receiver logs it reaches
  the exact end of every complete file; on a truncated one it stops at the
  partly written final block.
* :func:`salvage` cuts the readable prefix of a truncated SBF file at that
  block boundary and recompresses it the same way every time.
* :func:`decide` is the decision ladder: a pure function of the facts recorded
  in a plan, so the same facts always give the same action.

Determinism is defined on the DECOMPRESSED bytes (``content_sha256``): the
gzip framing is written with ``mtime=0`` and no file name, but the compressed
bytes can still differ between zlib builds, so they are never the identity.

Nothing here touches the network, a database or the archive. Scanning lives in
:mod:`receivers.archive.truncation_scan`, applying in
:mod:`receivers.archive.truncation_apply`.
"""

from __future__ import annotations

import binascii
import gzip
import hashlib
import zlib
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional, Union

FileRef = Union[str, Path]

_CHUNK = 1 << 20
_GZIP_MAGIC = b"\x1f\x8b"
SBF_SYNC = b"$@"

#: Hard ceiling on what :func:`salvage` holds in memory. A 15s daily SBF is
#: ~10 MB decompressed and a 1 Hz hour ~5 MB; anything near this is not a raw
#: file this tool should be rewriting.
MAX_SALVAGE_BYTES = 512 * 1024 * 1024

#: zlib level for salvaged files. Fixed, so a rerun on the same zlib build
#: reproduces the same compressed bytes too.
SALVAGE_GZIP_LEVEL = 6


class GzipStatus(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"  # the stream ends before its end-of-stream marker
    CORRUPT = "corrupt"  # bad deflate data or a failed CRC/length trailer
    NOT_GZIP = "not_gzip"
    EMPTY = "empty"
    MISSING = "missing"


BROKEN = frozenset({GzipStatus.TRUNCATED, GzipStatus.CORRUPT})


@dataclass(frozen=True)
class GzipProbe:
    """What one read of a file established."""

    status: GzipStatus
    size: int = 0  # on-disk (compressed) bytes
    compressed_sha256: Optional[str] = None
    readable_bytes: int = 0  # decompressed bytes recovered
    content_sha256: Optional[str] = None  # only when COMPLETE
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional[GzipProbe]:
        if d is None:
            return None
        return cls(**{**d, "status": GzipStatus(d["status"])})


def _inflate(path: FileRef, keep: bool) -> tuple[GzipProbe, Optional[bytes]]:
    """Stream-decompress ``path``. Returns (probe, prefix-or-None).

    Uses :func:`zlib.decompressobj` rather than :func:`gzip.open`, because
    ``gzip`` raises on a truncated stream and throws away what it had already
    decoded. Multi-member streams are followed; trailing zero padding after the
    last member is accepted (``gzip -t`` does the same).
    """
    p = Path(path)
    if not p.is_file():
        return GzipProbe(GzipStatus.MISSING), None
    size = p.stat().st_size
    if size == 0:
        return GzipProbe(GzipStatus.EMPTY, size=0), None

    csha = hashlib.sha256()
    dsha = hashlib.sha256()
    out = bytearray() if keep else None
    n_out = 0
    d = zlib.decompressobj(31)
    started = False
    error: Optional[str] = None
    corrupt = False

    with open(p, "rb") as fh:
        first = True
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            csha.update(chunk)
            if first:
                first = False
                if chunk[:2] != _GZIP_MAGIC:
                    # Keep hashing so the probe still carries the on-disk hash.
                    for rest in iter(lambda: fh.read(_CHUNK), b""):
                        csha.update(rest)
                    return (
                        GzipProbe(
                            GzipStatus.NOT_GZIP,
                            size=size,
                            compressed_sha256=csha.hexdigest(),
                        ),
                        None,
                    )
            if corrupt:
                continue  # finish the on-disk hash only
            data = chunk
            while data:
                if d.eof:
                    if not data.strip(b"\0"):
                        break  # trailing padding after the last member
                    d = zlib.decompressobj(31)
                try:
                    piece = d.decompress(data)
                except zlib.error as exc:
                    corrupt, error = True, str(exc)
                    break
                started = True
                dsha.update(piece)
                n_out += len(piece)
                if out is not None:
                    if n_out > MAX_SALVAGE_BYTES:
                        raise ValueError(
                            f"{p}: more than {MAX_SALVAGE_BYTES} decompressed bytes"
                        )
                    out += piece
                data = d.unused_data if d.eof else b""

    if corrupt:
        status = GzipStatus.CORRUPT
    elif started and d.eof:
        status = GzipStatus.COMPLETE
    else:
        status = GzipStatus.TRUNCATED
        error = "compressed stream ends before its end-of-stream marker"
        try:
            tail = d.flush()
        except zlib.error:
            tail = b""
        dsha.update(tail)
        n_out += len(tail)
        if out is not None:
            out += tail

    probe = GzipProbe(
        status,
        size=size,
        compressed_sha256=csha.hexdigest(),
        readable_bytes=n_out,
        content_sha256=dsha.hexdigest() if status == GzipStatus.COMPLETE else None,
        error=error,
    )
    return probe, (bytes(out) if out is not None else None)


def probe_gzip(path: FileRef) -> GzipProbe:
    """Read ``path`` once and classify its gzip stream. Never raises on bad data."""
    probe, _ = _inflate(path, keep=False)
    return probe


def read_readable_prefix(path: FileRef) -> tuple[GzipProbe, bytes]:
    """Probe ``path`` and return every decompressed byte it still yields."""
    probe, data = _inflate(path, keep=True)
    return probe, data or b""


@dataclass(frozen=True)
class SbfWalk:
    end: int  # offset just past the last CRC-valid block
    blocks: int
    stop: str  # "end" | "partial_block" | "bad_sync" | "bad_length" | "bad_crc"
    total: int  # bytes walked over

    @property
    def tail(self) -> int:
        return self.total - self.end


def sbf_complete_prefix(buf: bytes) -> SbfWalk:
    """Walk contiguous SBF blocks from offset 0; stop at the first bad one.

    Block layout: ``$@`` sync, CRC (u2 LE), ID (u2), Length (u2 LE, the whole
    block, >= 8 and a multiple of 4). The CRC is CRC-16-CCITT (poly 0x1021,
    init 0) over ID through the end of the block, which is exactly
    :func:`binascii.crc_hqx` with a start value of 0.

    Strictly contiguous on purpose: a receiver log has no bytes between blocks,
    so stopping at the first bad block can only keep LESS data, never splice in
    something that is not a block. No resynchronisation.
    """
    mv = memoryview(buf)
    total = len(buf)
    i = n = 0
    stop = "end"
    while i < total:
        if i + 8 > total:
            stop = "partial_block"
            break
        if buf[i : i + 2] != SBF_SYNC:
            stop = "bad_sync"
            break
        length = int.from_bytes(buf[i + 6 : i + 8], "little")
        if length < 8 or length % 4:
            stop = "bad_length"
            break
        if i + length > total:
            stop = "partial_block"
            break
        crc = int.from_bytes(buf[i + 2 : i + 4], "little")
        if binascii.crc_hqx(mv[i + 4 : i + length], 0) != crc:
            stop = "bad_crc"
            break
        i += length
        n += 1
    return SbfWalk(end=i, blocks=n, stop=stop, total=total)


@dataclass(frozen=True)
class Salvage:
    """A salvaged file: the CRC-valid SBF prefix, recompressed."""

    source_readable_bytes: int
    kept_bytes: int
    blocks: int
    stop: str
    content_sha256: str  # identity of the salvaged file (decompressed)
    data: bytes  # the new .gz bytes

    def summary(self) -> dict:
        return {
            "source_readable_bytes": self.source_readable_bytes,
            "kept_bytes": self.kept_bytes,
            "blocks": self.blocks,
            "stop": self.stop,
            "content_sha256": self.content_sha256,
            "gz_size": len(self.data),
        }


def is_sbf_name(name: str) -> bool:
    """Only Septentrio ``.sbf.gz`` raw is walkable. Everything else is
    reported unsupported, never treated as salvageable."""
    return name.lower().endswith(".sbf.gz")


def salvage(path: FileRef) -> Optional[Salvage]:
    """Salvage the readable SBF prefix of ``path``.

    Returns ``None`` when there is nothing to keep: the file is not an
    ``.sbf.gz``, it is not truncated or corrupt, or not even one complete
    block decodes.
    """
    if not is_sbf_name(Path(path).name):
        return None
    probe, prefix = read_readable_prefix(path)
    if probe.status not in BROKEN:
        return None
    walk = sbf_complete_prefix(prefix)
    if walk.blocks == 0:
        return None
    kept = prefix[: walk.end]
    data = gzip.compress(kept, compresslevel=SALVAGE_GZIP_LEVEL, mtime=0)
    return Salvage(
        source_readable_bytes=len(prefix),
        kept_bytes=walk.end,
        blocks=walk.blocks,
        stop=walk.stop,
        content_sha256=hashlib.sha256(kept).hexdigest(),
        data=data,
    )


# ---------------------------------------------------------------------------
# The decision ladder
# ---------------------------------------------------------------------------


class Action(StrEnum):
    REPLACE = "replace"  # a complete copy exists: copy it over the broken one(s)
    REFETCH = "refetch"  # the receiver still holds the file
    DEFERRED = "deferred"  # the receiver may hold it, but we cannot tell now
    SALVAGE = "salvage"  # gone everywhere: keep the CRC-valid prefix
    UNRECOVERABLE = "unrecoverable"  # gone everywhere, nothing decodes
    UNSUPPORTED = "unsupported"  # not a format this tool can walk
    CONFLICT = "conflict"  # facts disagree; a human decides


class ReceiverState(StrEnum):
    HOLDS = "holds"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    source: Optional[str] = None  # "local" | "archive" — where the bytes come from
    targets: tuple[str, ...] = ()  # locations the apply step writes

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "source": self.source,
            "targets": list(self.targets),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Decision:
        return cls(
            action=Action(d["action"]),
            reason=d["reason"],
            source=d.get("source"),
            targets=tuple(d.get("targets") or ()),
        )


LOCATIONS = ("archive", "local")


def decide(
    *,
    name: str,
    copies: dict[str, Optional[GzipProbe]],
    catalog_content_sha256: Optional[str],
    receiver: ReceiverState,
    salvage_source: Optional[str],
    salvage_blocks: int,
) -> Decision:
    """First match wins. Pure: depends only on its arguments.

    Args:
        name: the file name (decides whether the format is walkable).
        copies: ``{"archive": probe|None, "local": probe|None}``.
        catalog_content_sha256: the archive catalog's content hash, if a row
            exists with one. A complete copy that disagrees with it is a
            CONFLICT, never a silent replacement.
        receiver: whether the receiver still holds the file.
        salvage_source: the broken copy salvage would read (most readable bytes).
        salvage_blocks: CRC-valid blocks that copy yields (0 = none).
    """
    present = {k: v for k, v in copies.items() if v is not None}
    broken = [k for k in LOCATIONS if k in present and present[k].status in BROKEN]
    if not broken:
        return Decision(Action.CONFLICT, "no broken copy — nothing to repair")

    complete = [
        k
        for k in LOCATIONS
        if k in present and present[k].status == GzipStatus.COMPLETE
    ]
    # Both locations always end up holding the repaired file: the archive is
    # the record, and the local rolling window is where RINEX is converted
    # from (_run_backfill_rinex reads data_prepath). An archive-only repair
    # would otherwise be reported done while its RINEX never appears.
    everywhere = LOCATIONS

    # (a) A complete copy exists.
    if complete:
        src = complete[0]  # archive preferred: deterministic tie-break
        csha = present[src].content_sha256
        if catalog_content_sha256 and csha != catalog_content_sha256:
            return Decision(
                Action.CONFLICT,
                f"the complete {src} copy does not match the catalog hash",
                source=src,
            )
        return Decision(
            Action.REPLACE,
            f"complete {src} copy replaces the broken {'+'.join(broken)} copy",
            source=src,
            targets=tuple(k for k in LOCATIONS if k in broken or k not in present),
        )

    # (b) The receiver may still hold it.
    if receiver == ReceiverState.HOLDS:
        return Decision(
            Action.REFETCH,
            "receiver still holds this period",
            targets=everywhere,
        )
    if receiver == ReceiverState.UNKNOWN:
        return Decision(
            Action.DEFERRED,
            "receiver retention unknown — never salvage while the full file may exist",
        )

    # (c) Gone everywhere: salvage what decodes.
    if not is_sbf_name(name):
        return Decision(
            Action.UNSUPPORTED,
            "no block walker for this format; left in place",
        )
    if salvage_source and salvage_blocks > 0:
        return Decision(
            Action.SALVAGE,
            f"receiver no longer holds it; keep {salvage_blocks} CRC-valid blocks",
            source=salvage_source,
            targets=everywhere,
        )
    # (d)
    return Decision(
        Action.UNRECOVERABLE,
        "receiver no longer holds it and no complete SBF block decodes",
    )
