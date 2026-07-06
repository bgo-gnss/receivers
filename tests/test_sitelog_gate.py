"""Content-hash gate: a site log is (re)written only when the station content
changed vs the latest committed one — self-healing a stale/wrong committed log,
no-op'ing when TOS is unchanged."""

from pathlib import Path
from unittest.mock import patch

from receivers.dissemination import sitelogs
from receivers.dissemination.sitelogs import (
    _latest_sitelog,
    _normalize_sitelog,
    generate_site_log_if_changed,
)


def _log(up_ecc: str = "1.0140", prepared: str = "2026-07-05", prev: str = "") -> str:
    return (
        "0.   Form\n"
        f"     Date Prepared            : {prepared}\n"
        f"      Previous Site Log       : {prev}\n"
        "      Modified/Added Sections : 1\n"
        "1.   Site Identification\n"
        "     Site Name                : Raufarhöfn\n"
        "4.2  Antenna Type             : TRM57971.00     NONE\n"
        f"     Marker->ARP Up Ecc. (m)  :   {up_ecc}\n"
    )


# --- normalization ---------------------------------------------------------
def test_normalize_strips_volatile_lines():
    n = _normalize_sitelog(_log(prepared="2026-07-05", prev="rhof_x.log"))
    assert "Date Prepared" not in n
    assert "Previous Site Log" not in n
    assert "Modified/Added Sections" not in n
    assert "Site Name" in n and "Up Ecc" in n


def test_normalize_equal_across_prepared_date_and_prev():
    a = _normalize_sitelog(_log(prepared="2026-07-02", prev="a.log"))
    b = _normalize_sitelog(_log(prepared="2026-07-05", prev="b.log"))
    assert a == b  # only volatile lines differ → normalized identical


def test_normalize_differs_on_real_content():
    assert _normalize_sitelog(_log(up_ecc="1.0070")) != _normalize_sitelog(
        _log(up_ecc="1.0140")
    )


# --- latest-sitelog picker -------------------------------------------------
def test_latest_sitelog_picks_newest(tmp_path):
    for d in ("20260601", "20260702", "20260610"):
        (tmp_path / f"rhof00isl_{d}.log").write_text("x")
    assert _latest_sitelog(tmp_path, "RHOF00ISL").name == "rhof00isl_20260702.log"


def test_latest_sitelog_none_when_absent(tmp_path):
    assert _latest_sitelog(tmp_path, "RHOF00ISL") is None


# --- the gate --------------------------------------------------------------
def _patch_render(content, out_name, tmp_path):
    """Patch _render_sitelog to return (content, out_dir/out_name) and _write to
    record the write and return the path."""
    render = patch.object(
        sitelogs, "_render_sitelog", return_value=(content, tmp_path / out_name)
    )
    write = patch.object(
        sitelogs, "_write_sitelog", side_effect=lambda c, p, s, ll: Path(p)
    )
    return render, write


def test_gate_writes_when_no_prior(tmp_path):
    render, write = _patch_render(_log(), "rhof00isl_20260705.log", tmp_path)
    with render, write as w:
        r = generate_site_log_if_changed("RHOF", tmp_path)
    assert r is not None and r.changed is True
    w.assert_called_once()


def test_gate_noop_when_unchanged(tmp_path):
    # committed log with the SAME station content (older prepared date / prev)
    (tmp_path / "rhof00isl_20260702.log").write_text(
        _log(up_ecc="1.0140", prepared="2026-07-02", prev="old.log")
    )
    render, write = _patch_render(
        _log(up_ecc="1.0140", prepared="2026-07-05"), "rhof00isl_20260705.log", tmp_path
    )
    with render, write as w:
        r = generate_site_log_if_changed("RHOF", tmp_path)
    assert r is not None and r.changed is False
    assert r.path.name == "rhof00isl_20260702.log"  # the existing one
    w.assert_not_called()


def test_gate_self_heals_wrong_committed_log(tmp_path):
    # committed log has the WRONG Up Ecc (1.0070); current render is correct (1.0140)
    (tmp_path / "rhof00isl_20260702.log").write_text(_log(up_ecc="1.0070"))
    render, write = _patch_render(
        _log(up_ecc="1.0140"), "rhof00isl_20260705.log", tmp_path
    )
    with render, write as w:
        r = generate_site_log_if_changed("RHOF", tmp_path)
    assert r is not None and r.changed is True
    w.assert_called_once()


def test_gate_returns_none_on_render_failure(tmp_path):
    with patch.object(sitelogs, "_render_sitelog", return_value=None):
        assert generate_site_log_if_changed("RHOF", tmp_path) is None
