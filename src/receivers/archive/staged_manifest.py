"""Read a standard sha256sum manifest of existing, finished RINEX products.

Selection/validation only: no conversion, transfer, database access or cleanup.
All entries are checked for station/session/category before any product is opened.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .path_parse import parse_archive_path

MONTHS = (
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


@dataclass(frozen=True)
class StagedManifest:
    sha256: str
    products: tuple[tuple[str, str], ...]  # (archive-relative path, expected SHA256)

    @property
    def checksums(self) -> dict[str, str]:
        return dict(self.products)


@contextmanager
def open_directory_nofollow(root: Path):
    """Open an absolute directory without following any symlink component."""
    if not root.is_absolute():
        raise ValueError("absolute directory required")
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


@contextmanager
def open_regular_nofollow(root: Path, relative: str):
    """Descriptor-relative no-follow read, including every parent component."""
    parts = relative.split("/")
    if PurePosixPath(relative).is_absolute() or any(
        p in ("", ".", "..") for p in parts
    ):
        raise ValueError("safe relative path required")
    with open_directory_nofollow(root) as base:
        directory = os.dup(base)
    try:
        for part in PurePosixPath(relative).parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        fd = os.open(
            PurePosixPath(relative).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("manifest/product is not a regular file")
            yield stream
            after = os.fstat(stream.fileno())
            reached = os.stat(
                PurePosixPath(relative).name,
                dir_fd=directory,
                follow_symlinks=False,
            )

            def signature(s):
                return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

            if signature(before) != signature(after) or signature(after) != signature(
                reached
            ):
                raise ValueError("manifest/product changed during validation")
    finally:
        os.close(directory)


def read_staged_manifest(
    manifest: str,
    *,
    work_dir: str,
    stations: list[str],
    session: str,
    validate_files: bool = True,
    categories: tuple[str, ...] = ("rinex",),
) -> StagedManifest:
    """Validate the exact checksum-bound selection; never discover extra files.

    Supports sha256sum text (two spaces) and binary (space-star) records. Blank
    lines are ignored. Escaped paths, duplicates, absolute/traversal paths,
    unfinished files and non-RINEX entries are refused, not silently skipped.
    The caller must still coordinate writers: these reads are not a CAS/lock.
    """
    root = Path(os.path.abspath(os.path.expanduser(work_dir)))
    source = Path(os.path.abspath(os.path.expanduser(manifest)))
    with open_regular_nofollow(source.parent, source.name) as stream:
        raw = stream.read()
    products = {}
    for number, line in enumerate(raw.decode("ascii").splitlines(), 1):
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\s\\]+)", line)
        if not match:
            raise ValueError(f"manifest line {number}: expected sha256sum record")
        digest, relative = match.groups()
        path = PurePosixPath(relative)
        parts = relative.split("/")
        if (
            path.is_absolute()
            or len(parts) != 6
            or any(p in ("", ".", "..") for p in parts)
            or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts)
            or parts[2] not in stations
            or parts[3] != session
            or parts[4] not in categories
            or path.suffix not in (".Z", ".gz")
        ):
            raise ValueError(f"manifest line {number}: outside selected RINEX scope")
        short = re.fullmatch(
            r"[A-Za-z0-9]{4}\d{3}[0a-x]\.\d{2}[dDoO]\.(?:Z|gz)", path.name
        )
        long = re.fullmatch(
            r"[A-Za-z0-9]{9}_[RSU]_\d{11}_\d{2}[DHMSU]_\d{2}[CZDHMSU]_[GREJCSIM]O\.(?:rnx|crx)\.(?:Z|gz)",
            path.name,
        )
        if (
            not (short or long)
            or path.name[:4].upper() != parts[2]
            or not re.fullmatch(r"\d{4}", parts[0])
        ):
            raise ValueError(
                f"manifest line {number}: invalid observation filename/identity"
            )
        parsed = parse_archive_path(str(root / relative), str(root))
        if (
            parsed is None
            or parsed.file_date is None
            or parsed.file_date.year != int(parts[0])
            or MONTHS[parsed.file_date.month] != parts[1]
        ):
            raise ValueError(f"manifest line {number}: invalid observation date/path")
        if relative in products:
            raise ValueError(f"manifest line {number}: duplicate path")
        products[relative] = digest.lower()
    if not products:
        raise ValueError("manifest selects no products")
    # Selection must be wholly valid before opening any product, especially raw.
    if validate_files:
        for relative, expected in products.items():
            if file_checksum(root, relative) != expected:
                raise ValueError(f"staged checksum mismatch: {relative}")
    return StagedManifest(hashlib.sha256(raw).hexdigest(), tuple(products.items()))


def file_checksum(root: Path, relative: str) -> str:
    """Hash one enumerated regular file, without following namespace symlinks."""
    with open_regular_nofollow(root, relative) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def namespace_snapshot(root: Path, relatives, *, owner_uid: int) -> dict:
    """Pin only enumerated public files/parents; metadata checks, not a CAS."""
    directories, files = {}, {}
    with open_directory_nofollow(root) as base:
        s = os.fstat(base)
        root_pin = [s.st_dev, s.st_ino, s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)]
        for relative in relatives:
            parts = relative.split("/")
            if (
                len(parts) != 6
                or any(p in ("", ".", "..") for p in parts)
                or parts[4] != "rinex"
            ):
                raise ValueError("namespace scope requires explicit RINEX paths")
            directory = os.dup(base)
            try:
                for index, part in enumerate(parts[:-1]):
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    os.close(directory)
                    directory = child
                    value = os.fstat(directory)
                    directories["/".join(parts[: index + 1])] = [
                        value.st_dev,
                        value.st_ino,
                        value.st_uid,
                        value.st_gid,
                        stat.S_IMODE(value.st_mode),
                    ]
                with open_regular_nofollow(root, relative) as stream:
                    value = os.fstat(stream.fileno())
                    if value.st_uid != owner_uid or stat.S_IMODE(value.st_mode) not in (
                        0o600,
                        0o644,
                    ):
                        raise ValueError("unexpected archive baseline owner/mode")
                    files[relative] = [
                        value.st_dev,
                        value.st_ino,
                        value.st_uid,
                        value.st_gid,
                        value.st_nlink,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                        stat.S_IMODE(value.st_mode),
                    ]
            finally:
                os.close(directory)
    return {
        "owner_uid": owner_uid,
        "root": root_pin,
        "directories": directories,
        "files": files,
    }


def require_namespace(root: Path, relatives, expected: dict) -> None:
    current = namespace_snapshot(root, relatives, owner_uid=expected["owner_uid"])
    if (
        current["root"] != expected["root"]
        or any(
            value != expected["directories"].get(key)
            for key, value in current["directories"].items()
        )
        or any(
            value != expected["files"].get(key)
            for key, value in current["files"].items()
        )
    ):
        raise ValueError("archive file/parent namespace changed since reviewed preview")


def preserved_replacements(
    products: StagedManifest,
    originals: StagedManifest,
    *,
    original_root: Path,
    archive_root: Path,
) -> list[str]:
    """Prove existing original copies; return only still-baseline replacements.

    No raw discovery, conversion, numeric QC, copying or overwriting originals.
    Already-published candidates are omitted, not replayed. Call again immediately
    before transfer to detect stale preconditions; writer coordination remains
    external (these reads are not a CAS).
    """
    if products.checksums.keys() != originals.checksums.keys():
        raise ValueError("preservation selection must exactly match publication")
    if original_root == archive_root:
        raise ValueError("original copies must be separate from the live archive")
    pending = []
    for relative, candidate in products.products:
        baseline = originals.checksums[relative]
        if file_checksum(original_root, relative) != baseline:
            raise ValueError(f"protected original mismatch: {relative}")
        actual = file_checksum(archive_root, relative)
        if actual == candidate:
            continue
        if actual != baseline:
            raise ValueError(
                f"archive is neither approved baseline nor candidate: {relative}"
            )
        pending.append(relative)
    return pending
