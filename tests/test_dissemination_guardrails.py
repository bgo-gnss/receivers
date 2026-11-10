"""Guardrails for the EPOS dissemination one-file-per-slot invariant.

Covers the guardrails added after the ELEY R2-mistake cleanup that survive the
retirement of supersede-cleanup (2026-11; G2's same-slot purge read ``rinex_file``):

* **G1** — the engine skips a decimated (``sample``) product when the source is
  RINEX2 (short naming can't encode the rate → a rate-less duplicate).
* **G3** — ``published_name`` forces uppercase ``.D`` on RINEX2 Hatanaka shorts
  to match the archive (.d->.D) and the legacy EPOS portal.
"""

from __future__ import annotations

from datetime import date

import pytest

from receivers.dissemination import engine as eng_mod
from receivers.dissemination.config import ProductSpec, VersionPolicy
from receivers.dissemination.convert import published_name


# --------------------------------------------------------------------------- G3
class TestG3UppercaseHatanaka:
    def test_r2_short_forced_uppercase_D(self):
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        # lowercase obs .26o -> uppercase .26D.Z (not .26d.Z)
        assert published_name("ELEY0600.26o", r2) == "ELEY0600.26D.Z"

    def test_r2_short_already_uppercase_stays_D(self):
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        assert published_name("ELEY0600.26O", r2) == "ELEY0600.26D.Z"

    def test_r3_long_crx_unaffected(self):
        r3 = VersionPolicy(naming="long", hatanaka=True, compression="gz")
        out = published_name("RHOF00ISL_R_20261280000_01D_15S_MO.rnx", r3)
        assert out == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"

    def test_plain_obs_not_uppercased(self):
        plain = VersionPolicy(naming="short", hatanaka=False, compression="Z")
        # no Hatanaka -> name is the obs itself, must not be touched
        assert published_name("ELEY0600.26o", plain) == "ELEY0600.26o.Z"


# --------------------------------------------------------------------------- G1
class _Conv:
    def __init__(self, version, tmp):
        self.obs_name = "ELEY0600.26o"
        self.output_path = tmp / "obs.26o"
        self.output_path.write_text("     2.11           OBSERVATION DATA\n")
        self.rinex_version = version
        self.cached = False


def _min_target(tmp_path):
    from tests.test_dissemination import _target

    return _target(tmp_path)


class TestDecimatedR2IsEmitted:
    def test_decimated_product_produced_for_r2(self, tmp_path, monkeypatch):
        # G1 removed: the 30 s product for genuinely-R2 data (e.g. pre-2012
        # RHOF) is a real, wanted product — run_one(sample=30) on an R2 source
        # must PRODUCE a short-named file (uppercase .D via G3), never skip it.
        eng = eng_mod.EposDisseminate(_min_target(tmp_path))
        src = tmp_path / "RHOF1790.11D.Z"
        src.write_bytes(b"x")
        conv = _Conv(2, tmp_path)
        conv.obs_name = "RHOF1790.11o"
        monkeypatch.setattr(eng, "find_source", lambda s, d: src)
        monkeypatch.setattr(eng, "find_raw_source", lambda s, d: None)
        monkeypatch.setattr(eng_mod, "convert_for_dissemination", lambda *a, **k: conv)
        monkeypatch.setattr(eng, "_session_segment", lambda *a, **k: "30s_24hr")
        monkeypatch.setattr(eng_mod, "package", lambda *a, **k: tmp_path / "pkg")
        monkeypatch.setattr(eng, "_push", lambda *a, **k: True)
        r = eng.run_one("RHOF", date(2011, 6, 28), product=ProductSpec(sample=30))
        assert "decimated" not in r.message  # not skipped
        assert r.long_name == "RHOF1790.11D.Z"  # R2 short, uppercase (G3)
        assert r.ok
