"""Local generated-file admission tests; no receiver/archive/network access."""

import gzip
import hashlib
import os

import pytest

from receivers.archive.staged_manifest import read_staged_manifest

REL = "2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz"


def fixture(tmp_path, relative=REL):
    root = tmp_path / "stage"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(b"generated observations", mtime=1))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{digest}  {relative}\n")
    return root, path, manifest, digest


def read(root, manifest):
    return read_staged_manifest(
        str(manifest), work_dir=str(root), stations=["TEST"], session="15s_24hr"
    )


@pytest.mark.parametrize("delimiter", ["  ", " *"])
def test_standard_sha256sum_formats(tmp_path, delimiter):
    root, path, manifest, digest = fixture(tmp_path)
    manifest.write_text(f"{digest.upper()}{delimiter}{REL}\n\n")
    result = read(root, manifest)
    assert result.products == ((REL, digest),)
    assert result.sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "relative",
    [
        "/2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz",
        "../2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz",
        "2026/jun/TEST/15s_24hr/raw/TEST1720.26D.gz",
        "2026/jun/ELDC/15s_24hr/rinex/ELDC1720.26D.gz",
        "2026/jun/TEST/1Hz_1hr/rinex/TEST172a.26D.gz",
        "2026/jun/TEST/15s_24hr/rinex/TEST1720.26o",
        "2026//TEST/15s_24hr/rinex/TEST1720.26D.gz",
        "2026/jan/TEST/15s_24hr/rinex/TEST1720.26D.gz",
        "2026/jun/TEST/15s_24hr/rinex/not-an-observation.gz",
        "2026/jun/TEST/15s_24hr/rinex/ELDC1720.26D.gz",
        "2026/jun/TEST/15s_24hr/rinex/TEST202606210000a.sbf.gz",
    ],
)
def test_refuse_scope_before_opening_products(tmp_path, relative, monkeypatch):
    root, _, manifest, digest = fixture(tmp_path)
    manifest.write_text(f"{digest}  {REL}\n{digest}  {relative}\n")
    monkeypatch.setattr(
        hashlib,
        "file_digest",
        lambda *a, **k: pytest.fail("opened a product before checking whole selection"),
    )
    with pytest.raises(ValueError):
        read(root, manifest)


def test_duplicate_empty_and_bad_manifests(tmp_path):
    root, _, manifest, digest = fixture(tmp_path)
    for text in ("", f"{digest}  {REL}\n{digest}  {REL}\n", "not a checksum record\n"):
        manifest.write_text(text)
        with pytest.raises(ValueError):
            read(root, manifest)


def test_checksum_mismatch_refuses(tmp_path):
    root, path, manifest, _ = fixture(tmp_path)
    path.write_bytes(b"changed generated content")
    with pytest.raises(ValueError, match="checksum mismatch"):
        read(root, manifest)


@pytest.mark.parametrize("kind", ["manifest", "file", "parent"])
def test_no_symlink_dereference(tmp_path, kind):
    root, path, manifest, digest = fixture(tmp_path)
    if kind == "manifest":
        real = tmp_path / "real-list"
        manifest.rename(real)
        manifest.symlink_to(real)
    elif kind == "file":
        real = tmp_path / "real-product"
        path.rename(real)
        path.symlink_to(real)
    else:
        real = tmp_path / "real-year"
        (root / "2026").rename(real)
        (root / "2026").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        read(root, manifest)


def test_unknown_shared_parser_date_refuses_long_name(tmp_path):
    # The existing catalog date parser cannot currently date this IGS long name.
    # Do not silently turn that limitation into a NULL-date catalog row.
    relative = "2026/jun/TEST/15s_24hr/rinex/TEST00ISL_R_20261720000_01D_15S_MO.crx.gz"
    root, _, manifest, _ = fixture(tmp_path, relative)
    with pytest.raises(ValueError, match="invalid observation date/path"):
        read(root, manifest)


def test_fifo_is_rejected_without_blocking(tmp_path):
    root, path, manifest, _ = fixture(tmp_path)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        read(root, manifest)
