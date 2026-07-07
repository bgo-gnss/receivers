"""content_sha256 — SHA-256 over the DECOMPRESSED content of an archive file.

Compression-invariant: a file and its gzip / bzip2 / xz twin hash to the **same**
value, because we peel the *generic* compression layer (detected by magic bytes,
not by extension) and hash the bytes underneath. This is the unifying primitive
behind the archive index:

  * **integrity** — re-hashing detects bit-rot and silent loss,
  * **back-zip verify** — ``content_sha256(".T00") == content_sha256(".T00.gz")``
    proves a re-compression preserved the data, so the plain file is safe to delete,
  * **cross-format dedup** — ``.T02`` ≡ ``.T02.gz`` share a hash,
  * **#34 canonical product hash** — local↔archive divergence is a hash mismatch.

Hatanaka / RINEX content encodings are **not** peeled — they live *inside* the
hashed content — so this hash stays in lockstep with :mod:`receivers.utils.canonical_key`,
which likewise folds compression only. Two files that share a canonical key share
a hash; two files with distinct content never do. See design note
``1781867391-data-dissemination-archive-sync-design``.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import os
import subprocess
from pathlib import Path
from typing import IO, Union

from .compression_detector import detect_compression

FileRef = Union[str, "os.PathLike[str]"]
# A readable binary stream: a plain file, a gzip/bz2/lzma decompressing wrapper,
# or the `gzip -dc` subprocess stream used for .Z (Unix compress).
BinaryStream = Union[
    IO[bytes], gzip.GzipFile, bz2.BZ2File, lzma.LZMAFile, "_GzipDcStream"
]

_CHUNK = 1 << 20  # 1 MiB streaming reads — never load a whole file into memory


class CorruptArchiveFileError(Exception):
    """A compressed archive file could not be fully decompressed.

    Raised on truncated / corrupt input so the caller can record an integrity
    finding. The hasher never returns a hash of partially-decompressed data —
    a corrupt file must not be able to look healthy.
    """


class _GzipDcStream:
    """Readable stream of ``gzip -dc PATH`` stdout, checking a clean exit on close.

    ``.Z`` (Unix LZW *compress*, magic ``1f 9d``) has no Python stdlib reader —
    ``gzip``/``bz2``/``lzma`` cannot decode it. But the gzip(1) binary does, so
    we stream its stdout through the *same* chunked hasher every other format
    uses; the hash is still taken over the decompressed bytes, so a ``.d.Z``
    Hatanaka RINEX hashes identically to its decompressed ``.d`` twin.

    On exit a non-zero returncode (gzip hard error: not-in-format, I/O failure)
    becomes :class:`CorruptArchiveFileError`. NOTE: LZW ``.Z`` has no CRC/length
    trailer — unlike gzip — so a *truncated* ``.Z`` can decode to partial output
    with a zero exit and is NOT caught here. For ``.Z`` the integrity guarantee
    comes from content-hash COMPARISON (the verify pass against the stored
    ``content_sha256``), not from the decompressor.
    """

    def __init__(self, path: FileRef) -> None:
        self._path = os.fspath(path)
        self._proc = subprocess.Popen(
            ["gzip", "-dc", self._path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read(self, size: int = -1) -> bytes:
        assert self._proc.stdout is not None
        return self._proc.stdout.read(size)

    def __enter__(self) -> _GzipDcStream:
        return self

    def __exit__(self, *exc: object) -> None:
        exc_type = exc[0] if exc else None
        # The hasher reads stdout to EOF before we get here, so gzip has finished
        # producing data; draining stderr (small — error text only) won't deadlock.
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        stderr = b""
        if self._proc.stderr is not None:
            stderr = self._proc.stderr.read()
            self._proc.stderr.close()
        rc = self._proc.wait()
        # Only turn a bad exit into corruption when the body didn't already fail,
        # so we never mask the original error/traceback.
        if exc_type is None and rc != 0:
            detail = stderr.decode("utf-8", "replace").strip() or f"gzip exited {rc}"
            raise CorruptArchiveFileError(
                f"could not decompress {self._path!r}: {detail}"
            )


def _open_decompressed(path: FileRef, fmt: str | None) -> BinaryStream:
    """Open ``path`` as a binary stream of its decompressed content.

    ``fmt`` is the compression format name from :func:`detect_compression`
    (``None`` for an uncompressed file). Raises :class:`NotImplementedError`
    for formats that do not occur in the raw-tier archive.
    """
    if fmt is None:
        return open(path, "rb")
    if fmt == "gzip":
        return gzip.open(path, "rb")
    if fmt == "bzip2":
        return bz2.open(path, "rb")
    if fmt == "xz":
        return lzma.open(path, "rb")
    if fmt == "compress":
        # .Z (Unix compress / LZW) — no stdlib reader; delegate to the gzip(1)
        # binary, which decodes .Z. Appears as Hatanaka .d.Z in the RINEX tier.
        return _GzipDcStream(path)
    raise NotImplementedError(
        f"compression format {fmt!r} is not used in the GNSS archive"
    )


def content_sha256(path: FileRef, *, chunk_size: int = _CHUNK) -> str:
    """Return the SHA-256 hex digest of ``path``'s decompressed content.

    Compression is detected from the file's magic bytes (so a gzip stream named
    ``.T02`` still hashes as its decompressed content). gzip / bzip2 / xz / .Z
    (Unix compress, via the gzip binary) are all decoded; the hash is over the
    decompressed bytes for every format. Streams in ``chunk_size`` blocks. Raises
    :class:`CorruptArchiveFileError` if a compressed file is truncated or corrupt,
    and :class:`NotImplementedError` for unsupported formats (zstd / zip).
    """
    detected = detect_compression(Path(path))
    fmt = detected[0] if detected else None

    digest = hashlib.sha256()
    try:
        with _open_decompressed(path, fmt) as stream:
            for chunk in iter(lambda: stream.read(chunk_size), b""):
                digest.update(chunk)
    except (gzip.BadGzipFile, lzma.LZMAError, EOFError, OSError) as exc:
        # OSError covers bz2's "Invalid data stream" and gzip CRC failures.
        raise CorruptArchiveFileError(
            f"could not decompress {os.fspath(path)!r}: {exc}"
        ) from exc

    return digest.hexdigest()


def compressed_sha256(path: FileRef, *, chunk_size: int = _CHUNK) -> str:
    """Return the SHA-256 hex digest of ``path``'s ON-DISK (compressed) bytes.

    The counterpart to :func:`content_sha256`: no decompression, just the raw
    bytes as stored. This is the hash that corresponds to the EPOS
    ``md5checksum`` (same on-disk bytes, different algorithm) — the *only* valid
    sha256↔md5 mapping in the unified index (``md5uncompressed`` folds both gzip
    and CRX2RNX and has no existing sha256 twin). Unlike ``content_sha256`` this
    can never raise a corruption error — it hashes exactly what is on disk, so a
    truncated ``.Z`` still produces a well-defined (and comparison-detectable)
    hash. Streams in ``chunk_size`` blocks.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
