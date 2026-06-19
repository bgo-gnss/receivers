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
from pathlib import Path
from typing import IO, Union

from .compression_detector import detect_compression

FileRef = Union[str, "os.PathLike[str]"]
# A readable binary stream: a plain file or a gzip/bz2/lzma decompressing wrapper.
BinaryStream = Union[IO[bytes], gzip.GzipFile, bz2.BZ2File, lzma.LZMAFile]

_CHUNK = 1 << 20  # 1 MiB streaming reads — never load a whole file into memory


class CorruptArchiveFileError(Exception):
    """A compressed archive file could not be fully decompressed.

    Raised on truncated / corrupt input so the caller can record an integrity
    finding. The hasher never returns a hash of partially-decompressed data —
    a corrupt file must not be able to look healthy.
    """


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
        # .Z (Unix compress / LZW) — no stdlib reader. Not present in the raw
        # tier; appears as Hatanaka .d.Z in RINEX. Implement on the
        # RINEX/dissemination track (#34), with a tested gzip -dc fallback.
        raise NotImplementedError(
            ".Z (Unix compress) decompression is not supported yet — "
            "RINEX/dissemination track, see todo #34"
        )
    raise NotImplementedError(
        f"compression format {fmt!r} is not used in the GNSS archive"
    )


def content_sha256(path: FileRef, *, chunk_size: int = _CHUNK) -> str:
    """Return the SHA-256 hex digest of ``path``'s decompressed content.

    Compression is detected from the file's magic bytes (so a gzip stream named
    ``.T02`` still hashes as its decompressed content). Streams in ``chunk_size``
    blocks. Raises :class:`CorruptArchiveFileError` if a compressed file is truncated
    or corrupt, and :class:`NotImplementedError` for unsupported formats
    (``.Z`` / zstd / zip).
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
