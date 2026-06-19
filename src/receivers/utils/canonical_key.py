"""Canonical archive-file key — compression-invariant filename identity.

Two archive filenames that hold the *same data* and differ only in compression
or extension case (e.g. ``ALHV0410.26.T02`` ≡ ``ALHV0410.26.T02.gz`` ≡
``alhv0410.26.t02.gz``) must be recognised as the same file by every
"do we already have this?" read path: gap detection, the archive-sync delta,
the integrity scan, FormatResolver presence checks.

Why this exists
---------------
rek_new emits compressed raw (``.T02.gz``); the long-term archive on ananas
still holds some Trimble raw **uncompressed** (``.T02``), occasionally with
lowercase extensions. A naïve ``os.path.isfile(expected)`` (case-sensitive on
the NFS/ext4 mounts) plus a single ``+ ".gz"`` fallback misses those variants
and reports a present file as a gap. The canonical key folds the compression
suffix and case so the variants compare equal.

Scope — compression equivalence ONLY
-------------------------------------
The key deliberately does **not** fold Hatanaka / RINEX content transforms:
``.crx`` ≢ ``.rnx`` and ``.d`` ≢ ``.o`` are content re-encodings with a
*different* ``content_sha256``. The canonical key must stay in lockstep with
that hash so two filenames sharing a key always share a hash — otherwise the
dedup / ``archive_catalog`` logic the key feeds is silently corrupted. Folding
Hatanaka belongs to the back-zip/dedup track (with CRX2RNX-in-the-hash), not
here. The negative test in ``tests/test_canonical_key.py`` pins this boundary.

See design note ``1781867391-data-dissemination-archive-sync-design``.
"""

from __future__ import annotations

import os
from typing import Union

# Anything that names a file: a string path/filename or an os.PathLike.
FileRef = Union[str, "os.PathLike[str]"]

# Recognised compression suffixes, lowercase. One trailing suffix is stripped.
# Mirrors the formats in ``utils.compression_detector.CompressionFormat`` that
# actually appear in the GNSS archive. ``.Z`` (Unix compress) lowercases to
# ``.z``; ``.gz`` does not collide with ``.z`` because ``endswith`` matches the
# full ``.z`` token, not a bare ``z``.
COMPRESSION_SUFFIXES: tuple[str, ...] = (".gz", ".z", ".bz2", ".xz", ".zst")


def strip_compression(name: str) -> tuple[str, str]:
    """Split one trailing compression suffix off ``name`` (case-insensitive).

    Returns ``(base, suffix)`` where ``suffix`` is the matched suffix in its
    original case (``""`` if the name is not compressed). ``base`` keeps its
    original case.

    >>> strip_compression("ALHV0410.26.T02.gz")
    ('ALHV0410.26.T02', '.gz')
    >>> strip_compression("ELDC0410.26d.Z")
    ('ELDC0410.26d', '.Z')
    >>> strip_compression("ALHV0410.26.T02")
    ('ALHV0410.26.T02', '')
    """
    lower = name.lower()
    for suf in COMPRESSION_SUFFIXES:
        if lower.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)], name[-len(suf) :]
    return name, ""


def canonical_key(path: FileRef) -> str:
    """Compression- and case-invariant identity key for an archive file.

    Takes a path or filename (anything ``os.fspath``-able or a ``str``),
    reduces it to its basename, strips one trailing compression suffix, and
    lowercases the result. Two files that hold the same data differing only in
    compression or extension case map to the same key.

    >>> canonical_key("/data/2026/ALHV/raw/ALHV0410.26.T02.gz")
    'alhv0410.26.t02'
    >>> canonical_key("alhv0410.26.t02")
    'alhv0410.26.t02'
    >>> canonical_key("THOB202602101400b.sbf.gz")
    'thob202602101400b.sbf'

    Hatanaka is *not* folded (different letters, not just case):

    >>> canonical_key("ELDC0410.26d.Z") == canonical_key("ELDC0410.26o")
    False
    """
    name = os.path.basename(os.fspath(path))
    base, _ = strip_compression(name)
    return base.lower()


def same_archive_file(a: FileRef, b: FileRef) -> bool:
    """True if ``a`` and ``b`` are the same archive file modulo compression/case.

    >>> same_archive_file("ALHV0410.26.T02", "alhv0410.26.t02.gz")
    True
    >>> same_archive_file("ELDC0410.26d.Z", "ELDC0410.26o")
    False
    """
    return canonical_key(a) == canonical_key(b)


def find_by_canonical_key(directory: FileRef, expected_name: FileRef) -> str | None:
    """Return the path in ``directory`` matching ``expected_name``'s canonical key.

    Compression- and case-invariant directory lookup: lists ``directory`` and
    returns the first entry whose :func:`canonical_key` equals that of
    ``expected_name`` (a path or bare filename). Returns ``None`` when the
    directory is absent/not-a-directory or no entry matches.

    This is the robust replacement for ``os.path.isfile(expected) or
    isfile(expected + ".gz")`` — it also catches ``.Z``/``.bz2`` and the
    reverse case (expected ``.gz`` but the archive holds the plain file).
    """
    key = canonical_key(expected_name)
    try:
        entries = os.listdir(os.fspath(directory))
    except (FileNotFoundError, NotADirectoryError):
        return None
    for entry in entries:
        if canonical_key(entry) == key:
            return os.path.join(directory, entry)
    return None
