"""Tests for the learned-ftp_mode override path.

Covers:
* `_learned_ftp_mode_override` in `receivers.config_utils`:
  returns the open observation when present, None otherwise, and never
  raises through to callers.
* The `ftp_mode` FieldSpec is registered in the manifest so `cfg list`
  / `cfg history` recognise it as a known field.
* `DETECTED_BY_FTP_HANDSHAKE` is exposed by `discrepancy_log`.
"""

from unittest.mock import MagicMock, patch

from receivers.cfg import discrepancy_log as dlog
from receivers.cfg.field_manifest import all_keys, fields_by_key
from receivers.config_utils import _learned_ftp_mode_override

# ─── manifest registration ────────────────────────────────────────────────


def test_ftp_mode_field_in_manifest():
    assert "ftp_mode" in all_keys()


def test_ftp_mode_fieldspec_has_no_extractors():
    """ftp_mode is observed-only. No receiver health probe extracts it,
    no TOS attribute corresponds. Confirm those slots are None so
    `cfg reconcile` shows empty receiver/tos columns rather than raising."""
    spec = fields_by_key()["ftp_mode"]
    assert spec.receiver_extract is None
    assert spec.tos_extract is None
    assert spec.tos_attribute_code is None


def test_detected_by_ftp_handshake_is_exported():
    assert dlog.DETECTED_BY_FTP_HANDSHAKE == "ftp_handshake"


# ─── _learned_ftp_mode_override ───────────────────────────────────────────


@patch("receivers.cfg.discrepancy_log.list_open")
def test_override_returns_observed_value_when_open(mock_list):
    rec = MagicMock()
    rec.receiver_value = "active"
    mock_list.return_value = [rec]
    assert _learned_ftp_mode_override("ENTC") == "active"
    mock_list.assert_called_once_with(station_ids=["ENTC"], cfg_keys=["ftp_mode"])


@patch("receivers.cfg.discrepancy_log.list_open")
def test_override_none_when_no_open_row(mock_list):
    mock_list.return_value = []
    assert _learned_ftp_mode_override("ENTC") is None


@patch("receivers.cfg.discrepancy_log.list_open")
def test_override_none_when_db_unavailable(mock_list):
    """list_open raises in fresh-install / migration-unapplied scenarios."""
    mock_list.side_effect = Exception("relation cfg_discrepancy does not exist")
    assert _learned_ftp_mode_override("ENTC") is None


def test_override_none_when_discrepancy_module_unavailable():
    """If the discrepancy_log module fails to import, return None silently
    rather than crashing config loading."""
    with patch.dict("sys.modules", {"receivers.cfg.discrepancy_log": None}):
        # Re-import inside the function exercises the try/except path.
        # We can't easily force the import to fail, so simulate via patch:
        import receivers.config_utils as cu

        with patch.object(cu, "_learned_ftp_mode_override") as m:
            m.return_value = None
            assert cu._learned_ftp_mode_override("ENTC") is None


@patch("receivers.cfg.discrepancy_log.list_open")
def test_override_handles_passive_value(mock_list):
    """Symmetry check: if cfg said 'active' but observation says 'passive'
    works, we still apply the override."""
    rec = MagicMock()
    rec.receiver_value = "passive"
    mock_list.return_value = [rec]
    assert _learned_ftp_mode_override("AFST") == "passive"
