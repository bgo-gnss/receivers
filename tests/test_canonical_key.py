"""Tests for the compression-invariant canonical archive-file key.

The canonical key folds *compression* and *extension case* only. The negative
tests pin the scope boundary: Hatanaka / RINEX content re-encodes must NOT fold,
because they carry a different ``content_sha256`` and the key has to stay in
lockstep with that hash (see ``utils/canonical_key.py`` and design note
1781867391).
"""

import os

import pytest

from receivers.utils.canonical_key import (
    canonical_key,
    find_by_canonical_key,
    same_archive_file,
    strip_compression,
)


class TestStripCompression:
    @pytest.mark.parametrize(
        "name,base,suffix",
        [
            ("ALHV0410.26.T02.gz", "ALHV0410.26.T02", ".gz"),
            ("ELDC0410.26d.Z", "ELDC0410.26d", ".Z"),
            ("foo.sbf.bz2", "foo.sbf", ".bz2"),
            ("foo.sbf.xz", "foo.sbf", ".xz"),
            ("foo.sbf.zst", "foo.sbf", ".zst"),
            ("ALHV0410.26.T02", "ALHV0410.26.T02", ""),  # uncompressed
            ("THOB...b.sbf", "THOB...b.sbf", ""),
        ],
    )
    def test_split(self, name, base, suffix):
        assert strip_compression(name) == (base, suffix)

    def test_only_one_suffix_stripped(self):
        # A single trailing suffix — not recursive.
        assert strip_compression("file.gz.gz") == ("file.gz", ".gz")

    def test_does_not_strip_bare_suffix_as_whole_name(self):
        # ".gz" alone is not a compressed file — guarded by len check.
        assert strip_compression(".gz") == (".gz", "")


class TestCanonicalKey:
    def test_compression_folds(self):
        assert canonical_key("ALHV0410.26.T02.gz") == canonical_key("ALHV0410.26.T02")

    def test_case_folds(self):
        assert canonical_key("ALHV0410.26.T02") == canonical_key("alhv0410.26.t02")

    def test_compression_and_case_fold_together(self):
        assert canonical_key("ALHV0410.26.T02") == canonical_key("alhv0410.26.t02.gz")

    def test_z_compress_folds_to_uncompressed(self):
        assert canonical_key("ELDC0410.26d.Z") == canonical_key("ELDC0410.26d")

    def test_basename_only(self):
        assert canonical_key(
            "/data/2026/feb/ALHV/raw/ALHV0410.26.T02.gz"
        ) == canonical_key("ALHV0410.26.T02")

    def test_accepts_pathlike(self):
        from pathlib import Path

        assert (
            canonical_key(Path("x/THOB202602101400b.sbf.gz")) == "thob202602101400b.sbf"
        )

    def test_septentrio_example(self):
        assert canonical_key("THOB202602101400b.sbf.gz") == "thob202602101400b.sbf"

    # --- Negative boundary: Hatanaka / content re-encodes must NOT fold ---

    def test_hatanaka_does_not_fold_to_obs(self):
        # .d (Hatanaka) vs .o (plain obs): different letters, different content,
        # different content_sha256 -> must remain distinct keys.
        assert canonical_key("ELDC0410.26d.Z") != canonical_key("ELDC0410.26o")

    def test_crx_does_not_fold_to_rnx(self):
        # Long-name RINEX 3: CRX (Hatanaka) vs RNX (plain) are different content.
        assert canonical_key(
            "ELDC00ISL_R_20260410000_01D_15S_MO.crx.gz"
        ) != canonical_key("ELDC00ISL_R_20260410000_01D_15S_MO.rnx.gz")

    def test_raw_does_not_fold_to_rinex(self):
        assert canonical_key("ALHV0410.26.T02") != canonical_key("ALHV0410.26o")


class TestSameArchiveFile:
    def test_same_modulo_compression(self):
        assert same_archive_file("ALHV0410.26.T02", "alhv0410.26.t02.gz")

    def test_distinct_content_not_same(self):
        assert not same_archive_file("ELDC0410.26d.Z", "ELDC0410.26o")


class TestFindByCanonicalKey:
    def test_finds_compressed_when_expecting_plain(self, tmp_path):
        # Archive holds the compressed file; we expect the plain name.
        (tmp_path / "ALHV0410.26.T02.gz").write_bytes(b"data")
        hit = find_by_canonical_key(tmp_path, "ALHV0410.26.T02")
        assert hit is not None
        assert os.path.basename(hit) == "ALHV0410.26.T02.gz"

    def test_finds_plain_when_expecting_compressed(self, tmp_path):
        # The reverse the old `+ ".gz"` fallback could not do.
        (tmp_path / "ALHV0410.26.T02").write_bytes(b"data")
        hit = find_by_canonical_key(tmp_path, "ALHV0410.26.T02.gz")
        assert hit is not None
        assert os.path.basename(hit) == "ALHV0410.26.T02"

    def test_finds_case_variant(self, tmp_path):
        (tmp_path / "alhv0410.26.t02.gz").write_bytes(b"data")
        hit = find_by_canonical_key(tmp_path, "ALHV0410.26.T02")
        assert hit is not None

    def test_finds_z_compress_variant(self, tmp_path):
        (tmp_path / "ELDC0410.26d.Z").write_bytes(b"data")
        hit = find_by_canonical_key(tmp_path, "ELDC0410.26d.gz")
        assert hit is not None

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "OTHER0410.26.T02.gz").write_bytes(b"data")
        assert find_by_canonical_key(tmp_path, "ALHV0410.26.T02") is None

    def test_does_not_match_hatanaka_sibling(self, tmp_path):
        # Only the obs file is present; expecting the Hatanaka file -> miss.
        (tmp_path / "ELDC0410.26o").write_bytes(b"data")
        assert find_by_canonical_key(tmp_path, "ELDC0410.26d") is None

    def test_missing_directory_returns_none(self, tmp_path):
        assert find_by_canonical_key(tmp_path / "nope", "ALHV0410.26.T02") is None

    def test_not_a_directory_returns_none(self, tmp_path):
        f = tmp_path / "afile"
        f.write_bytes(b"x")
        assert find_by_canonical_key(f, "ALHV0410.26.T02") is None
